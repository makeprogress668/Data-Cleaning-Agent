# API Contract

后端使用 FastAPI。当前保留旧的同步处理接口；Web 控制台使用异步 Job API，
支持受控多轮澄清、计划确认、会话读取、结果读取、复核回写、取消和产物下载。

## 旧接口

### `POST /api/v1/cleaning/process`

用途：上传文件并同步执行处理。

表单字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `files` | file[] | Excel、CSV、JSON、TXT、Markdown、PDF、DOCX 等输入文件 |
| `config` | string | 可选 JSON 字符串，例如 `result_limit`、`include_diagnostics`、`job_overrides` |

同步入口无法暂停展示计划。若计划因高移除比例、影响无法估算、模糊匹配或缺少授权证据
需要复核，会返回 400；确认后可在 config 中传兼容字段
`"confirm_destructive": true`。该字段虽沿用旧名，但批准的是计划中已记录的全部
`confirmation_reasons`，不会放宽 TaskSpec 动作校验。

返回重点字段：

```json
{
  "status": "success",
  "job_id": "...",
  "counts": {},
  "processing": {},
  "files": {
    "download_url": "/api/v1/cleaning/jobs/{job_id}/result.xlsx"
  }
}
```

### `GET /api/v1/cleaning/jobs/{job_id}`

用途：查询同步任务状态。

### `GET /api/v1/cleaning/jobs/{job_id}/result.xlsx`

用途：下载旧接口生成的 Excel 结果。

## Job API

### `POST /api/v1/jobs`

用途：创建异步任务，保存上传文件并立即返回 `job_id`。

表单字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `files` | file[] | 输入文件 |
| `goal` | string | 业务目标 |
| `mode` | string | `discover` 只生成概览；`plan` 只生成方案；`answer` 执行并交付结果 |
| `config` | string | 可选 JSON 配置 |
| `recipe_id` | string | 可选；复用同租户成功任务保存的 TaskSpec，目标不可改写 |
| `semantic_model_id` | string | 可选；使用同租户业务指标/关系模型；不能与 recipe 同时使用 |

### `GET /api/v1/jobs?limit=20`

用途：返回按创建时间倒序排列的真实任务摘要，供任务切换和失效缓存恢复使用。
摘要包含状态、模式、目标、关键行数、质量评分、待复核数，以及
`has_discovery / has_plan / has_result` 页面可用性标记。文件模式下损坏或不完整的
任务目录不会阻塞整个列表；PostgreSQL 模式直接读取持久化任务历史。

### `GET /api/v1/jobs/{job_id}`

用途：读取完整 job 状态，包括 `TaskSpec`、`ExecutionPlan`、`OutputSpec`、
真实图表清单和产物状态。

### `GET /api/v1/jobs/{job_id}/status`

用途：读取轻量状态，前端用于轮询。失败任务额外返回
`failure_stage=planning|execution|delivery`，供进度页准确定位失败阶段；
`review_count` 表示真实复核记录总数，用于开放复核入口；
`pending_review_count` 表示尚未作出业务结论的记录数，用于待复核徽标。
生产队列还返回 `queued_at / started_at / completed_at`、`queue_wait_ms`、
`dispatch_attempts`、`sla_seconds / sla_breached`，用于任务历史和 SLA 监控。

### `GET /api/v1/jobs/{job_id}/session`

用途：读取当前 `TaskSpec`、会话轮次、计划版本、计划 hash 和
`ConversationTurn` 历史。

任务元数据的 API 契约不随存储后端变化：本地默认使用原子 JSON 文件，生产可通过
`DATA_AGENT_METADATA_BACKEND=postgres` 切换 PostgreSQL。后者用事务行锁保护状态、
会话、复核决定和澄清答案的读改写；确认请求在状态 CAS 内同时比较
`plan_id + plan_hash`。
生产异步任务先把稳定 `dispatch_id` 写入 PostgreSQL outbox 再投递 ARQ；Redis 短暂
不可用时保持 `pending` 并自动重放，不要求调用方重新提交。

### `GET /api/v1/jobs/{job_id}/files`

用途：列出 job 输出文件。

### `GET /api/v1/jobs/{job_id}/files/{file_name}`

用途：按文件名下载 job 目录下的输出文件。服务端会限制文件名和目录范围。
S3 模式根据状态中的产物 manifest 按需物化文件，URL 和响应格式与本地模式相同。

### `GET /api/v1/jobs/{job_id}/business-answer`

用途：任务成功后返回结构化 `BusinessAnswer`，包含结论、指标、限量结果预览、
真实复核记录、`OutputSpec` 和产物链接；未完成时返回 404 供前端继续轮询。

### `GET /api/v1/jobs/{job_id}/events`

用途：读取 PlanSnapshot 版本、ExecutionEvent、总执行耗时以及 LLM
调用次数、token 和延迟汇总。不会返回 prompt、样本值或 API Key。

### `GET/POST /api/v1/jobs/{job_id}/review`

用途：GET 按 `offset/limit` 返回真实受阻记录、`has_more` 与已保存决定；
POST 持久化复核结果并支持增量合并。
任务未完成时 GET 返回 404；零异常任务返回空 `items`，不会生成占位记录。
复核状态仅允许 `pending | accepted | excluded`，分别表示待复核、确认可用和
确认不采用。当前接口不接收修正值，也不会直接改写已生成的结果文件。

### 结果版本、撤销与分支

