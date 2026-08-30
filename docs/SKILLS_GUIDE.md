# 高频 Skill 设计与复用指南

仓库中的 `skills/` 是面向 Codex/其他 Agent 的可复用操作包。它们把高频业务目标、
澄清条件、调用方式和交付验收固化下来，但不包含新的数据处理算法。

## 当前目录

| Skill | 适用场景 | 主要 Golden 依据 |
| --- | --- | --- |
| `data-agent-enrich-tables` | 关联表补字段、VLOOKUP | `order_customer_enrichment` |
| `data-agent-consolidate-files` | 月度/批次同结构文件纵向合并 | `monthly_union` |
| `data-agent-summarize-metrics` | 汇总、趋势、交叉表、图表报告 | 4 个 aggregation 场景 |
| `data-agent-quality-review` | 异常检查、标记、问题说明、复核 | supplier/laboratory/review 场景 |
| `data-agent-standardize-fields` | 明确字段的空格、字符和值标准化 | normalization/security 场景 |

## 实现逻辑

一次 Skill 调用仍然走现有唯一执行链：

```text
用户请求
  -> Skill 判断必要字段和歧义
  -> 生成明确的中文业务目标
  -> data-agent plan / answer
  -> TaskSpec -> JobConfig -> ExecutionPlan
  -> Capability Registry -> Runner -> Output Linter
  -> 检查最终工作簿
```

因此 Skill 不会绕过确认策略，也不会直接调用 pandas 修改数据。Skill 的主要价值是把
“哪些信息必须问清楚”和“最终应该检查什么”固定下来，减少每次重新发明提示词。

## 如何修改

每个 Skill 只有两层内容：

- `SKILL.md`：触发范围、必须遵守的边界和操作流程；
- `references/goal-patterns.md`：业务目标模板和可观察验收条件。

常见修改方式：

1. 想改变何时触发：修改 YAML frontmatter 的 `description`，保持场景互斥。
2. 想适配行业术语：修改 `goal-patterns.md`，替换表名、字段和业务说法。
3. 想增加新的检查：在 acceptance checks 中增加对最终工作簿的业务断言。
4. 想增加新的数据操作：不要在 Skill 里写 pandas 脚本；先把操作加入 TaskSpec、
   Capability Registry 和 Runner，再让 Skill 引用它。

不要把客户数据、API Key、固定本机绝对路径或真实租户 ID 写进 Skill。

## 如何复用

- 在本仓库中，Agent 可以按路径读取对应 `SKILL.md`。
- 需要安装到 Codex 时，将单个 Skill 目录复制到用户的 Codex skills 目录；目录名保持
  与 frontmatter 的 `name` 一致。
- 分发给其他项目时，可以单独打包某个 Skill；它依赖的是稳定的 `data-agent` CLI，
  不应依赖本仓库内部 Python 函数。

这些 Skill 当前是 Agent 操作层，不是 Web 产品里的原生 Skill 商店。后续若要让业务用户
在 Web 中选择 Skill，应新增版本化 Skill Manifest/Registry，使其引用 Recipe、Semantic
Model 和 Connector；最终仍编译到同一 TaskSpec/ExecutionPlan 链路。

## 验证

修改 Skill 后至少执行：

```powershell
backend\.venv\Scripts\python.exe path\to\skill-creator\scripts\quick_validate.py skills\{skill-name}
```

其中 `path\to\skill-creator` 替换为当前 Agent 环境中 Skill Creator 的安装目录，不要把
某台机器的用户目录提交到仓库。

结构校验只证明 Skill 可加载。业务修改还需要运行对应 Golden task 或工作簿验收，不能
只检查命令退出码。
