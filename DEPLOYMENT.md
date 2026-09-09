# 部署指南（生产上线）

本文档说明如何将 **数据炼金师** 从开发环境部署为可上线的生产服务，覆盖 Docker
镜像、docker-compose 编排、环境变量、安全加固、反向代理与运维要点。

架构：nginx（静态前端 + `/api` 反向代理）→ 多 uvicorn API worker → Redis/ARQ
执行 worker。任务状态、会话、计划快照和复核决定保存在 PostgreSQL；上传文件、结果
和复核快照保存在 S3 兼容对象存储（compose 默认 MinIO）。

当前版本支持多租户 API Key 或 OIDC/JWT、角色授权、任务/对象/缓存隔离和按租户配额。
默认 compose 继续由 nginx 注入服务端 Key；接入终端用户登录时，由外部 OIDC auth
proxy 完成登录并向每个请求注入 Bearer token，内置 nginx 会原样转发。完整边界见
[`docs/MULTI_TENANCY.md`](docs/MULTI_TENANCY.md)。

```
浏览器 ──▶ web 容器 (nginx :80)
                 ├── 静态前端 (React 构建产物)
                 └── /api/* ──▶ backend (uvicorn × 2)
                                      ├── PostgreSQL（元数据、CAS、outbox）
                                      ├── Redis（ARQ 持久队列）──▶ worker（确定性执行）
                                      └── MinIO/S3（上传、结果、复核快照）
```

---

## 一、快速开始（docker-compose）

前置：已安装 Docker 与 Docker Compose v2。

```powershell
# 1. 准备环境变量（生产务必修改密钥与来源白名单）
Copy-Item .env.example .env
#    .env.example 里的密钥全部是注释掉的，需要先取消注释再填强值：
#      DATA_AGENT_API_KEY            对外鉴权，也可改用 OIDC 三项
#      DATA_AGENT_DATABASE_PASSWORD  PostgreSQL
#      DATA_AGENT_ARTIFACT_SECRET    MinIO，要和上面那个不同
#    另外把 DATA_AGENT_CORS_ORIGINS 收敛为真实前端域名。
#    漏了不会静默生效：前两个由 compose 的 ${VAR:?} 拦下，
#    API Key 由 DATA_AGENT_ENV=production 在启动阶段拦下。

# 2. 构建并启动
docker compose up -d --build

# 3. 查看状态与日志
docker compose ps
docker compose logs -f backend

# 4. 访问控制台
Start-Process http://localhost:8080
```

停止与清理：

```powershell
docker compose down          # 停止并移除容器（保留数据卷）
docker compose down -v       # 连同 metadata/queue/artifact 数据卷一并删除（谨慎）
```

---

## 二、环境变量

所有变量均可写入根目录 `.env`（compose 通过 `env_file` 注入 backend）。完整清单见
[.env.example](./.env.example)，此处列出生产相关项。

### 安全（强烈建议在生产中设置）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_AGENT_ENV` | `production`（compose） | 生产模式下 API Key 和 OIDC 均未配置时启动失败 |
| `DATA_AGENT_API_KEY` | 条件必填 | 单 Key部署由 nginx 注入；配置 OIDC 时可留空 |
| `DATA_AGENT_TENANT_API_KEYS_JSON` | 空 | 多租户 Key 与 viewer/operator/admin 角色映射；设置后优先于单 Key |
| `DATA_AGENT_OIDC_ISSUER` | 空 | OIDC token 的可信 issuer |
| `DATA_AGENT_OIDC_AUDIENCE` | 空 | 本 API 的 audience |
| `DATA_AGENT_OIDC_JWKS_URL` | 空 | IdP 的 HTTPS JWKS；与静态公钥二选一 |
| `DATA_AGENT_OIDC_ALGORITHMS` | `RS256` | 服务端固定算法白名单，不从 token header 推导 |
| `DATA_AGENT_OIDC_TENANT_CLAIM` / `_ROLE_CLAIM` | `tenant_id` / `role` | 映射现有租户隔离和 RBAC |
| `DATA_AGENT_CORS_ORIGINS` | `*` | 允许的跨域来源，逗号分隔。生产改为你的控制台域名 |
| `DATA_AGENT_RATE_LIMIT_PER_MINUTE` | `600` | 每 IP 每 60s 滑动窗口请求上限，`<=0` 关闭 |

