# Local development launcher: backend API plus the web console, one command.
#
# Why it exists: this machine holds DATA_AGENT_API_KEY and
# DATA_AGENT_TENANT_API_KEYS_JSON as user-level environment variables, because
# docker compose cannot even parse its file without them. Every new terminal
# inherits them, so a plain `uvicorn` run switches tenant authentication on. The
# Vite dev server sends no X-API-Key, and the download buttons are
# <a download href> anchors that cannot carry a header at all, so the console
# 401s on every request with nothing on screen pointing at the cause. The
# deployed stack is fine: nginx injects the header server-side.
#
# Blanking the two variables below therefore covers this process tree only. What
# is stored in the user environment is left exactly as it was, so compose keeps
# working, and a plain uvicorn run in any other terminal still authenticates.
[CmdletBinding()]
param(
    [ValidateRange(1024, 65500)][int]$ApiPort = 8000,
    [ValidateRange(1024, 65500)][int]$WebPort = 5173,
    [switch]$NoWeb,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$RootDir = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $RootDir ".tmp\dev"
$StatePath = Join-Path $RunDir "processes.json"

function Stop-LocalServices {
    if (-not (Test-Path -LiteralPath $StatePath)) { return }
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    foreach ($entry in $state) {
        $process = Get-Process -Id $entry.id -ErrorAction SilentlyContinue
        # PID reuse must never terminate an unrelated process.
        if ($process -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $entry.started) {
            & taskkill.exe /PID $process.Id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "停止进程失败：$($process.Id)" }
        }
    }
    Remove-Item -LiteralPath $StatePath
}

function Find-FreePort([int]$Preferred) {
    foreach ($candidate in $Preferred..($Preferred + 20)) {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidate)
        try { $listener.Start(); return $candidate }
        catch [System.Net.Sockets.SocketException] { }
        finally { $listener.Stop() }
    }
    throw "从 $Preferred 开始的端口均不可用，请关闭旧服务后重试。"
}

function Wait-Ready([string]$Url, $Process) {
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) { throw "服务启动后退出，请查看日志：$RunDir" }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
                throw "接口鉴权仍未通过：$Url。请查看日志：$RunDir"
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "服务未在 45 秒内就绪：$Url。请查看日志：$RunDir"
}

if ($Stop) {
    Stop-LocalServices
    Write-Output "本地启动器管理的前后端已停止。"
    exit 0
}

$Python = Join-Path $RootDir "backend\.venv\Scripts\python.exe"
$Vite = Join-Path $RootDir "apps\web\node_modules\vite\bin\vite.js"
if (-not (Test-Path -LiteralPath $Python)) { throw "缺少后端虚拟环境，请按 README 安装后端依赖。" }
if (-not $NoWeb) {
    $Node = (Get-Command node.exe -ErrorAction Stop).Source
    if (-not (Test-Path -LiteralPath $Vite)) { throw "缺少前端依赖，请在 apps\web 运行 npm.cmd install。" }
}

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
Stop-LocalServices
$ApiPort = Find-FreePort $ApiPort
$WebPort = Find-FreePort $WebPort
if ($WebPort -eq $ApiPort) { $WebPort = Find-FreePort ($WebPort + 1) }
$ApiUrl = "http://127.0.0.1:$ApiPort"
$WebUrl = "http://127.0.0.1:$WebPort"

# Whitespace survives Windows PowerShell 5.1 and blocks dotenv from restoring a
# key. Authentication strips it to empty. LLM credentials remain available.
$overrides = @{
    DATA_AGENT_API_KEY = " "
    DATA_AGENT_TENANT_API_KEYS_JSON = " "
    VITE_API_BASE_URL = $ApiUrl
}
$saved = @{}
$started = @()
try {
    foreach ($name in $overrides.Keys) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $overrides[$name], "Process")
    }
    Write-Output "正在启动本地后端：$ApiUrl（仅本机访问）"
    $api = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "data_agent.api:app", "--host", "127.0.0.1", "--port", "$ApiPort"
    ) -WorkingDirectory $RootDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $RunDir "api.out.log") `
        -RedirectStandardError (Join-Path $RunDir "api.err.log")
    $started += @{ id = $api.Id; started = $api.StartTime.ToUniversalTime().Ticks.ToString() }
    ConvertTo-Json -InputObject @($started) | Set-Content -LiteralPath $StatePath -Encoding UTF8
    # Health alone is insufficient: it intentionally bypasses authentication.
    Wait-Ready "$ApiUrl/api/v1/config" $api
    Wait-Ready "$ApiUrl/api/v1/recipes" $api
    Wait-Ready "$ApiUrl/api/v1/semantic-models" $api

    if (-not $NoWeb) {
        Write-Output "正在启动前端：$WebUrl"
        $web = Start-Process -FilePath $Node -ArgumentList @(
            ('"' + $Vite + '"'), "--host", "127.0.0.1", "--port", "$WebPort", "--strictPort"
        ) -WorkingDirectory (Join-Path $RootDir "apps\web") -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $RunDir "web.out.log") `
            -RedirectStandardError (Join-Path $RunDir "web.err.log")
        $started += @{ id = $web.Id; started = $web.StartTime.ToUniversalTime().Ticks.ToString() }
        ConvertTo-Json -InputObject @($started) | Set-Content -LiteralPath $StatePath -Encoding UTF8
        Wait-Ready "$WebUrl/" $web
        Wait-Ready "$WebUrl/api/v1/config" $web
        Write-Output "启动成功，请打开：$WebUrl"
        if (-not $NoBrowser) { Start-Process $WebUrl }
    } else {
        Write-Output "后端启动成功：$ApiUrl"
    }
    Write-Output "停止服务：双击 stop-local.cmd。日志：$RunDir"
} catch {
    Stop-LocalServices
    throw
} finally {
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
    }
}
