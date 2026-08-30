# Backend Architecture

后端目标是保持现有 CLI/API/demo/tests 可用，同时把代码边界调整为更适合产品化演进的结构。

## 设计原则

- 保留 Python 包名 `data_agent`。
- 保留现有确定性数据处理逻辑，不做重写。
- CLI 和 API 只负责输入输出适配，业务能力通过 `services/` 复用。
- LLM 只生成受控意图/计划，确定性执行器负责所有数据变换。
- 新增模块必须替代现有重复职责，不创建无实现的 bridge 目录。

## 主流程

```text
CLI / API
  -> services
  -> agent / planning
  -> TaskSpec / TaskSession
  -> JobConfig
  -> Capability Registry / ExecutionPlan
  -> OutputSpec / PlanSnapshot
  -> pipelines
  -> tools
  -> Output Linter
  -> exporter / optional report, charts and audit
  -> S3-compatible Artifact Store
  -> ExecutionEvent / LLM usage
```

## 关键入口

| 入口 | 文件 |
| --- | --- |
| CLI | `backend/src/data_agent/cli/main.py` |
| FastAPI | `backend/src/data_agent/api/app.py` |
| 旧 API 兼容 | `backend/src/data_agent/api/app.py` |
| 新 Job API | `backend/src/data_agent/api/routes.py` |
| 异步任务编排 | `backend/src/data_agent/api/job_manager.py` |
| 持久队列 / ARQ worker | `backend/src/data_agent/api/job_queue.py` / `arq_worker.py` |
| 元数据 Repository | `backend/src/data_agent/api/metadata_repository.py` |
| 对象存储边界 | `backend/src/data_agent/api/artifact_store.py` / `artifact_service.py` |
| 共享处理服务 | `backend/src/data_agent/services/processing.py` |
| Planning | `backend/src/data_agent/planning/` |
| 能力注册与计划编译 | `backend/src/data_agent/capabilities/` |
| 确定性执行 | `backend/src/data_agent/pipelines/runner.py` |
| 大表 DuckDB 过滤/聚合/关联下推 | `backend/src/data_agent/tools/duckdb_runner.py` |
| Recipe | `backend/src/data_agent/api/recipe_store.py` |
| 业务语义层 | `backend/src/data_agent/semantic_layer.py` |
| Connector SDK | `backend/src/data_agent/connectors/` |
| OIDC/API Key 身份 | `backend/src/data_agent/authentication.py`、`tenancy.py` |
| 受控数据上下文 | `backend/src/data_agent/agent/data_context.py` |
| 统一意图契约 | `backend/src/data_agent/schemas/task.py` |
| 任务会话 | `backend/src/data_agent/schemas/session.py` |
| 计划快照 | `backend/src/data_agent/schemas/plan.py` |
| 产物契约 | `backend/src/data_agent/schemas/output.py` |
| 产物校验 | `backend/src/data_agent/tools/output_linter.py` |
| 离线评测与回放 | `backend/src/data_agent/evaluation/` |
| 执行与 LLM 观测 | `backend/src/data_agent/observability/` |

## 正确性约束

- `TaskSpec` 的过滤、去重、派生、合并、汇总、宽转长、关联和输出意图都使用
  闭世界结构化类型；未知字段和不完整规则在进入 JobConfig 前即被拒绝。
- `planning/goal_interpreter.py` 是 TaskSpec 到 JobConfig 破坏性操作的唯一编译入口。
- `capabilities/confirmation.py` 将授权和风险升级分开判定：引用原话负责授权；
  预计移除比例达到 25%、影响无法估算或使用模糊匹配时要求确认。原因码和影响证据
  固化在 ExecutionStep 中，CLI、同步 API、异步 Job API 共用同一结果。
- LLM 精修后会重新施加 TaskSpec 操作，不能删除用户已明确的过滤或去重要求。
- 默认数据上下文会模式化敏感字段、自由文本和内容中疑似联系方式；Planner 只接收
  去路径、去数据字面值的安全 JobConfig 投影。
- ExecutionPlan 必须覆盖 TaskSpec 中可执行动作，也不能包含 TaskSpec 未声明的
  用户语义能力；否则计划阶段拒绝执行。
- Capability Registry 同时拥有公式算子到能力的绑定；Planner 和 Runner 都从注册表
  解析，不再维护两份映射。`project_schema`、`write_formulas` 等交付阶段也属于白名单。
