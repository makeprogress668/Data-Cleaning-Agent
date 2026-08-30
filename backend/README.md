# Backend

后端的产品能力、契约和目录说明以根目录
[README.md](../README.md) 为准；API 细节见
[docs/API_CONTRACT.md](../docs/API_CONTRACT.md)。

## 安装

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,api]"
```

## 启动 API

```powershell
uvicorn data_agent.api:app --host 127.0.0.1 --port 8000 --reload
```

## 运行 CLI

以下命令均从 `backend/` 目录执行（PowerShell 续行符为反引号 `` ` ``）：

```powershell
data-agent discover ..\data\input --output ..\data\output

data-agent plan ..\data\input `
  --goal "清洗订单数据，输出可导入系统的数据" `
  --output ..\data\output\planning_review.xlsx `
  --job-output ..\configs\planned_cleaning_job.json

data-agent answer ..\data\input `
  --goal "清洗订单并列出异常" `
  --output ..\data\output

data-agent run ..\configs\planned_cleaning_job.json --verbose
```

普通 `answer` 任务只生成 `final_result.xlsx`；图表、报告、问题说明和审计仅在目标或参数明确要求时增加。

## 验证

```powershell
python -m ruff check src tests
python -m pytest tests -q

cd ..
backend\.venv\Scripts\python.exe scripts\run_golden_evaluation.py
```

Docker 和生产环境配置见根目录
[DEPLOYMENT.md](../DEPLOYMENT.md)。
