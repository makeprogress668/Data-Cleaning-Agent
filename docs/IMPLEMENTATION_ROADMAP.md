# 四阶段整改进度与验收

本文按外部审查提出的四阶段路线记录完成度；仓库 PRD 自身的 Phase 编号不同，不混用。
完成度只按已实现且有验收证据的内容计算。

## 总览

| 阶段 | 当前状态 | 完成度 | 尚未闭环 |
| --- | --- | ---: | --- |
| 前置：澄清优先级一致性 | 已完成 | 100% | 无 |
| 第一阶段：发布安全 | 已完成 | 100% | 无 |
| 第二阶段：执行内核收口 | 已完成 | 100% | 无 |
| 第三阶段：Agent 评测体系 | 已完成 | 100% | 无 |
| 第四阶段：规模化和产品能力 | 已完成 | 100% | 无 |

## 前置修复

TaskSpec 的 `high/medium/low` 现在稳定映射为公开 API 的“高/中/低”，不再把 medium
显示成“高”。回归测试覆盖非阻塞问题仍可展示、但不会暂停任务。

## 第一阶段：发布安全

已完成：

- 确认接口以 `plan_id + plan_hash` 原子比较，执行前再次校验。
- PlanSnapshot v7 绑定 TaskSpec、输入内容/结构、JobConfig、ExecutionPlan、OutputSpec、
  Planner、Capability Registry 和 compiler 版本。
- 上传内容按不可信文本处理；只有计划声明的系统公式可以写成 Excel 公式。
- 理解缓存绑定租户、语义 profile、prompt/model 版本并带 TTL；原始值不进入 key。
- API Key 租户映射、请求身份、任务/对象/缓存/长期记忆隔离和租户配额形成闭环。
- 仓库指定虚拟环境可运行全量测试和验收。

验收标准：确认过期计划返回冲突；输入/契约/编译器漂移使 hash 失效；公式注入测试、
缓存隔离测试、后端全量测试和工作簿验收矩阵通过。

## 第二阶段：执行内核收口

已完成：

- TaskSpec v2 使用 Pydantic discriminated union 表达类型化操作，并保留只读兼容投影。
- TaskSpec → JobConfig → ExecutionPlan v3 使用共享编译/校验链，ExecutionPlan 绑定
  JobConfig hash。
- Runner 只执行 Capability Registry 注册能力，并强制参数 schema、engine、effects、
  row budget 和 acceptance rules。
- OutputSpec v2 声明精确字段、字段策略、行范围、sheet 顺序和最小产物边界。
- 运行事件必须回绑计划步骤；未注册、未声明、乱序或配置漂移都会失败。
- 修复汇总到主表时误复制原始明细字段、以及重复询问统计维度的入口漂移。

验收标准：契约闭世界测试、registry/runner/output linter 测试和最终工作簿验收通过；
CLI 与 API 复用同一规划/执行服务。

## 第三阶段：Agent 评测体系

已完成的体系见 `docs/EVALUATION.md`：

- 110 个中文任务、15 个场景、至少 8 个业务域；包含 5 组各 20 种说法。
- 覆盖负面操作、1%/50%/99% 删除、多层表头、隐藏行、数字字符串、公式注入、
  一对多、合并、宽转长、交叉表和最小产物。
- 回放生产链路并读取最终工作簿；检查意图、能力、精确行列和值、禁止操作、公式、
  PlanSnapshot 水合一致性和 LLM usage/latency/token。
- CI 每次运行 51 条确定性底线；59 条模型表达明确跳过，不计作通过。
- 当前 v10 完整冷缓存实际执行 59 条模型任务，最终 59/59，意图、能力、输出和结果语义
  均为 1.0；原始报告保留在本地评测目录，不提交运行产物。
- 当前 v10 完整热缓存最终 59/59，四项指标均为 1.0，且 59 条执行来源均校验为
  `llm_cached`；后续按版本变化重新执行评测门禁。
- 冷测发现并修复“复核清单”被误解为扣留主结果行、以及已给出判断标准仍重复追问；
  热测发现并修复缺失 ID 处理方式被错误列为阻塞问题。
