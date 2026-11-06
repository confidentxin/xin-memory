# Upgrade an existing install: pull, reinstall, restart.
#
# What it does:
#   1. git pull (skip when the tree has local changes)
#   2. stop the kernel, pip install -e ., start it again
#   3. re-sync the DSH plugin through the official CLI (fallback: copy script)
#   4. restart the 30s heartbeat
#
# Data is untouched: episodes / facts / intents all live in Postgres.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\upgrade-windows.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Step($n, $text) { Write-Host ""; Write-Host "== $n/4  $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   !!  $text" -ForegroundColor Yellow }

Step 1 "拉取最新代码"
$dirty = & cmd /c "git status --porcelain 2>&1"
if ($dirty) {
  Warn "有未提交改动，跳过 git pull"
} else {
  & cmd /c "git pull --ff-only 2>&1" | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "已是最新" } else { Warn "git pull 失败，继续用当前代码" }
}

Step 2 "重装内核并重启"
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "memory-kernel" -and $_.Name -match "python|memory-kernel" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$out = & $venvPy -m pip install -q -e . 2>&1
if ($LASTEXITCODE -ne 0) { Warn ($out | Select-Object -Last 5 | Out-String); throw "pip install 失败" }
Ok "代码已更新"

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
    Ok "内核已重启：$health"
    $up = $true
    break
  } catch { }
}
if (-not $up) { Warn "内核未就绪，看 kernel.err.log" }

Step 3 "同步 DSH 插件"
$dsh = $env:DSH_CLI
if (-not $dsh) {
  $guess = "D:\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
  if (Test-Path $guess) { $dsh = $guess }
}
$pluginPath = Join-Path $Root "integrations\dsh-memory-plugin"
if ($dsh) {
  $env:CI = "true"
  & cmd /c "`"$dsh`" plugin --profile desktop add `"file:$($pluginPath -replace '\\','/')`" 2>&1" | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "已通过 dsh CLI 同步（file: 依赖）" } else { $dsh = $null }
}
if (-not $dsh) {
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\install-desktop-plugin.ps1")
  Ok "已通过复制方式同步（未找到 dsh CLI）"
}

Step 4 "重启心跳"
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
Ok "心跳运行中（$loops 个循环）"

Write-Host ""
Write-Host "升级完成。重启 DSH 让插件加载新代码。" -ForegroundColor Green
Write-Host "数据未受影响：episodes / facts / intents 都在 Postgres 里。"
