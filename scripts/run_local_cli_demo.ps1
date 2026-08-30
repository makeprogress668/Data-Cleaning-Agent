# Windows 版本地 CLI 演示，与 run_local_cli_demo.sh 等价。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\run_local_cli_demo.ps1
$ErrorActionPreference = "Stop"

# 控制台按 UTF-8 输出，否则中文目标和表名会显示为乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

if (-not $env:PYTHON_BIN) {
    if (Test-Path "backend\.venv\Scripts\python.exe") {
        $PythonBin = "backend\.venv\Scripts\python.exe"
    } elseif (Test-Path ".venv\Scripts\python.exe") {
        $PythonBin = ".venv\Scripts\python.exe"
    } else {
        $PythonBin = "python"
    }
} else {
    $PythonBin = $env:PYTHON_BIN
}

# 装好 venv 后优先用控制台脚本；否则回退到 -m 调用。
# 不用 `python -c "..."`：PowerShell 5.1 向原生 exe 传参时会吃掉内嵌的双引号。
$DataAgentExe = Join-Path $RootDir "backend\.venv\Scripts\data-agent.exe"

function Invoke-DataAgent {
    if (Test-Path $DataAgentExe) {
        & $DataAgentExe @args
    } else {
        # Windows 的 PYTHONPATH 分隔符是 ";"，不是 ":"。
        $previous = $env:PYTHONPATH
        if ($previous) {
            $env:PYTHONPATH = "$RootDir\backend\src;$previous"
        } else {
            $env:PYTHONPATH = "$RootDir\backend\src"
        }
        try {
            & $PythonBin -m data_agent.cli.main @args
        } finally {
            $env:PYTHONPATH = $previous
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "data-agent 退出码 $LASTEXITCODE" }
}

if (-not $env:GOAL) { $Goal = "清洗订单，列出异常，生成一张处理状态饼图和简要报告" } else { $Goal = $env:GOAL }
if (-not $env:INPUT_DIR) { $InputDir = "data\input" } else { $InputDir = $env:INPUT_DIR }
if (-not $env:OUTPUT_DIR) { $OutputDir = "data\output\local_cli_demo" } else { $OutputDir = $env:OUTPUT_DIR }

Write-Output "==> 1. 生成本地样例输入数据"
& $PythonBin scripts\create_sample_data.py
if ($LASTEXITCODE -ne 0) { throw "生成样例数据失败" }

Write-Output ""
Write-Output "==> 2. 用户输入的中文目标"
Write-Output $Goal

Write-Output ""
Write-Output "==> 3. 调用 CLI 生成业务结果包"
Invoke-DataAgent answer $InputDir --goal $Goal --output $OutputDir --include-audit

Write-Output ""
Write-Output "==> 4. 输出结果目录"
Get-ChildItem -Path $OutputDir -Recurse -Depth 1 -File | Sort-Object FullName | ForEach-Object { $_.FullName }

Write-Output ""
Write-Output "==> 5. final_result.xlsx 包含的 sheet"
# 走临时脚本而不是 python -c：PowerShell 5.1 会破坏内嵌引号的参数。
$sheetScript = Join-Path ([System.IO.Path]::GetTempPath()) "list_sheets_$PID.py"
@"
import sys
from pathlib import Path
from openpyxl import load_workbook

path = Path(sys.argv[1]) / 'final_result.xlsx'
wb = load_workbook(path, read_only=True, data_only=True)
for sheet in wb.sheetnames:
    print('- ' + sheet)
"@ | Set-Content -Path $sheetScript -Encoding UTF8
try {
    & $PythonBin $sheetScript $OutputDir
} finally {
    Remove-Item $sheetScript -Force -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output "==> 6. business_answer.md 摘要预览"
if (Test-Path "$OutputDir\business_answer.md") {
    Get-Content -Path "$OutputDir\business_answer.md" -Encoding UTF8 -TotalCount 50
} else {
    Write-Output "当前目标未声明报告，因此未生成 business_answer.md"
}

Write-Output ""
Write-Output "==> 运行完成"
Write-Output "最终数据：$OutputDir\final_result.xlsx（如目标要求列出异常，问题说明为其中一个 sheet）"
if (Test-Path "$OutputDir\business_answer.html") {
    Write-Output "业务报告：$OutputDir\business_answer.html"
}