- 新生成的 ExecutionPlan 为 v3：除步骤顺序与真实执行顺序一致外，还绑定 JobConfig
  hash。未注册、未声明、参数不合法、配置漂移或乱序阶段会在运行前被拦截；v1/v2
  计划只做兼容读取。
- Reflection 候选不能更换主实体，且必须重新通过 TaskSpec 动作、目标关联字段和
  破坏性操作一致性校验。
- v7 PlanSnapshot 将 TaskSpec/目标理解、输入文件内容指纹、JobConfig、ExecutionPlan、
  OutputSpec 和 Planner/能力注册表/编译器版本绑定到同一个 hash。确认接口原子比较
  `plan_id + plan_hash`，执行线程再次校验后才加载快照；输入身份使用可移植逻辑名，
  同一对象下载到另一 worker 的暂存根目录仍可恢复；v3–v5 只读兼容。
- 用户确认 PlanSnapshot 后严格执行快照，不再通过 Reflection 修改计划。
- Excel 导出默认将来自上传文件的公式样式字符串写成普通文本；只有 Runner 明确登记的
  系统生成公式列可以写成可执行公式，避免把不可信数据升级为工作簿代码。
- `dirty_data` 的安全文本标准化由 `runner` 真正执行，ExecutionPlan 不记录未执行步骤。
- `filter_rows` 与 `dedupe` 直接改变 DataFrame 行集，不生成内部布尔结果列。
- Lookup 同名字段使用声明的 suffix，Output Linter 将其视为可追溯字段。
- 默认交付主表只包含可直接使用记录，问题记录进入目标显式声明的「问题说明」。
- Golden Task 走生产规划与执行链路，按每个声明 sheet 校验意图、能力、精确行列、
  值级不变量、未授权操作和公式安全；基线任务被删除同样视为回归。完整门禁见
  `docs/EVALUATION.md`。
- Job API 将完整复核清单独立持久化并分页读取，轻量状态文件只保留预览和计数。
- 元数据通过 `MetadataRepository` 统一保存。开发环境默认保持原子 JSON 文件；生产
  可切换 PostgreSQL，状态、会话、计划快照、澄清答案和复核决定的读改写使用事务行锁。
- PostgreSQL 状态文档同时保存 `plan_id + plan_hash`，确认接口在同一行 CAS 中校验
  生命周期与计划身份。旧 JSON 只做幂等复制，数据库已有内容永远不会被旧文件覆盖。
- ARQ 模式使用 PostgreSQL 状态文档作为 transactional outbox：先持久化稳定
  `dispatch_id` 再投递 Redis，启动与周期任务会重放未领取消息。worker 只执行 CAS
  领取成功的当前消息，API 重启不改变远端任务状态。
- 对象存储先持久化上传再入队；worker 在私有暂存目录物化输入，完成后提交结果、报告、
  图表、复核清单与结果快照。下载和复核回写按 manifest 按需拉取，不依赖共享文件卷。
- 大表 CSV 解析、常用过滤、数值安全的 group aggregate 和确定性 lookup 达到阈值后
  使用 DuckDB；匹配结果在交付边界物化，模糊/特殊业务语义保持 pandas 路径；
  商业数字字符串等无法保证等价的情况自动留在共享 pandas 语义路径。实际引擎进入
  ExecutionEvent，而不是根据计划猜测。

## 兼容策略

- `backend/src/data_agent/api/__init__.py` 导出 FastAPI `app`。
- `data-agent` CLI 入口保持 `data_agent.cli.main:app`。
- 旧同步 API 保留一个兼容周期，新能力只增加到 Job API。
- `tools/`、`business_report/`、`services/business_delivery.py` 继续作为真实实现，
  不再创建平行 bridge 包。

## 后续演进

- 当单任务长期超过 10 分钟、需要跨多次人工暂停或补偿事务时，再由 ARQ 迁移 Temporal。
- 继续用真实基准决定 DuckDB join/画像的下推范围；当前没有证据证明 Polars 能覆盖
  这套 Excel/对象列语义，因此不引入第二套 DataFrame 契约。
- 将进程级 HTTP 限流迁移到网关或 Redis，以获得多 API 副本的全局配额。
- 将规则来源、置信度、冲突处理和字段级血缘进一步结构化。