### 运行与容量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_AGENT_API_WORKERS` | `1`（compose 为 `2`） | `thread/file` 模式必须为 1；PostgreSQL + ARQ 可水平扩展 API |
| `DATA_AGENT_FORWARDED_ALLOW_IPS` | `127.0.0.1` | uvicorn 信任的代理 IP；compose 隔离网络内显式设为 `*` |
| `DATA_AGENT_API_MAX_WORKERS` | `2` | 仅本地 thread 模式使用的进程内线程池大小 |
| `DATA_AGENT_JOB_TTL_SECONDS` | `604800` | 启动时清理超过该秒数的历史任务目录（7 天），`<=0` 关闭 |
| `DATA_AGENT_MAX_UPLOAD_BYTES` | `104857600` | 单文件上传上限（100 MB），需与 nginx `client_max_body_size` 保持一致 |
| `DATA_AGENT_LOG_LEVEL` | `INFO` | 结构化日志级别 |
| `DATA_AGENT_API_WORK_DIR` | 容器内 `/data/api_jobs` | 可丢弃的执行暂存目录；compose 用隔离且有上限的 tmpfs |
| `DATA_AGENT_METADATA_BACKEND` | `postgres`（compose） | 本地默认 `file`；生产使用 PostgreSQL |
| `DATA_AGENT_UNDERSTANDING_CACHE_BACKEND` | `postgres`（compose） | 生产 worker 共享理解缓存；本地默认跟随文件元数据后端 |
| `DATA_AGENT_UNDERSTANDING_CACHE_TTL_SECONDS` | `2592000` | 理解缓存最长复用 30 天；`<=0` 禁用过期，不建议生产使用 |
| `DATA_AGENT_TENANT_ID` | `default` | 本地或旧单 Key 模式的默认租户 |
| `DATA_AGENT_MAX_ACTIVE_JOBS_PER_TENANT` | `4`（compose） | 每租户活跃任务硬上限；PostgreSQL 原子准入 |
| `DATA_AGENT_MAX_BATCH_UPLOAD_BYTES` | `524288000` | 单次请求所有文件合计上限 |
| `DATA_AGENT_MAX_STORED_BYTES_PER_TENANT` | `0` | 租户对象存储额度；`0` 关闭 |
| `DATA_AGENT_MIN_FREE_TEMP_BYTES` | `268435456` | 接收任务前要求的临时盘最低余量 |
| `DATA_AGENT_DATABASE_URL` | compose 自动组装 | PostgreSQL conninfo/DSN |
| `DATA_AGENT_DATABASE_POOL_SIZE` | `10` | 每个后端进程的元数据连接池上限 |
| `DATA_AGENT_METADATA_AUTO_MIGRATE` | `1` | 首次启动时幂等复制旧 JSON 元数据；不覆盖数据库、不删除源文件 |
| `DATA_AGENT_JOB_QUEUE_BACKEND` | `thread`（compose 为 `arq`） | ARQ 模式要求 PostgreSQL 与 Redis |
| `DATA_AGENT_REDIS_URL` | 空 | ARQ 连接地址 |
| `DATA_AGENT_ARQ_MAX_JOBS` | `1`（compose） | 每 worker 容器单任务，使容器资源上限可作为任务边界 |
| `DATA_AGENT_ARQ_JOB_TIMEOUT` | `3600` | 单次执行超时秒数 |
| `DATA_AGENT_ARQ_MAX_TRIES` | `3` | worker 丢失、连接中断等基础设施失败的最大尝试数 |
| `DATA_AGENT_OUTBOX_INTERVAL_SECONDS` | `30` | PostgreSQL outbox 未成功投递时的重放间隔 |
| `DATA_AGENT_ARTIFACT_BACKEND` | `local`（compose 为 `s3`） | 生产使用 S3 兼容对象存储 |
| `DATA_AGENT_ARTIFACT_BUCKET` | 空 | 对象存储 bucket |
| `DATA_AGENT_S3_ENDPOINT_URL` | 空 | MinIO 等兼容服务端点；AWS S3 可留空 |
| `DATA_AGENT_ARTIFACT_PREFIX` | `jobs` | bucket 内任务命名空间前缀 |
| `DATA_AGENT_JOB_SLA_SECONDS` | `3600` | 端到端 SLA；状态返回是否超时、排队耗时和派发次数 |
| `DATA_AGENT_DUCKDB_MIN_ROWS` | `100000` | 达到阈值且语义兼容时下推过滤/聚合/确定性关联 |
| `WEB_PORT` | `8080` | 宿主机上前端对外暴露的端口 |

