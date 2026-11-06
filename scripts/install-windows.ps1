# Memory kernel one-shot installer for Windows.
#
# What it does, in order:
#   1. check Python 3.11+
#   2. create .venv and install the package
#   3. start Postgres (docker compose) and wait for it
#   4. create .env from .env.example when missing
#   5. start the kernel and wait for /health
#   6. install the DSH plugin into the desktop profile
#   7. start the 30s heartbeat and register it at logon
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#
# After it finishes: put your API key in .env, then restart DSH.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Step($n, $text) { Write-Host ""; Write-Host "== $n/7  $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   !!  $text" -ForegroundColor Yellow }

Step 1 "检查 Python"
$python = $null
foreach ($candidate in @("python", "py -3.13", "py -3.12", "py -3.11")) {
  try {
    $version = & cmd /c "$candidate --version 2>&1"
    if ($LASTEXITCODE -eq 0 -and $version -match "3\.(1[1-9]|[2-9]\d)") {
      $python = $candidate
      Ok "$version  ($candidate)"
      break
    }
  } catch { }
}
if (-not $python) { throw "需要 Python 3.11+，请先安装：https://www.python.org/downloads/" }

Step 2 "创建虚拟环境并安装依赖"
# 内核进程会锁住 .venv\Scripts\memory-kernel.exe，pip 升级时报 WinError 32。
# 先停掉所有内核进程，第 5 步再重新拉起。
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "memory-kernel" -and $_.Name -match "python|memory-kernel" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$venv = Join-Path $Root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
  & cmd /c "$python -m venv .venv 2>&1" | Out-Null
  Ok "已创建 .venv"
} else {
  Ok ".venv 已存在，跳过创建"
}
$venvPy = Join-Path $venv "Scripts\python.exe"
# 直接调用，不走 cmd /c：cmd 的引号规则会把参数吞掉，导致莫名失败的退出码。
$installOutput = & $venvPy -m pip install -q -e . 2>&1
if ($LASTEXITCODE -ne 0) {
  Warn ($installOutput | Select-Object -Last 5 | Out-String)
  throw "pip install 失败"
}
Ok "已安装 memory_kernel"

Step 3 "启动数据库"
$docker = $null
try { & cmd /c "docker --version 2>&1" | Out-Null; if ($LASTEXITCODE -eq 0) { $docker = "docker" } } catch { }
if (-not $docker) {
  Warn "未找到 docker，跳过。请自行准备 PostgreSQL 16 + pgvector，并把 DATABASE_URL 写进 .env"
} else {
  & cmd /c "docker compose up -d postgres 2>&1" | Out-Null
  $ready = $false
  for ($i = 0; $i -lt 30; $i++) {
    $state = & cmd /c "docker inspect -f `"{{.State.Health.Status}}`" xin-memory-postgres-1 2>&1"
    if ($state -match "healthy") { $ready = $true; break }
    Start-Sleep -Seconds 2
  }
  if ($ready) { Ok "postgres 就绪（端口 54329）" } else { Warn "postgres 未在 60 秒内 healthy，请检查 docker compose logs postgres" }
}

Step 4 "准备 .env"
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
  Ok ".env 已存在，保留原文件"
} else {
  Copy-Item (Join-Path $Root ".env.example") $envFile
  Ok "已从 .env.example 生成 .env（记得填 OPENAI_API_KEY）"
}

Step 5 "启动内核"
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "memory-kernel" -and $_.Name -match "python|memory-kernel" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
# 不要用 `cmd /c start ... | Out-Null`：子进程继承管道句柄，PowerShell 会一直等它退出。
# UseShellExecute=true 让新进程不继承我们的句柄，才能真正后台化。
$kpsi = New-Object System.Diagnostics.ProcessStartInfo
$kpsi.FileName = "$env:SystemRoot\System32\cmd.exe"
$kpsi.Arguments = "/c start `"memory-kernel`" /b `".venv\Scripts\memory-kernel.exe`" serve > kernel.out.log 2> kernel.err.log"
$kpsi.UseShellExecute = $true
$kpsi.WindowStyle = "Hidden"
[void][System.Diagnostics.Process]::Start($kpsi)
$up = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Seconds 2
  try {
    $health = (Invoke-WebRequest "http://127.0.0.1:8741/health" -UseBasicParsing -TimeoutSec 3).Content
    Ok "内核已启动：$health"
    $up = $true
    break
  } catch { }
}
if (-not $up) { Warn "内核未就绪，看 kernel.err.log" }

Step 6 "安装 DSH 插件"
$dshCli = "D:\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
if (Test-Path $dshCli) {
  $env:CI = "true"
  $pluginPath = (Join-Path $Root "integrations\dsh-memory-plugin") -replace "\\", "/"
  & cmd /c "`"$dshCli`" plugin --profile desktop add `"file:$pluginPath`" 2>&1" | Out-Null
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dshCli)) {
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\install-desktop-plugin.ps1")
}
Ok "插件已写入 DSH desktop profile（重启 DSH 后生效）"

Step 7 "启动心跳并注册开机自启"
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "tick-loop" -and $_.Name -eq "cmd.exe" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "$env:SystemRoot\System32\cmd.exe"
$psi.Arguments = "/c `"`"$Root\tick-loop.cmd`"`""
$psi.UseShellExecute = $true
$psi.WindowStyle = "Hidden"
[void][System.Diagnostics.Process]::Start($psi)
Start-Sleep -Seconds 2
$loops = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "tick-loop" -and $_.Name -eq "cmd.exe" }).Count
if ($loops -ge 1) { Ok "心跳已启动（$loops 个循环）" } else { Warn "心跳未启动，手动双击 tick-loop.cmd" }
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\memory-kernel.cmd"
$shim = @"
@echo off
call "$Root\start-kernel.cmd"
ping -n 6 127.0.0.1 >nul
call "$Root\tick-loop.cmd"
"@
[IO.File]::WriteAllText($startup, $shim, (New-Object System.Text.ASCIIEncoding))
Ok "心跳已启动；登录自启：$startup"

Write-Host ""
Write-Host "完成。" -ForegroundColor Green
Write-Host "  1) 在 .env 里填 OPENAI_API_KEY（百炼或 OpenAI）"
Write-Host "  2) 重启 DeepSeek Harness，让插件生效"
Write-Host "  3) 急停：New-Item `"$env:USERPROFILE\.dsh\ACT_HALT`" -ItemType File"
