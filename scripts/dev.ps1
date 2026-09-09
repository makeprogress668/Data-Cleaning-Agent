# 本地开发一键启动：后端 API + 前端控制台。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
#
# 为什么需要这个脚本：
# 只要 Windows 用户环境里存在 DATA_AGENT_API_KEY 或 DATA_AGENT_TENANT_API_KEYS_JSON，
# 后端就会要求每个请求带 X-API-Key。前端从不发这个头，而且下载按钮是 <a download href>，
# 浏览器的锚点无法携带请求头 —— 于是控制台整页 401，界面上还看不出原因。
# 本地开发用不上租户鉴权，所以这里只清掉本进程（及其子进程）里的这两个变量，
# 你保存在用户环境变量里的值原封不动。
[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173,
    [switch]$NoWeb
)

$ErrorActionPreference = "Stop"

# 控制台按 UTF-8 输出，否则中文提示会显示为乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$Python = Join-Path $RootDir "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "找不到 $Python`n先建虚拟环境：python -m venv backend\.venv，再 pip install -e backend[dev,api]"
}

# 两个都要清：JSON 那个优先级更高，只清 DATA_AGENT_API_KEY 不起作用。
$env:DATA_AGENT_API_KEY = $null
$env:DATA_AGENT_TENANT_API_KEYS_JSON = $null

function Test-PortBusy([int]$Port) {
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $listening
}

if (Test-PortBusy $ApiPort) {
    throw "$ApiPort 端口已被占用。先停掉占用它的进程，或加 -ApiPort 换一个端口。"
}

Write-Output "==> 启动后端 API（端口 $ApiPort，已关闭租户鉴权）"
$apiArgs = @(
    "-m", "uvicorn", "data_agent.api:app",
    "--host", "127.0.0.1", "--port", "$ApiPort", "--reload"
)
$api = Start-Process -FilePath $Python -ArgumentList $apiArgs -WorkingDirectory $RootDir -PassThru

# 等健康检查通过再起前端，否则前端首屏会先撞上一次连接失败。
$healthy = $false
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/health" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        # 还没起来，继续等。
    }
}
if (-not $healthy) {
    throw "后端 30 秒内没有就绪，看一眼它自己那个窗口里的报错。"
}
Write-Output "    就绪：http://127.0.0.1:$ApiPort/docs"

if ($NoWeb) {
    Write-Output ""
    Write-Output "只起了后端。停止：taskkill /PID $($api.Id) /T /F"
    return
}

if (-not (Test-Path (Join-Path $RootDir "apps\web\node_modules"))) {
    throw "前端依赖没装。先执行：cd apps\web 然后 npm install"
}

Write-Output "==> 启动前端控制台（端口 $WebPort）"
$web = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" `
    -WorkingDirectory (Join-Path $RootDir "apps\web") -PassThru

Write-Output ""
Write-Output "控制台：http://localhost:$WebPort"
Write-Output "停止：taskkill /PID $($api.Id) /T /F  以及关掉前端那个窗口"
