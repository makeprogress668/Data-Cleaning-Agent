#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "backend/.venv/bin/python" ]]; then
    PYTHON_BIN="backend/.venv/bin/python"
  elif [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

run_data_agent() {
  PYTHONPATH="$ROOT_DIR/backend/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -c \
    'import sys; from data_agent.cli.main import app; sys.argv[0] = "data-agent"; app()' "$@"
}

GOAL="${GOAL:-清洗订单，列出异常，生成一张处理状态饼图和简要报告}"
INPUT_DIR="${INPUT_DIR:-data/input}"
OUTPUT_DIR="${OUTPUT_DIR:-data/output/local_cli_demo}"

echo "==> 1. 生成本地样例输入数据"
"$PYTHON_BIN" scripts/create_sample_data.py

echo
echo "==> 2. 用户输入的中文目标"
echo "$GOAL"

echo
echo "==> 3. 调用 CLI 生成业务结果包"
run_data_agent answer "$INPUT_DIR" \
  --goal "$GOAL" \
  --output "$OUTPUT_DIR" \
  --include-audit

echo
echo "==> 4. 输出结果目录"
find "$OUTPUT_DIR" -maxdepth 2 -type f | sort

echo
echo "==> 5. final_result.xlsx 包含的 sheet"
"$PYTHON_BIN" - <<PY
from pathlib import Path
from openpyxl import load_workbook

path = Path("$OUTPUT_DIR") / "final_result.xlsx"
wb = load_workbook(path, read_only=True, data_only=True)
for sheet in wb.sheetnames:
    print(f"- {sheet}")
PY

echo
echo "==> 6. business_answer.md 摘要预览"
if [[ -f "$OUTPUT_DIR/business_answer.md" ]]; then
  "$PYTHON_BIN" - <<PY
from pathlib import Path

path = Path("$OUTPUT_DIR") / "business_answer.md"
lines = path.read_text(encoding="utf-8").splitlines()
for line in lines[:50]:
    print(line)
PY
else
  echo "当前目标未声明报告，因此未生成 business_answer.md"
fi

echo
echo "==> 运行完成"
echo "最终数据：$OUTPUT_DIR/final_result.xlsx（如目标要求列出异常，问题说明为其中一个 sheet）"
if [[ -f "$OUTPUT_DIR/business_answer.html" ]]; then
  echo "业务报告：$OUTPUT_DIR/business_answer.html"
fi
