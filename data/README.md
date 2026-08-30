# Data Directory

`data/` 只放运行输入和运行输出，不放产品源码。

```text
data/
  input/       # 用户待处理数据包，可包含 Excel/CSV/JSON/Markdown/PDF/DOCX
  output/      # data-agent answer/discover/profile/recommend 的默认输出
  demo/        # scripts/run_end_to_end_demo.py 生成的端到端演示产物
  api_jobs/    # FastAPI 上传处理产生的任务目录
```

约定：

- `data/input/` 是用户或 demo 放入的原始数据包。
- `data/output/` 是一次性运行结果，可以删除后重新生成。
- `data/demo/` 是可复现 demo 结果，由脚本生成。
- `data/api_jobs/` 是 API 运行任务目录，可以按任务保留或定期清理。
