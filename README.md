<div align="center">

# 数据炼金师 · Data Cleaning Agent

**上传一堆乱七八糟的业务表格，说一句话，拿到能直接用的结果。**

不是生成一段处理代码让你自己跑，而是直接交付一个可复核、可导入、可追溯的结果文件。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#-快速开始)
[![FastAPI](https://img.shields.io/badge/FastAPI-async%20jobs-009688?logo=fastapi&logoColor=white)](#-三种使用方式)
[![React](https://img.shields.io/badge/React%2018-Vite%208-61DAFB?logo=react&logoColor=white)](#-三种使用方式)
[![Tests](https://img.shields.io/badge/tests-pytest%20%2B%20golden-2ea44f)](#-开发与测试)
[![Excel](https://img.shields.io/badge/Excel-VLOOKUP·SUMIF·透视·分列-217346?logo=microsoftexcel&logoColor=white)](#-它能做哪些-excel-的活)
[![LLM](https://img.shields.io/badge/LLM-可选·任意%20OpenAI%20兼容网关-8A2BE2)](#-启用-llm-理解层可选)

</div>

---

## 目录

- [🎯 它解决什么问题](#-它解决什么问题)
- [👥 适合谁用](#-适合谁用)
- [🔍 和其他做法的区别](#-和其他做法的区别)
- [✨ 核心特性](#-核心特性)
- [🧮 它能做哪些 Excel 的活](#-它能做哪些-excel-的活)
- [🚀 快速开始](#-快速开始)
- [🧭 三种使用方式](#-三种使用方式)
- [📤 上传数据要注意什么](#-上传数据要注意什么)
- [📦 你会得到什么](#-你会得到什么)
- [🔒 安全边界](#-安全边界)
- [🧩 架构](#-架构)
- [🧠 启用 LLM 理解层（可选）](#-启用-llm-理解层可选)
- [🔧 配置项](#-配置项)
- [🧪 开发与测试](#-开发与测试)
- [📊 Demo 与评测](#-demo-与评测)
- [❓ 常见问题](#-常见问题)
- [🚧 当前边界](#-当前边界)

---

## 🎯 它解决什么问题

业务同事手上有一批表：订单明细、客户档案、商品主数据，外加一份 Word 写的《导入规则说明》。要把它们并成一张能导进系统的表——这件事今天要么手工做半天，要么找人写脚本。

这个项目让你**只说目标**：

```powershell
data-agent answer .\data\input --goal "清洗订单数据，补齐客户和商品名称，列出不能使用的数据原因"
```

**输入**（3 个表 + 1 份规则文档）：

```text
orders.csv        order_id  customer_id  product_id  amount  status     email
                  O-1001    C001         P001         199.9  paid       buyer1@example.com
                  O-1002    C002         P002         -20.0  paid       buyer2@example.com
                  O-1003    C404         P003          80.0  cancelled  bad-email

customers.csv     C001 Acme Ltd / C002 Beta Co          （没有 C404）
products.csv      P001 / P002 / P003
cleaning_rules.md 「customer_id 必填」「amount 必须 >= 0」「status 允许值: paid, unpaid, refunded」…
```

**输出**（一个 Excel，两个 sheet）：

`处理结果` —— 可以直接用的数据，客户名、商品名已经从另外两张表补齐：

| order_id | customer_id | product_id | amount | status | email | customer_name | product_name |
| --- | --- | --- | --- | --- | --- | --- | --- |
| O-1001 | C001 | P001 | 199.9 | paid | buyer1@… | **Acme Ltd** | **Analytics Suite** |

`问题说明` —— 不能用的数据，以及**为什么**不能用（用业务话讲，不是报错码）：

| 处理状态 | 问题类型 | 建议处理 | 源行号 |
| --- | --- | --- | --- |
| 需处理 | amount 小于允许的最小值 | 人工确认后决定处理方式 | 1 |
| 需处理 | 关联数据未匹配；status 取值不在允许范围 | 确认映射关系或补充主数据 | 2 |

注意第二行：`C404` 在客户表里不存在、`cancelled` 不在规则允许的取值里——**这两条规则一条来自数据本身，一条来自你那份 Word 文档**，系统自己读出来的。

> 上面这个例子来自项目自带样例数据，跟着[快速开始](#-快速开始)可以原样复现。

---

## 👥 适合谁用

**如果你符合下面任意一条，这个工具是为你做的：**

- 手上经常有几张需要对齐、合并、清洗的业务表，但**不写 SQL / Python / VLOOKUP**
- 有一份写在 Word/PDF 里的字段规范或导入模板，希望**规则能被真正执行**，而不是靠人记
- 需要把数据导入 ERP / CRM / 内部系统，**导之前必须知道哪些记录会失败、为什么**
- 处理结果要能**交给别人复核**，而不是一个黑盒里出来的文件
- 数据敏感，**不能把整张表传给大模型**

**如果你更需要下面这些，它可能不合适：**

- 探索式分析、做图表看板（这个项目只按目标出图，不是 BI 工具）
- 连接数据仓库跑 SQL（它处理的是**文件**：Excel / CSV，不是数据库）
- 亿级数据的 ETL 流水线（当前以单机 pandas 内存计算为主）

---

## 🔍 和其他做法的区别

| | 直接问通用大模型 | 传统 ETL / 脚本 | **数据炼金师** |
| --- | :---: | :---: | :---: |
| 上手成本 | 低 | 需要开发 | 低（一句话） |
| 数据是否离开本地 | 整表上传 | 不离开 | **只传字段名/脱敏样本，可配为纯本地** |
| 结果可复现 | ❌ 每次不一样 | ✅ | ✅ **无 Key 时全程确定性引擎** |
| 会不会编造字段 | ❌ 常见 | — | ✅ **字段不存在直接拒绝执行** |
| 删行前是否确认 | ❌ | 看脚本怎么写 | ✅ **原话授权；高影响或无法估算时再确认** |
| 说明文档里的规则 | 要自己贴进去 | 要自己实现 | ✅ **自动抽取 PDF/Word/MD 里的规则** |
| 为什么这条数据不能用 | 解释不稳定 | 要自己写日志 | ✅ **每条都有业务话术的原因** |
| 人工复核能否改结果 | — | 要重跑 | ✅ **复核结论一键回写成新文件** |

核心差别一句话：**大模型只负责"读懂你要什么"，所有动数据的操作都由确定性代码执行，并且执行前要过校验、执行后可追溯。**

---

## ✨ 核心特性

### 1. 一句话直达，读不懂就问

用日常业务语言描述目标即可，不需要写处理步骤。目标信息不全时，系统**只问一个最关键的问题**（默认最多 3 轮），而不是瞎猜或抛一堆参数让你填。

### 2. 看得懂说明文档

上传的 `.md` / `.txt` / `.json` / `.pdf` / `.docx` 会被解析成校验规则——「必填」「取值范围」「格式」「导入字段清单」都会真正参与判定，并在结果里注明是哪条要求没过。

### 3. 跨表补齐，匹配不上会说话

自动识别主表、维表和关联键。匹配不上的记录不会静默丢弃，而是进复核清单并注明「关联数据未匹配」。

### 4. 交付物只有你要的东西

默认**只产出一个文件、一个 sheet**。报告、图表、异常清单、可导入视图、审计包——目标里提了才生成。写出前有 Output Linter 校验，没声明的内容会让任务失败，而不是先生成再藏起来。

### 5. 人工复核能改变结果

复核页面记录「确认可用 / 确认不采用」，完成后一键生成 `final_result_v2.xlsx`：确认可用的记录并回结果表，确认不采用的移出。**原始结果文件保留，源数据始终不改。**

### 6. 每条判断都能追溯

结果里的每个问题都有业务话术的原因和建议动作。开启审计后还能拿到字段级变更记录、规则命中明细和数据血缘。

### 7. 删数据要有你的原话为凭

判断「用户是不是要删行」不靠关键词表——那种做法漏一个说法就掉进错误的分支。这里的规则是：模型可以自由理解（「把重复的记录**清理掉**」不需要命中任何关键词），但它必须**引用你目标里的原话**，而校验只有一步、确定性、可审计：这段引用是不是真的出自你的目标。

```text
把重复的记录清理掉          → 引得出原话 → 执行
看看金额低于0的订单         → 「看看」不是删除 → 交付筛选视图，原表不动
统计每个客户的金额（模型顺手提议删负数行）→ 引不出原话 → 不删，也不拿它烦你
```

漏判的代价是多问一句，而不是删掉你没让删的数据。

### 8. 不配 Key 也能用

不配置大模型时，整条链路退回确定性规则引擎，功能完整、结果可复现。大模型只是让「读懂目标」这一步更准。

---

## 🧮 它能做哪些 Excel 的活

你在 Excel 里点来点去的那些操作，这里是一句话。**做完的表可以直接用，不需要再学一套语法。**

| 你想做的 | 说一句 | 得到 |
| --- | --- | --- |
| VLOOKUP 补齐 | 「把客户名称关联到订单明细」 | 多一列，匹配不上的留空并说明 |
| **带公式的 VLOOKUP** | 「**用 VLOOKUP** 把客户名称匹配过来」 | 单元格里是 `=VLOOKUP($B2,客户档案!$A:$B,2,FALSE)` —— 点开能看、能改、会重算 |
| 跨表 SUMIF | 「把订单明细的金额按客户汇总到客户档案」 | 主表多一列合计，行数不变，没订单的客户是 0 |
| 数据透视 | 「按城市统计金额合计」 | 一张汇总表 |
| **交叉表** | 「按城市和月份做**交叉表**」 | 行是城市、列是月份的矩阵 |
| 逆透视 | 「把月份列转成行」 | 12 个月份列变成一个月份列 |
| 分列 | 「把地址拆成省、市、区」 | 三个新列，分隔符自己看数据推 |
| 删除重复项 | 「把重复的记录清理掉」 | 去重，并告诉你删了几行 |
| 合并多个文件 | 「把 12 个月的销售合并成一张表」 | 一张表，每行记得来自哪个文件 |
| 按模板填表 | 上传空模板 + 「按模板填好数据」 | 模板的列名和顺序原样保留，填不上的留空 |

三个和 Excel 不一样的地方：

- **会计写法认得** —— `1,234`、`(567)`、`¥(567.00)`、`1.2万` 都能正确参与计算，而 `pd.to_numeric` 会把它们全变成 0
- **公式就是验证** —— 要求带公式时，源表会一起放进工作簿，点开单元格看到的和你自己写的一模一样，不需要额外的核对表
- **不动你没提的数据** —— 明细里 `1,234` 还是 `1,234`，只有汇总用了解析后的数字

---

## 🚀 快速开始

**前置条件**

| | 要求 |
| --- | --- |
| Python | ≥ 3.10（推荐 3.12） |
| Node.js | ≥ 20.19 或 ≥ 22.12（只有用 Web 控制台才需要） |
| 大模型 | **可选**，不配也能跑完整流程 |

**三步跑通**

```powershell
# 1. 安装后端
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,api]"

# 2. 生成样例数据（订单 / 客户 / 商品 + 规则文档 + 导入模板）
cd ..
backend\.venv\Scripts\python.exe scripts\create_sample_data.py

# 3. 跑一次完整处理
cd backend
data-agent answer ..\data\input `
  --goal "清洗订单数据，补齐客户和商品名称，列出不能使用的数据原因"
```

产物写在 `data/output/`：`final_result.xlsx`（`.data-agent-work/` 是中间目录，可忽略）。

> **PowerShell 提示**：续行符是反引号 `` ` `` 不是反斜杠。若 `Activate.ps1` 被执行策略拦截，可跳过激活直接用 `backend\.venv\Scripts\python.exe -m ...`。
>
> **macOS / Linux**：`python3 -m venv .venv`、`source .venv/bin/activate`、`backend/.venv/bin/python`，续行符 `\`。

**接着试试这些目标**，感受产物如何随目标变化：

```text
--goal "清洗订单数据"                              → 1 个 sheet，别的都不生成
--goal "…，列出不能使用的数据原因"                  → 多出「问题说明」sheet
--goal "…，输出可导入系统的数据"                    → 多出「可导入数据」sheet
--goal "…，生成一张处理状态饼图和简要报告"           → 多出 charts/ 和 business_answer.html
--goal "…，过滤 amount < 0 的记录"                → 先估算；移除达到 25% 时加 --yes 才执行
--goal "…，过滤掉金额小于 0 的记录"                → 条件没落到具体字段，会先反问你要按什么过滤
```

---

## 🧭 三种使用方式

| 入口 | 怎么用 | 适合 |
| --- | --- | --- |
| **Web 控制台** | 上传文件 → 填目标 → 看进度 → 预览下载 → 复核回写 | 业务用户、演示 |
| **CLI** | `data-agent answer <目录> --goal "…"` | 本地处理、批处理、验收 |
| **Job API** | `POST /api/v1/jobs` 上传 + 轮询状态 | 接入业务系统、自建前端 |

三个入口共用同一套规划与执行代码，**同样的输入和目标产出完全相同的工作簿和质量评分**。

<details>
<summary><b>启动 Web 控制台</b></summary>

```powershell
# 终端 1：后端
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn data_agent.api:app --host 127.0.0.1 --port 8000 --reload

# 终端 2：前端
cd apps\web
npm install
npm run dev        # http://127.0.0.1:5173，默认代理后端 8000
```

界面结构：一级入口是「新建任务 / 当前任务 / 设置」；当前任务内有「任务总览 / 数据概览 / 处理方案 / 处理结果 / 异常复核」，可切换历史任务。所有页面由 URL 里的 `job_id` 锁定。

> 本地开发绑定 `127.0.0.1`。`0.0.0.0` 会暴露到局域网且常被 Windows 防火墙拦截；容器内监听由 `backend/Dockerfile` 负责。

</details>

<details>
<summary><b>CLI 全部命令</b></summary>

```powershell
# 完整处理并交付（常用参数：--output --include-audit --yes --debug）
data-agent answer ..\data\input --goal "清洗订单数据，列出异常"

# 只做数据发现和画像，不处理
data-agent discover ..\data\input --output ..\data\output

# 生成可审阅的处理方案，不执行
data-agent plan ..\data\input --goal "清洗订单数据" `
  --output ..\data\output\planning_review.xlsx `
  --job-output ..\configs\planned_cleaning_job.json

# 用已有 JobConfig 直接执行
data-agent run ..\configs\planned_cleaning_job.json --verbose
```

</details>

<details>
<summary><b>Job API 端点</b></summary>

异步任务：`POST` 立即返回 `job_id`，轮询状态，支持澄清与计划确认的暂停/恢复。

```text
POST   /api/v1/jobs                              # 创建任务（202 Accepted）
GET    /api/v1/jobs                              # 最近任务列表
GET    /api/v1/jobs/{job_id}                     # 完整任务文档
GET    /api/v1/jobs/{job_id}/status              # 状态轮询
GET    /api/v1/jobs/{job_id}/session             # TaskSpec、计划版本与会话轮次
GET    /api/v1/jobs/{job_id}/business-answer     # 一页式业务结果
GET    /api/v1/jobs/{job_id}/discovery           # 数据发现视图
GET    /api/v1/jobs/{job_id}/plan                # 处理计划视图
GET    /api/v1/jobs/{job_id}/events              # 真实执行事件与 LLM 用量
GET    /api/v1/jobs/{job_id}/files               # 产物清单
GET    /api/v1/jobs/{job_id}/files/{file_name}   # 产物下载
GET    /api/v1/jobs/{job_id}/review              # 可分页复核记录与已保存决策
POST   /api/v1/jobs/{job_id}/review              # 复核回写
POST   /api/v1/jobs/{job_id}/rematerialize       # 按复核结论重新生成结果文件
GET    /api/v1/jobs/{job_id}/versions            # 结果版本图
POST   /api/v1/jobs/{job_id}/versions/{v}/select # 选择历史版本
POST   /api/v1/jobs/{job_id}/versions/undo       # 撤销到父版本
POST   /api/v1/jobs/{job_id}/versions/{v}/branch # 从历史版本创建分支
GET    /api/v1/jobs/{job_id}/clarification       # 澄清问题
POST   /api/v1/jobs/{job_id}/clarification       # 提交答案并恢复执行
GET    /api/v1/jobs/{job_id}/plan-confirmation   # 执行前计划摘要
POST   /api/v1/jobs/{job_id}/plan-confirmation   # 确认计划并开始执行
DELETE /api/v1/jobs/{job_id}                     # 取消并删除任务
GET/POST /api/v1/recipes                         # 保存和复用已验收任务定义
GET/POST /api/v1/semantic-models                 # 业务指标与关系语义层
GET    /api/v1/connectors                        # Connector SDK 描述与配置 schema
POST   /api/v1/connectors/jobs                   # 从 connector 数据源创建任务
GET    /api/v1/config                            # 服务、理解层、数据保护和上传设置
GET    /health                                   # 健康检查
```

兼容旧的同步接口：`POST /api/v1/cleaning/process`、`GET /api/v1/cleaning/jobs/{job_id}`、`GET /api/v1/cleaning/jobs/{job_id}/result.xlsx`。

接口契约详见 [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md)。

</details>

---

## 📤 上传数据要注意什么

| 项 | 说明 |
| --- | --- |
| 支持的数据表 | `.xlsx` / `.xlsm` / `.xls` / `.csv`，Excel 的**每个 sheet** 成为一张独立的表 |
| 支持的说明文档 | `.md` / `.markdown` / `.txt` / `.json` / `.pdf` / `.docx` / `.doc` |
| 上传上限 | 单文件 100 MB，单次最多 50 个文件（均可配置） |
| **文件名很重要** | 文件名（去扩展名）就是表名，会参与主表定位。中文完整保留，`订单明细.xlsx` 比 `sheet1.xlsx` 更容易被目标命中 |
| 多 sheet 命名 | 单 sheet 用文件名；多 sheet 用 `文件名__sheet名` |
| 说明文档怎么被用 | 必填、取值范围、格式、导入字段等要求会被抽成校验规则，和你的目标一起决定哪些记录可用 |

目标直接写业务诉求即可，不用写技术步骤。例如「清洗订单明细，补齐客户名称，列出不能使用的数据原因」。

> 目标信息不足时系统会先停下来问一个问题（Web 显示澄清页，**不是卡住**），回答后继续。
> 过滤、去重必须能引用目标原话；预计移除达到 25%、影响无法可靠估算或使用模糊匹配时，Web 会停在计划确认页，CLI 需要加 `--yes`。

---

## 📦 你会得到什么

三个入口的产物统一是 `final_result.xlsx`，内容**完全由目标决定**。普通清洗只有一个文件、一个 sheet：

```text
data/output/
  final_result.xlsx           # 仅「处理结果」sheet
```

目标里明确要求后才增加：

| 目标或参数 | 增加的产物 |
| --- | --- |
| 要求异常 / 复核 | `final_result.xlsx` 增加「问题说明」sheet |
| 要求可导入系统的数据 | `final_result.xlsx` 增加「可导入数据」sheet |
| 完成复核后调用回写 | `final_result_v2.xlsx`（应用「确认可用 / 不采用」判断） |
| 要求具体图表 | `charts/` 中只生成声明的那几张 |
| 要求报告 | `business_answer.html` / `.md` |
| `--include-audit` | 独立 `audit_package/`（清洗日志、血缘、字段变更、规则命中、扣分明细） |

> Output Linter 在写出前校验 sheet、图表、文件和字段来源。未被声明的内容会让任务**失败**，而不是先生成再隐藏——这是「零冗余交付」的执行保证，不是口号。

---

## 🔒 安全边界

这部分是本项目和「让大模型直接处理数据」最本质的区别。

**1. 大模型碰不到数据的写入路径**

模型只输出候选 JSON（理解结果、计划草案、改进建议），所有读取、匹配、计算、导出都由确定性代码执行。草案要通过字段存在性、配置 schema、能力白名单、动作覆盖、产物边界五道校验才会被采纳，任何一道不过就退回确定性方案。

**2. 模型看到多少数据由你决定**

`DATA_AGENT_LLM_DATA_ACCESS_MODE` 三档：

| 取值 | 模型能看到 |
| --- | --- |
| `metadata_only` | 只有表名和字段名，**不读任何单元格** |
| `masked_samples`（默认） | 字段名 + 脱敏样本 |
| `trusted_samples` | 字段名 + 少量真实样本（仅限受信任环境） |

**3. 授权与风险确认分层处理**

过滤和去重首先必须有可引用的用户原话；没要求的动作不会因为点了确认就混入计划。在已经授权的前提下，预计移除主表记录达到 **25%**、影响无法可靠估算，或关联使用**模糊匹配**时才升级复核。三个入口使用同一套判定：Web 停在计划确认页并展示原因，CLI 需要 `--yes`，同步接口继续用兼容字段 `confirm_destructive=true`。每个步骤的 `confirmation_reasons` 和影响证据都会进入 PlanSnapshot。

**4. 源数据永不被修改**

所有处理在内存副本上进行，输出到新文件。人工复核也只生成新版本文件，不回写原表。

**5. 确认的是同一份计划和同一批输入**

计划确认页会回传 `plan_id + plan_hash`；后端在原子状态转换和实际执行前分别校验。
TaskSpec、输入文件内容、执行步骤和交付契约任何一项变化，旧页面都不能确认新计划。

**6. 不可信单元格不会变成可执行公式**

上传文件里以 `= / + / - / @` 等字符开头的文本仍按原值交付，但在 XLSX 中强制标记为
普通文本。只有用户明确要求公式、且由确定性执行器生成的公式列才会成为可执行公式。

**7. 执行过程可观测**

每个步骤的真实耗时和进出行数被记录下来（未执行的步骤明确标为 skipped，不会伪装成成功），加上 prompt / 模型 / 能力注册表的版本指纹和 LLM token 用量，都能从 `/events` 取到。

---

## 🧩 架构

```text
上传文件 ──▶ 理解数据 ──▶ 理解目标 ──▶ 生成计划 ──▶ 确定执行 ──▶ 反思自检 ──▶ 交付产物
 (Discover)   (Profile)   (Understand)   (Plan)      (Execute)    (Reflect)    (Deliver)
```

**两层，一条明确的执行边界：**

- **Agent 层（`agent/`）**：理解目标、起草可审阅计划、执行后反思。按策略读取元数据或脱敏样本，只产出候选 JSON，**不直接执行或修改数据**。
- **确定性底座（`pipelines/` + `tools/`）**：用代码执行读取、画像、lookup、公式、规则校验、导出和审计。

**核心契约链路**：

```text
TaskSpec（用户意图）
  └─▶ JobConfig（可执行配置）
        └─▶ ExecutionPlan（可审阅的步骤 DAG）
              └─▶ PlanSnapshot（确认后不可变的快照）
                    └─▶ OutputSpec（允许交付什么）
```

用户确认的是 `PlanSnapshot`，执行的也是它——确认后不会再重新规划，所以「你看到的」和「实际跑的」是同一件事。

<details>
<summary><b>目录结构</b></summary>

```text
data-cleaning-agent/
  backend/                    # Python / FastAPI / CLI 后端
    src/data_agent/
      agent/                  # 理解层：LLM 客户端、目标理解、planner、反思闭环、prompts、记忆
      capabilities/           # Capability Registry、能力映射、ExecutionPlan 编译
      api/                    # FastAPI app、异步 Job API、任务管理、状态存储、安全中间件
      cli/                    # data-agent CLI
      planning/               # 目标解析与能力推断
      services/               # CLI/API 共用应用服务（规划执行 / 业务结论 / 交付 / 复核回写）
      pipelines/              # 确定性执行管线（runner）
      tools/                  # 确定性能力库：读取、画像、lookup、公式、脏数据、规则、评分、图表、审计、导出
      business_report/        # Markdown / HTML 业务报告渲染
      observability/          # 执行事件记录与 LLM 用量采集
      evaluation/             # Golden Task 离线回放与回归指标
      schemas/                # TaskSpec、ExecutionPlan、PlanSnapshot、OutputSpec、JobConfig
      utils/                  # 共享小工具与技术异常转中文友好提示
    tests/
  apps/web/                   # React 18 / TypeScript / Vite 前端控制台
  docs/                       # 项目结构、API 契约、前后端架构、验收文档
  data/                       # input / output / demo / api_jobs，本地运行数据
  configs/                    # JobConfig 示例和生成结果
  scripts/                    # 样例数据、端到端 demo、Golden 评测
  examples/                   # 兼容样例
```

更详细的分层说明见 [`docs/PROJECT_STRUCTURE.md`](docs/PROJECT_STRUCTURE.md) 和 [`docs/BACKEND_ARCHITECTURE.md`](docs/BACKEND_ARCHITECTURE.md)。

</details>

---

## 🧠 启用 LLM 理解层（可选）

不配置时完全走确定性引擎，功能完整。配置后，模型负责**读懂目标并起草规划**，确定性引擎仍然校验草案并执行全部数据处理。模型不可用时自动回退，不会中断任务。

```bash
cp .env.example .env
```

```ini
# 兼容任意 OpenAI 协议网关（公有云、内网代理、本地部署皆可）
DATA_AGENT_LLM_API_KEY=sk-...
DATA_AGENT_LLM_BASE_URL=https://your-gateway/v1    # 缺省 https://api.openai.com/v1
DATA_AGENT_LLM_MODEL=gpt-4o-mini
```

> `.env` 放在**仓库根目录**。设 `DATA_AGENT_LLM_ENABLED=0` 可保留密钥但强制走确定性路径，方便做 A/B 对照。

**怎么确认理解层真的生效了？** 打开 Web 控制台的「设置」页，看「目标理解」卡片——显示模型名说明已启用，显示「规则引擎」说明没读到 Key。后端日志也会在启动后打印一次：

```text
INFO data_agent.agent.llm_client 理解层已启用：https://your-gateway/v1 / gpt-4o-mini
```

---

## 🔧 配置项

完整清单见 [`.env.example`](.env.example)，常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_AGENT_LLM_API_KEY` | 空 | 配置后启用理解层；为空则纯确定性引擎 |
| `DATA_AGENT_LLM_BASE_URL` / `_MODEL` | OpenAI | 网关地址与模型 |
| `DATA_AGENT_LLM_ENABLED` | 1 | 设 0 可保留密钥但强制走确定性路径 |
| `DATA_AGENT_LLM_DATA_ACCESS_MODE` | `masked_samples` | 模型能看到多少数据，见[安全边界](#-安全边界) |
| `DATA_AGENT_UNDERSTANDING_CACHE_BACKEND` | 跟随元数据后端 | 本地使用文件，生产 PostgreSQL 多 worker 共享理解缓存 |
| `DATA_AGENT_UNDERSTANDING_CACHE_TTL_SECONDS` | 2592000 | 理解缓存最长复用 30 天 |
| `DATA_AGENT_TENANT_ID` | `default` | 本地或旧单 Key 模式的默认租户 |
| `DATA_AGENT_TENANT_API_KEYS_JSON` | 空 | 多租户 API Key 与角色映射；设置后优先于单 Key |
| `DATA_AGENT_OIDC_ISSUER` / `_AUDIENCE` / `_JWKS_URL` | 空 | 多终端用户 OIDC/JWT；启用后以 tenant/role claim 进入隔离与 RBAC |
| `DATA_AGENT_CLARIFY_ENABLED` | 1 | 受控多轮澄清 |
| `DATA_AGENT_MAX_CLARIFICATION_ROUNDS` | 3 | 单任务最多澄清轮数（1–10） |
| `DATA_AGENT_CONFIRM_ENABLED` | 0 | 让**所有** answer 任务执行前确认；分层策略判定需复核的计划不受此开关影响 |
| `DATA_AGENT_REFLECTION_ENABLED` / `_MAX_ROUNDS` / `_TARGET_SCORE` | 1 / 1 / 90 | 有界反思闭环 |
| `DATA_AGENT_MAX_UPLOAD_BYTES` / `_FILES` | 100MB / 50 | 上传限制 |
| `DATA_AGENT_MAX_ACTIVE_JOBS_PER_TENANT` | 0（compose 为 4） | 每租户活跃任务配额 |
| `DATA_AGENT_MAX_BATCH_UPLOAD_BYTES` | 500MB | 单批所有上传文件的合计上限 |
| `DATA_AGENT_ENV` | 空 | 生产设为 `production`：缺 API Key 和 OIDC 时启动失败 |
| `DATA_AGENT_API_KEY` | 空 | 单 Key/旧部署使用；配置 OIDC 时可留空 |
| `DATA_AGENT_RATE_LIMIT_PER_MINUTE` | 600 | 单 IP 限流，≤0 关闭 |
| `DATA_AGENT_DUCKDB_CSV_MIN_BYTES` | 5MB | 超过该大小的 CSV 走 DuckDB 加速解析 |
| `DATA_AGENT_DUCKDB_MIN_ROWS` | 100000 | 等价性安全的过滤/聚合/确定性关联达到该规模后下推 DuckDB |
| `DATA_AGENT_METADATA_BACKEND` | `file` | 生产 compose 使用 PostgreSQL |
| `DATA_AGENT_JOB_QUEUE_BACKEND` | `thread` | 生产 compose 使用 Redis/ARQ |
| `DATA_AGENT_ARTIFACT_BACKEND` | `local` | 生产 compose 使用 S3 兼容对象存储 |
| `DATA_AGENT_JOB_SLA_SECONDS` | 3600 | 状态中计算端到端 SLA 是否超时 |

**Docker 部署**：

```bash
docker compose up --build      # Nginx 托管前端静态资源并反代 API
```

生产化配置（环境变量、反代、鉴权）详见 [`DEPLOYMENT.md`](DEPLOYMENT.md)。
多租户 Key、角色、存储边界、配额及内置 Web 的单租户限制见
[`docs/MULTI_TENANCY.md`](docs/MULTI_TENANCY.md)。

---

## 🧪 开发与测试

```powershell
# 后端
cd backend
.\.venv\Scripts\Activate.ps1
python -m ruff check src tests ..\scripts
python -m pytest tests -q

# 前端
cd apps\web
npm install
npm run build                      # tsc -b && vite build
```

**验收矩阵** —— 单元测试查函数，这个查**用户拿到的工作簿**：

```powershell
backend\.venv\Scripts\python.exe scripts\make_acceptance_data.py data\acceptance
backend\.venv\Scripts\python.exe scripts\run_acceptance_matrix.py scripts\acceptance_cases.json --no-llm
```

每条用例的期望值是先在纸上算出来、再写进 [`scripts/acceptance_cases.json`](scripts/acceptance_cases.json) 的：

```json
{ "goal": "按城市统计金额合计",
  "expect": { "rows_unchanged": true,
              "summary_contains": [{ "城市": "北京", "金额合计": 3234 }] } }
```

它存在的理由很具体：曾经 428 个单元测试全绿，而「按城市统计金额合计」交出过一张**全是 0** 的汇总表——金额列是文本 `1,234`，`sum()` 把它当成了 0。**看起来完全正常的错误答案，只有这一层拦得住。**CI 会跑不需要模型的那部分。

---

## 📊 Demo 与评测

```powershell
# 本地 CLI 快速演示（Windows；macOS / Linux 用同名 .sh）
powershell -ExecutionPolicy Bypass -File scripts\run_local_cli_demo.ps1

# 四个业务域端到端 demo（订单、供应商、资产、非结构化规则）+ 验收汇总
backend\.venv\Scripts\python.exe scripts\run_end_to_end_demo.py

# 110 条跨业务域 Golden Task（确定性底线；模型表达明确显示为跳过）
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py --mode deterministic --cache-mode off
```

产物写入 `data/demo/` 与 `data/evaluation/<run-id>/replay_report.json`。
模型冷/热缓存评测、基线比较和发布标准见 [`docs/EVALUATION.md`](docs/EVALUATION.md)。
四阶段整改的实际完成度与剩余验收见
[`docs/IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md)。
高频 Skill 的场景、实现逻辑和复用方法见
[`docs/SKILLS_GUIDE.md`](docs/SKILLS_GUIDE.md)。

---

## ❓ 常见问题

<details>
<summary><b>必须配置大模型 API Key 吗？</b></summary>

不需要。不配置时整条链路走确定性规则引擎，清洗、匹配、规则校验、异常识别、交付全都正常。大模型只让「读懂目标」这一步更准——比如你说「把作废的单子挑出来」，规则引擎可能匹配不到，模型能理解。

</details>

<details>
<summary><b>我的数据会被上传到大模型吗？</b></summary>

默认只传**字段名和脱敏样本**，不传整表。设 `DATA_AGENT_LLM_DATA_ACCESS_MODE=metadata_only` 则连单元格都不读，只给表名和字段名。完全不配置 Key 就是纯本地处理。

</details>

<details>
<summary><b>它会不会把我的数据改坏？</b></summary>

源文件永远不被修改，所有处理在内存副本上做、输出到新文件。过滤、去重必须来自你的明确要求；预计移除达到 25% 或无法可靠估算时会强制复核，并告诉你原因和预计影响。

</details>

<details>
<summary><b>任务卡在「等待澄清」是出错了吗？</b></summary>

不是。目标信息不足时系统会问一个最关键的问题，这是设计行为。Web 会显示澄清页，回答后自动继续。不想被打断可设 `DATA_AGENT_CLARIFY_ENABLED=0`。

</details>

<details>
<summary><b>复核完了，怎么让结果文件反映我的判断？</b></summary>

在异常复核页逐条标记「确认可用 / 确认不采用」，然后点「按复核结论生成结果」，会产出 `final_result_v2.xlsx`。原始结果文件保留不变，可以对照。

</details>

<details>
<summary><b>为什么我的结果里字段比原表少？</b></summary>

交付表会自动去掉内部技术列（`_` 开头、`*_std` 标准化派生列、追溯列等）。如果是业务字段缺失，通常是因为目标没提到、或来源表里没有——处理方案页能看到实际执行了哪些步骤。

</details>

<details>
<summary><b>能处理多大的数据？</b></summary>

单文件上限 100 MB（可配）。大 CSV 走 DuckDB 加速解析；常用过滤和数值语义安全的
分组聚合达到 10 万行后也会下推 DuckDB，并在执行事件里记录实际引擎。其他清洗仍以
pandas 内存计算为主，所以更大规模仍需按输入宽度配置 worker 内存。

</details>

---

## 🚧 当前边界

**已经具备**

- 通用业务数据发现、清洗、跨表匹配、异常识别、质量评分、图表分析和目标驱动交付
- 从说明文档、JSON Schema、PDF、DOCX 和导入模板中抽取规则
- LLM 优先、规则兜底的目标理解；受控多轮澄清、不可变计划确认、有界反思闭环
- Capability Registry、ExecutionPlan、OutputSpec 与 Output Linter 接入全部主链路
- 人工复核回写、结果版本选择、撤销与分支；成功任务可保存为 Recipe
- 跨业务域 Golden Task 回放、结果值级不变量、版本指纹、LLM 用量与真实执行事件
- 异步 Job API + 同步兼容接口；API Key 鉴权、限流、结构化日志、任务 TTL 清理
- 本地原子 JSON / 生产 PostgreSQL 双元数据后端，事务 CAS、任务历史和旧数据幂等迁移
- Redis/ARQ 持久队列、PostgreSQL outbox、跨进程领取/恢复与任务 SLA 状态
- 本地目录 / S3 兼容双产物后端；生产 API 与 worker 不依赖共享文件卷
- DuckDB 大 CSV 读取与等价性安全的过滤/聚合/确定性关联下推，实际引擎进入执行事件
- 租户级业务语义层和 Connector SDK；OIDC/JWT 与 API Key 双认证路径
- Excel 常用操作：VLOOKUP（可输出真公式）、跨表 SUMIF、透视与交叉表、逆透视、分列、去重、多文件合并、按模板填表
- 破坏性操作的引用授权、澄清做成选择题并记住用户偏好、理解结果可复现
- CLI、Web 控制台、Docker 部署、端到端 demo 与验收矩阵

**仍在演进**

- 扫描版 PDF 和旧版 `.doc` 暂不做 OCR / 二进制解析
- 模糊关联、复杂画像和 Excel 专有语义仍以 pandas 为主；确定性关联已可下推 DuckDB
- 反思以内部质量评分为排序依据，引入外部 ground-truth 是后续方向
- 复核支持回写生成新版本，但尚不支持在界面上直接修改字段值
- 多 API 副本的 HTTP 限流仍是进程级近似值，公网部署应由统一网关实施全局限流
- 仓库提供 OIDC/JWT 验证与 claim 映射，但登录跳转、MFA 和用户生命周期由外部 IdP/网关负责
- 目标理解的确定性兜底仍是词表驱动，配了模型时才认得全部口语说法

---

<div align="center">

**用一句话，把脏乱数据炼成能直接交付的结果。**

[快速开始](#-快速开始) · [三种使用方式](#-三种使用方式) · [安全边界](#-安全边界) · [常见问题](#-常见问题)

</div>