- 评测器支持带版本校验的工作簿离线重评分和定点重跑合并。重评分/合并本身不调用
  LLM，并在报告中记录原始报告、替换任务和每条产物来源，避免为断言修正重复付费。
- 冷/热运行已经实际暴露并推动修复澄清优先级、能力证据、问题报告与行扣留分离、
  趋势聚合、报告/图表类型和变更清单等契约缺口；Golden 同时增加了更严格的行级断言。

当前本地发布证据：609 个后端测试通过；Ruff 通过；工作簿验收 7/7；确定性 Golden
51/51；前端 production build 通过；Docker Compose 的 PostgreSQL、Redis、MinIO、
backend、worker、web 均通过健康检查，nginx 入口和 XLSX 下载通过真实运行时验收。

验收结论：确定性、冷缓存、热缓存三道 Golden 门禁及常规工程检查全部通过，第三阶段
已形成“任务集 → 真实生产链路 → 最终工作簿断言 → 模型/缓存观测 → 定点修复 → 可审计
增量复验”的闭环，可标记为 100% 完成。

## 第四阶段：规模化和产品能力

已完成：

- PostgreSQL 元数据、事务 CAS 和旧文件幂等迁移。
- Redis/ARQ 持久队列、PostgreSQL outbox、稳定 dispatch id、重试和恢复。
- S3/MinIO 产物存储；生产 API 与 worker 不依赖共享文件卷。
- DuckDB 大 CSV 读取，以及语义安全的过滤、聚合和确定性关联下推；关系算子只在
  交付边界物化匹配位置，保留 pandas 等价回退。
- 多 API/worker、任务历史、基础设施重试和 SLA 状态。
- API Key 到 `tenant_id + role` 的请求身份，跨租户任务访问返回 404。
- PostgreSQL 租户复合索引、对象租户前缀、理解缓存和长期记忆租户隔离。
- PostgreSQL 原子并发准入，以及单批上传、租户存储和临时磁盘配额。
- compose 的 CPU、内存、PID、tmpfs 和单 worker 单任务硬资源边界。
- nginx 使用 Docker DNS 动态解析 backend，web 健康检查穿透到后端；只重建 backend
  不会再让一个表面健康的 web 容器持续返回 502。

- 成功任务可保存为租户级 Recipe；Recipe 只保存已验证 TaskSpec/目标定义，新输入仍重新
  画像、编译计划、计算 hash 和执行确认，不复用旧文件或旧执行快照。
- 结果版本形成不可删除的父子图；支持选择、undo 和从任意历史版本 branch，所有版本
  都保留独立工作簿，公开 API 不暴露内部文件路径。
- 租户级业务语义层声明 entity、dimension、measure 和 relationship，可把自定义业务词
  确定性编译为 TaskSpec，并以声明关系替代猜测关联。
- Connector SDK 通过 `data_agent.connectors` entry point 注册可信插件，配置由 Pydantic
  schema 校验，产物强制限制在任务目录和支持的文件类型内；内置 JSON records 与历史
  任务产物 connector。
- OIDC/JWT 支持静态公钥或 JWKS，强制 signature、algorithm allow-list、issuer、audience、
  expiry 和 subject 校验，并把可信 tenant/role claim 接入现有隔离与 RBAC；API Key 保持
  向后兼容。
- Web 控制台提供 Recipe/语义模型选择、保存 Recipe，以及结果版本选择、撤销和分支入口。
- Golden 报告除 prompt/model/registry 外，进一步绑定编译器和完整 Python 源码指纹，
  防止跨实现复用旧产物重评分。

第四阶段验收标准：Recipe 复用不调用理解模型且针对新输入重新编译；语义指标/关系输出
值正确且零 LLM；connector 边界逃逸被拒绝；版本选择/撤销/分支不删除历史；大表关联
实际记录 DuckDB 引擎且与 pandas 契约一致；真实 RSA OIDC token 通过、错误 audience
拒绝；前端生产构建、后端全量测试、工作簿矩阵和最终 compose 队列任务全部通过。

多租户身份与运维边界见 `docs/MULTI_TENANCY.md`。