- `POST /api/v1/jobs/{job_id}/rematerialize`：以当前复核决定生成新版本；
- `GET /api/v1/jobs/{job_id}/versions`：返回父子版本图和公开下载 URL；
- `POST /api/v1/jobs/{job_id}/versions/{version_id}/select`：移动当前指针；
- `POST /api/v1/jobs/{job_id}/versions/undo`：移动到当前版本的父节点；
- `POST /api/v1/jobs/{job_id}/versions/{version_id}/branch`：从指定历史节点和独立决定生成
  新版本。

选择和撤销均不删除文件。版本响应不返回服务端路径、对象 key 或完整复核决定。

### Recipe、业务语义层与 Connector

- `GET/POST /api/v1/recipes`、`GET/DELETE /api/v1/recipes/{recipe_id}`；
- `GET/POST /api/v1/semantic-models`、`GET/DELETE /api/v1/semantic-models/{id}`；
- `GET /api/v1/connectors` 返回已安装 connector 及其 JSON Schema；
- `POST /api/v1/connectors/jobs` 以校验后的 connector 配置物化输入并进入同一 JobManager。

Recipe 不保存输入文件或旧 ExecutionPlan；新任务仍对新文件重新画像和编译。Connector
只接受已安装插件 ID 和结构化配置，不接受请求内代码，输出必须位于当前任务目录。

### `GET /api/v1/config`

用途：返回前端所需的服务可用性、业务化数据保护等级和上传限制。该接口不返回
模型名、模型网关、Prompt、内部性能阈值或密钥；启用 API Key 后同样需要鉴权。

### `GET /api/v1/jobs/{job_id}/discovery`

用途：返回真实数据发现视图。尚未生成时返回 404，不以空对象表示“未发现问题”。

### `GET /api/v1/jobs/{job_id}/plan`

用途：返回实际执行方案和 `impact_preview`（预计输入/输出/移除行数、影响字段、
新增字段）。尚未生成时返回 404，不以空方案表示“无需处理”。

### `GET/POST /api/v1/jobs/{job_id}/clarification`

用途：读取下一个 `TaskSpec.missing_slots` 问题、提交回答并幂等恢复任务。
每轮只处理一个槽位，达到配置上限后转计划确认。

### `GET/POST /api/v1/jobs/{job_id}/plan-confirmation`

用途：读取目标理解、能力步骤、产物契约、`plan_id` 和 `plan_hash`，确认指定
`PlanSnapshot` 后恢复执行。POST 必须回传 GET 返回的两个标识：

`execution_plan.steps[*].confirmation_reasons` 会说明为什么暂停：
`high_removal_ratio`（预计移除至少 25%）、`impact_unknown`（影响无法估算）、
`fuzzy_matching`（模糊匹配）或 `missing_authorization`（缺少可引用原话）。
`impact_estimate` 保存作出判定时的输入行数、预计移除行数和比例；客户端只负责翻译展示，
不能自行重算或覆盖。

```json
{
  "plan_id": "32 位十六进制标识",
  "plan_hash": "64 位 SHA-256"
}
```

状态转换与快照标识比较在同一任务锁内完成；旧页面提交过期计划时返回 409，任务保持
`awaiting_confirmation`，用户刷新后才能确认新计划。确认后的执行线程会再次比较同一组
标识，然后才加载快照，不再进入反思改写。

新建的 v7 快照还把 TaskSpec、目标理解、输入文件 SHA-256、字段结构、JobConfig、
ExecutionPlan（含确认原因、影响和阈值）、OutputSpec、Planner、Capability Registry
和计划编译器版本共同纳入 hash。输入指纹使用与 worker 暂存根目录无关的逻辑身份，
因此跨进程恢复不会因为绝对路径变化而误报篡改；v3–v6 按原策略只读兼容。

### `DELETE /api/v1/jobs/{job_id}`

用途：运行中任务先取消，worker 结束后再次调用可物理清理 PostgreSQL 元数据、
对象存储任务前缀和本地缓存。

## 状态约定

前端统一使用：

```text
pending | running | needs_clarification | awaiting_confirmation |
succeeded | failed | cancelled
```

旧接口中的 `completed` / `success` 在前端 API client 中会映射为 `succeeded`。

## 产物约定

- Web、CLI 和 Job API 的默认用户文件统一为 `final_result.xlsx`，只包含「处理结果」sheet。
- 复核 sheet、图表和报告必须由目标显式声明。
- 「处理结果」只包含可直接使用记录；待复核记录只进入显式声明的「问题说明」。
- 审计包必须由调用参数显式开启，且与业务交付物分离。
- Job API 的 `charts` 只返回真实生成文件；不存在占位图表。
- Output Linter 会拒绝未声明 sheet、图表、文件和无来源字段。

## 动作契约

- 明确过滤条件和去重键先写入 `TaskSpec`，再编译为 JobConfig 的
  `filter_rows` / `dedupe` 操作。
- 两个操作直接改变结果行集，不生成布尔标记列。
- TaskSpec 声明的过滤、去重、关联、分析、图表和导出动作必须被
  ExecutionPlan 覆盖；TaskSpec 未声明的派生、分析、图表等能力同样会被拒绝。
- JobConfig 中的过滤/去重规则必须与 TaskSpec 逐项一致，Reflection 候选也执行
  同一校验。
- TaskSpec 的嵌套操作是闭世界契约：未知键、不完整的集合过滤、缺少排序字段的
  latest/earliest 去重、不完整派生等在规划前返回校验错误。
- 新计划返回 `ExecutionPlan.version = 3`。v3 绑定 JobConfig hash；阶段在运行前必须
  已注册、参数和引擎符合 schema、行预算未越界并按顺序匹配计划，事件绑定时再校验
  一次；历史 v1/v2 计划仅按兼容规则读取。
