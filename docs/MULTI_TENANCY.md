# 多租户鉴权、隔离与配额

多租户身份支持两条路径：受控 API 客户使用独立 API Key；终端用户使用 OIDC/JWT。
两者都在服务端解析为 `tenant_id + role`，客户端不能通过业务请求参数自行指定租户。
生产环境两种方式都未配置时服务启动失败，只有认证已正确配置后才接受业务请求。

## 配置

```dotenv
DATA_AGENT_TENANT_API_KEYS_JSON={"acme":{"api_key":"...","role":"admin"},"demo":{"api_key":"...","role":"viewer"}}
```

角色权限：

- `viewer`：只读请求；
- `operator`：可创建、确认、复核任务，但不能删除；
- `admin`：完整租户权限。

配置多租户映射后，它优先于旧的 `DATA_AGENT_API_KEY`。不同租户不能复用同一 Key。

OIDC 示例：

```dotenv
DATA_AGENT_OIDC_ISSUER=https://id.example.com/
DATA_AGENT_OIDC_AUDIENCE=data-agent
DATA_AGENT_OIDC_JWKS_URL=https://id.example.com/.well-known/jwks.json
DATA_AGENT_OIDC_ALGORITHMS=RS256
DATA_AGENT_OIDC_TENANT_CLAIM=tenant_id
DATA_AGENT_OIDC_ROLE_CLAIM=role
```

Bearer token 强制校验签名、固定算法白名单、issuer、audience、expiry 和 subject。默认
必须提供租户 claim，角色缺省为最小权限 `viewer`。登录跳转、MFA、用户停用和 token
签发由外部 IdP/OIDC auth proxy 负责；proxy 向请求注入 Bearer，仓库不保存用户密码。
API Key 与 Bearer 同时出现时以
Bearer 为准，不用服务端 Key 掩盖无效用户令牌。

## 隔离边界

- 每个任务状态持久化 `tenant_id`；列表按租户过滤，访问其他租户任务统一返回 404。
- PostgreSQL 有 `(tenant_id, job_id, document_kind)` 唯一索引；`job_id` 仍是全局随机
  UUID，兼容已有 API 和历史数据。
- 对象 key 固定为 `<prefix>/tenants/<tenant_id>/<job_id>/...`；删除和 TTL 清理使用
  原任务所属租户的前缀。
- 理解缓存 key 包含租户；长期记忆在多租户模式下写入
  `tenants/<tenant_id>/` 独立目录；后台 worker 从队列配置恢复租户上下文。
- API Key 不写入任务配置、日志、缓存或评测报告。

## 配额与资源边界

```dotenv
DATA_AGENT_MAX_ACTIVE_JOBS_PER_TENANT=4
DATA_AGENT_MAX_BATCH_UPLOAD_BYTES=524288000
DATA_AGENT_MAX_STORED_BYTES_PER_TENANT=0
DATA_AGENT_MIN_FREE_TEMP_BYTES=268435456
```

`0` 表示禁用对应配额。PostgreSQL 模式用 tenant advisory lock 把“统计活跃任务并创建
pending 状态”放进同一事务，多个 API 实例不会同时穿透配额。本地文件模式用进程内租户
锁。批量上限按实际落盘字节累计；存储准入按“已有占用 + 本批实际字节”投影，临时盘
不足或投影超额返回 507，并发已满返回 429，批量过大返回 413。多 API 实例下的存储
投影属于准入保护而非计费级强一致预留；如果需要严格容量售卖，应再引入数据库用量账本。

生产 compose 还对 API/worker 设置 CPU、内存、PID 和 2 GiB tmpfs 硬边界。worker
每容器并发为 1，使容器资源上限近似等同单任务上限；横向扩容通过增加 worker 副本，
任务执行超时仍由 `DATA_AGENT_ARQ_JOB_TIMEOUT` 控制。

## 验收

```powershell
backend\.venv\Scripts\python.exe -m pytest `
  backend/tests/test_middleware.py `
  backend/tests/test_tenant_quotas.py `
  backend/tests/test_artifact_store.py `
  backend/tests/test_metadata_repository.py -q
```

验收覆盖 Key 和真实 RSA OIDC token 到租户/角色解析、错误 audience 拒绝、跨租户 404、
租户列表过滤、并发原子准入、对象/记忆命名空间，以及 413/429/507 配额拒绝。
