# Configs

这里用于放置可执行的 `JobConfig` 草案。

常见生成方式：

```bash
data-agent recommend-files data/input \
  --output data/output/cleaning_recommendations.xlsx \
  --job-output configs/recommended_cleaning_job.json
```

然后可以检查或执行：

```bash
data-agent inspect configs/recommended_cleaning_job.json
data-agent run configs/recommended_cleaning_job.json
```
