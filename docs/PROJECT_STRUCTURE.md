# 项目结构

单仓库、前后端分离。后端 Python 包名为 `data_agent`，前端独立在 `apps/web/`，根目录只放共享文档、运行数据、配置、脚本和 Skill。

```text
data-cleaning-agent/
  backend/
    pyproject.toml
    README.md
    Dockerfile
    src/data_agent/
      agent/                # 理解层：LLM 客户端、目标理解、planner、反思闭环、prompts、长期记忆
      api/                  # FastAPI app、Job API、异步任务管理、状态存储、安全中间件
      capabilities/         # Capability Registry、tools 映射、ExecutionPlan 编译
      cli/                  # data-agent 命令行入口
      evaluation/           # Golden Task Suite、离线回放和版本对比
      connectors/           # 内置 Connector 与第三方 entry-point SDK
      semantic_layer.py     # 租户级业务指标、实体和关系定义
      authentication.py     # OIDC/JWT 验证与身份映射
      observability/        # LLM usage/latency 与 ExecutionEvent
      planning/             # 目标解析与能力推断（goal_interpreter）
      services/             # CLI/API 共用的应用服务（processing / business_delivery）
      pipelines/            # 确定性执行管线（runner）
      tools/                # 确定性能力库：读取、画像、lookup、公式、脏数据、规则、评分、分析、图表、审计、导出
      business_report/      # Markdown / HTML 业务报告渲染
      schemas/              # TaskSpec、ExecutionPlan、PlanSnapshot、OutputSpec、JobConfig
      utils/                # 错误到中文友好提示的转换
    tests/
  apps/web/
    package.json
    Dockerfile
    src/
      api/                  # 后端请求封装、类型定义、状态标签
      routes/               # 页面级容器
      components/           # 布局、上传、任务、报告、表格、状态组件
      hooks/                # 任务提交、状态轮询、结果读取、澄清、计划确认
      styles/               # 全局样式
  docs/                     # 结构、API、前后端架构、验收文档
  data/                     # input / output / demo / api_jobs / evaluation（默认不提交）
  configs/                  # JobConfig 示例和生成结果
  scripts/                  # 样例数据、端到端 demo、本地 CLI demo
  skills/                   # 可版本化复用的高频 Data Agent 操作 Skill
```

## 放置规则

- 后端业务能力放在 `backend/src/data_agent/`。
- CLI 入口放在 `backend/src/data_agent/cli/`，FastAPI 入口放在 `backend/src/data_agent/api/`。
- 前端页面、组件、hooks、API client 放在 `apps/web/src/`。
- 共享运行数据放在根目录 `data/`，不要放到 `backend/data/`。
- 生成或人工维护的 JobConfig 放在 `configs/`。
- 可复现 demo 和样例数据脚本放在 `scripts/`。
- Agent 操作流程放在 `skills/`；只包装稳定 CLI/API，不在 Skill 中实现数据变换。
- 运行产物写入 `data/output/`、`data/demo/`、`data/api_jobs/`，默认不提交真实数据（见 `.gitignore`）。

## 后端分层边界

- `agent/`：理解层。`llm_client` 封装 OpenAI 兼容网关；`data_context`
  统一控制元数据、脱敏样本和可信样本三档上下文；`goal_understanding` 做
  LLM 优先、规则兜底的目标理解；`planner` 起草并校验候选 JobConfig；
  `reflection` 实现执行后有界反思闭环；`memory` 是可选长期记忆。LLM
  只产出候选 JSON，不直接执行或修改数据。
- `planning/`：`goal_interpreter` 把目标映射到主表/字段/能力，并把 TaskSpec
  中明确的过滤与去重要求编译为 JobConfig，是两条规划入口共用的确定性底座。
- `capabilities/`：注册可用确定性能力，声明版本、参数、风险、字段影响和失败策略；
  注册表同时维护公式算子绑定；`confirmation` 统一计算确认原因和影响证据；其余模块
  把 JobConfig 编译成与真实运行顺序一致的 ExecutionPlan，并校验 TaskSpec 动作覆盖。
- `services/`：应用服务层，对 CLI/API 提供稳定封装。`processing` 负责画像→规划→执行的拆分（`plan_input_paths` + `execute_planned_job`）与反思包装；`business_delivery` 负责一页式业务交付。
- `pipelines/`：`runner` 是确定性执行管线，串联读取、清洗、匹配、公式、校验、导出。
- `tools/`：确定性能力库，所有数据变换都在这里；`output_linter` 在写出前校验产物边界。
- `evaluation/`：版本化 Golden Tasks、生产链路离线回放、指标聚合和基线回归对比；
  场景范围、模型/缓存矩阵与发布门禁见 `docs/EVALUATION.md`。
- `observability/`：记录不含 prompt/数据的 LLM usage；v2 计划下拒绝未注册、未声明或
  乱序的执行阶段，再将真实指标绑定到 ExecutionPlan。
- `api/`：HTTP 输入输出、上传、异步任务生命周期（`job_manager`）、Redis/ARQ 派发
  与 worker、状态门面（`job_store`）、文件→PostgreSQL 元数据迁移与 Repository、
  S3 兼容产物存储、鉴权/限流/追踪（`middleware`），不写底层清洗算法。
- `business_report/`：把 `BusinessAnswer` 渲染成 Markdown / HTML 报告。
- `schemas/`：`TaskSpec`（意图）、`ExecutionPlan/PlanSnapshot`（执行）、
  `OutputSpec`（产物）和 `JobConfig`（算子参数）等契约。
- `utils/`：把技术异常转成面向业务用户的中文提示。

## 前端分层边界

- `api/`：后端请求封装、TypeScript 类型、状态中文标签。
- `routes/`：页面级容器（上传、发现、计划审阅、计划确认、任务详情、结果看板、异常复核、澄清、设置）。
- `components/`：布局、上传、任务、报告、表格、状态组件。
- `hooks/`：任务提交、状态轮询、结果读取、澄清与计划确认交互。
- `styles/`：全局样式。

## 兼容说明

- Python 包名为 `data_agent`，`data-agent` CLI entrypoint 为 `data_agent.cli.main:app`。
- `from data_agent.api import app` 可用。
- 旧接口 `/api/v1/cleaning/*` 与新版 Job API `/api/v1/jobs/*` 同时可用。