### LLM 与智能增强（可选）

不配置时全程走确定性引擎，功能完整可用；配置后启用"LLM 规划 + 反思闭环"两层智能。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_AGENT_LLM_API_KEY` | 空 | OpenAI 或任意 OpenAI 兼容网关的密钥；为空则关闭 LLM 层 |
| `DATA_AGENT_LLM_BASE_URL` | OpenAI 公有 API | 内网网关填你的代理地址 |
| `DATA_AGENT_LLM_MODEL` | `gpt-4o-mini` | 端点提供的模型名 |
| `DATA_AGENT_REFLECTION_ENABLED` | `1` | 反思闭环开关（无 LLM 时自动为空操作） |
| `DATA_AGENT_REFLECTION_MAX_ROUNDS` | `1` | 反思最大轮数 |
| `DATA_AGENT_REFLECTION_TARGET_SCORE` | `90` | 质量分低于该目标才触发反思 |

CLI 和未经过人工确认的 API 任务可使用同一套有界反思：质量分低于目标且配置了
LLM 时，系统让模型提出一处经校验的 JobConfig 调整并重跑；候选必须再次通过
TaskSpec 闭世界一致性校验。人工确认后的 PlanSnapshot 严格原样执行，不进入反思。

---

## 三、单独构建镜像

如不使用 compose，也可分别构建：

```bash
# 后端（构建上下文为 backend/）
docker build -t data-agent-backend:latest ./backend

# 前端（构建上下文为 apps/web/）
docker build -t data-agent-web:latest ./apps/web
```

单独运行生产后端前，需先提供外部 PostgreSQL、Redis 和 S3 兼容服务，并在 `.env`
设置对应地址；本地单机模式可继续使用 file/thread/local 默认值：

```powershell
docker run -d --name data-agent-backend `
  --env-file .env `
  -e DATA_AGENT_API_WORKERS=1 `
  -p 8000:8000 `
  data-agent-backend:latest
```

运行前端（反向代理到后端容器）：

```powershell
docker run -d --name data-agent-web `
  -e BACKEND_HOST=data-agent-backend `
  -e BACKEND_PORT=8000 `
  -e DATA_AGENT_API_KEY="$env:DATA_AGENT_API_KEY" `
  --link data-agent-backend `
  -p 8080:80 `
  data-agent-web:latest
```

> 前端默认以同源 `/api` 访问后端，由 nginx 反向代理并在服务端注入 API Key。
> 若前端需直连不同域名的后端，
> 可在构建时传入 `--build-arg VITE_API_BASE_URL=https://api.example.com`。

---

## 四、多 worker 与故障恢复

- API 先在 PostgreSQL 状态文档中提交 `pending + queue_message`，再投递 Redis；Redis
  暂时不可用时 outbox 周期重放，因此不会出现“返回 job_id 但任务永久丢失”。
- 每次派发使用稳定 `dispatch_id`；ARQ 去重，worker 再以 PostgreSQL CAS 领取，过期
  消息或取消后的消息不能执行。
