# Agent 评测与发布门禁

这套评测验证的是“用户最后拿到什么”，不是只验证函数是否返回。Golden Task 会走
生产规划、计划快照、执行和导出链路，再读取最终工作簿做断言。

## 当前基线

- 110 个中文用户任务，来自 15 个独立场景、至少 8 个业务域。
- 5 个核心场景各有 20 种中文表达，用来检查同一意图的语言鲁棒性。
- 51 个任务构成无模型确定性底线；59 个语义表达明确标为 `requires_llm`，无模型运行
  会显示为跳过，不能伪装成通过。
- 覆盖未要求操作、删除比例 1%/50%/99%、二层表头、合并单元格、隐藏行、文本数字、
  括号负数、多表合并、宽转长、交叉表、关联补齐和公式注入。

每个任务可检查 TaskSpec 动作、Capability、sheet 精确顺序、字段精确集合、行数、具体
单元格值、禁止出现的行或字段、图表、报告、公式安全和 PlanSnapshot 水合一致性。
`clarification_slots` 只统计 TaskSpec 中 `priority=high` 的阻塞槽位；medium 非阻塞问题
仍记录在报告的 `non_blocking_clarification_slots`，但不会被误判为无法交付。

## 本地命令

所有命令从仓库根目录执行，并使用仓库指定的虚拟环境：

```powershell
# 无网络、无模型的确定性底线；CI 每次运行
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py `
  --mode deterministic --cache-mode off --run-id local-deterministic

# 只运行必须由模型理解的表达；发布候选必须分别验证冷、热缓存
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py `
  --mode configured --cache-mode cold --requires-llm-only --run-id rc-llm-cold
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py `
  --mode configured --cache-mode warm --requires-llm-only --run-id rc-llm-warm

# 和一个已审核报告比较；已通过任务消失或转为失败都会使命令失败
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py `
  --mode deterministic --cache-mode off `
  --baseline data\evaluation\approved\replay_report.json

# 只改了 Golden 断言时，复用不可变工作簿离线重评分；不调用模型
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py `
  --rescore-report data\evaluation\source\replay_report.json `
  --run-id rc-rescored

# 修复单个任务后，把定点重跑替换进完整报告；不重复调用其余任务
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py `
  --merge-base-report data\evaluation\full\replay_report.json `
  --replace-report data\evaluation\target\replay_report.json `
  --run-id rc-merged
```

每次运行使用独立目录，不覆盖旧报告。报告记录 prompt、planner、registry/compiler、
Golden suite 和模型版本，任务级检查结果，P95 时延，以及 LLM 调用数、token 和延迟；
不保存 API Key。离线重评分必须通过运行时版本校验；定点合并还必须保证模型、缓存模式、
评测指纹完全一致，并记录被替换任务和每条 artifact 来源。

## 发布门禁

发布候选必须同时满足：

1. 确定性 Golden：51/51 通过，四项覆盖率均为 1.0；只能跳过声明为
   `requires_llm` 的任务。
2. 模型 Golden：所选 `requires_llm` 任务不得跳过，冷缓存和热缓存均 100% 通过；
   热缓存实际来源必须为 `llm_cached`。
3. 与上一份已审核的同模式报告比较时，不得删除已通过任务，不得出现指标下降。
4. 后端 pytest、Ruff、工作簿验收矩阵和前端 production build 全部通过。
5. 新增意图表达、Capability 或交付类型时，必须先增加 Golden/验收断言，不能只增加
   单元测试。

完整模型评测会产生外部调用费用，CI 默认只跑确定性底线；模型报告由发布流程保留和
审核。当前仓库不把某次模型输出提交为永久真值，避免模型或 prompt 版本变化后把陈旧
结果当成新基线。

供应商鉴权、欠费、限流或网络失败会记录为 LLM 调用失败；系统可以在普通任务中安全
回退，但发布门禁不得把这种回退计作模型通过。恢复供应商后必须重新取得完整报告或在
相同版本下用定点重跑替换失败任务，不能把确定性回退计作模型成功。

## 最近一次本地验收记录

- 当前确定性底线：51/51，四项覆盖率均为 1.0。
- 当前 v10 冷缓存门禁：59/59，四项覆盖率均为 1.0。原始 59 条
  任务全部实际执行，之后只修正了一个与产品原则冲突的 Golden 断言；重评分新增调用 0。
- 当前 v10 热缓存门禁：59/59，四项覆盖率均为 1.0，全部来源为 `llm_cached`。完整批次中唯一失败
  是 supplier v15 的阻塞问题，修复后该任务热缓存定点 1/1，通过版本校验后替换；合并
  新增调用 0，定点复验本身 3 次调用。
- 当前工程门禁：后端 609/609，Ruff 通过，工作簿验收 7/7，前端 production build
  通过。第三阶段发布门禁已闭环。

完整回放报告写入被 Git 忽略的 `data/evaluation/`。发布时应重新运行门禁并把报告作为
CI 或 GitHub Release 产物保存，不把历史运行产物提交到源码仓库。