- API 重启不会把远端执行中的任务误判失败；worker 丢失后 ARQ 使用同一派发标识重试。
- 上传先写入对象存储再入队。任意 worker 将它们物化到自己的临时目录；完成后全部
  交付物和复核快照回传对象存储。PlanSnapshot v7 的输入指纹不绑定本机绝对路径，
  并把 TaskSpec、文件内容、capability registry 和编译器版本一起绑定到 plan hash。
- 本地 `file + thread + local` 模式仍强制一个 API worker，行为与原开发环境兼容。
- HTTP 限流窗口仍是 API 进程级近似值；公网多副本应在统一网关再配置全局限流。

---

## 五、反向代理与 HTTPS

生产建议在 web 容器前再放一层网关（如云负载均衡 / 独立 nginx / Caddy）终止 TLS：

- 转发 `X-Forwarded-Proto`、`X-Forwarded-For`；代理必须覆盖客户端传入的
  `X-Forwarded-For`，并通过 `DATA_AGENT_FORWARDED_ALLOW_IPS` 限定可信代理。
- 上传较大文件时放宽 `client_max_body_size` 与读写超时（内置 nginx 已设 128m / 600s）。
- 将 `DATA_AGENT_CORS_ORIGINS` 收敛为实际前端域名，并设置 API Key 或 OIDC。

---

## 六、健康检查与可观测性

- **健康检查**：`GET /health` 返回 `{"status":"ok"}`，后端与前端镜像均内置
  `HEALTHCHECK`，compose 中 web 依赖 backend `service_healthy` 才启动。
- **请求追踪**：每个请求分配 `X-Request-ID`（可由客户端透传），出现在结构化日志
  与响应头中，便于按 ID 定位一次失败。
- **任务观测**：`GET /api/v1/jobs/{job_id}/events` 返回计划版本、执行事件、总耗时和
  LLM token/延迟汇总；状态摘要另含 `queue_wait_ms`、`dispatch_attempts` 和
  `sla_breached`。DuckDB 下推步骤在事件 `metrics.engine` 中标识真实引擎。
- **日志**：结构化输出到 stdout，交由容器/编排平台采集：

  ```powershell
  docker compose logs -f backend worker
  ```

---

## 七、数据持久化与备份

- PostgreSQL 元数据位于 `metadata_data`，Redis AOF 位于 `queue_data`，MinIO 对象位于
  `artifact_data`。容器内 `/data/api_jobs` 只是临时缓存，不纳入备份。
- 元数据优先使用 `pg_dump`；对象存储使用版本化 bucket/生命周期策略或存储平台快照。
  两者应使用相同备份批次标识。
- 定期备份：

  ```powershell
  docker run --rm -v data-cleaning-agent_artifact_data:/data -v "${PWD}:/backup" `
    alpine tar czf /backup/artifact_data_backup.tgz -C /data .
  ```

- 历史任务到期时会同时删除 PostgreSQL 文档、对象前缀和本地缓存。

---

## 八、上线前检查清单

- [ ] 已在 `.env` 中取消注释并设置强 `DATA_AGENT_API_KEY`，或 OIDC issuer/audience/JWKS 与 claim 映射
- [ ] 已取消注释并设置强 `DATA_AGENT_DATABASE_PASSWORD`，PostgreSQL 健康检查通过
- [ ] 已取消注释并设置不同的强 `DATA_AGENT_ARTIFACT_SECRET`，MinIO bucket 初始化成功
- [ ] `DATA_AGENT_CORS_ORIGINS` 收敛为真实前端域名（非 `*`）
- [ ] 前置网关终止 HTTPS，转发 `X-Forwarded-*`
- [ ] `DATA_AGENT_MAX_UPLOAD_BYTES` 与 nginx `client_max_body_size` 一致
- [ ] `metadata_data`、`queue_data`、`artifact_data` 已纳入备份/恢复演练
- [ ] 如需 LLM：`DATA_AGENT_LLM_API_KEY` 等已配置且网关可达
- [ ] 已验证 `GET /health` 与一次完整任务闭环（上传 → 轮询 → 下载结果）
