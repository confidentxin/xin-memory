# Make sure the whole stack is up, repairing whatever is down.
#
# Checks, in dependency order:
#   1. Postgres container   -> start it, wait for healthy
#   2. kernel /health       -> restart it
#   3. 30s heartbeat        -> start it
#
# Safe to run any time; every step is skipped when already healthy.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\ensure-running.ps1

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$container = "xin-memory-postgres-1"
$kernelUrl = if ($env:KERNEL_URL) { $env:KERNEL_URL.TrimEnd("/") } else { "http://127.0.0.1:8741" }
$repaired = @()

function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Fix($text) { Write-Host "   FIX $text" -ForegroundColor Yellow; $script:repaired += $text }

# 1. 数据库
$state = (& cmd /c "docker inspect -f `"{{.State.Status}}`" $container 2>&1") -join ""
if ($state -notmatch "running") {
  & cmd /c "docker start $container 2>&1" | Out-Null
  Fix "postgres 容器已启动（原来是 $state）"
} else {
  Ok "postgres 正在运行"
}
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
  $health = (& cmd /c "docker inspect -f `"{{.State.Health.Status}}`" $container 2>&1") -join ""
  if ($health -match "healthy") { $healthy = $true; break }
  Start-Sleep -Seconds 2
}
if ($healthy) { Ok "postgres healthy" } else { Write-Host "   !!  postgres 未 healthy，内核可能起不来" -ForegroundColor Red }

# 2. 内核
$up = $false
for ($i = 0; $i -lt 3; $i++) {
  try {
    $r = Invoke-WebRequest "$kernelUrl/health" -UseBasicParsing -TimeoutSec 4
    if ($r.StatusCode -eq 200) { $up = $true; break }
  } catch { }
  Start-Sleep -Seconds 1
}
if (-not $up) {
  Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match "memory-kernel" -or $_.CommandLine -match "memory-kernel\.exe`?\s+serve" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
  # 用 UseShellExecute 启动脚本本身：直接 & 调用时，start /b 起的子进程会继承
  # 当前 shell 的句柄，导致这个脚本永久挂住等它退出。
  $kpsi = New-Object System.Diagnostics.ProcessStartInfo
  $kpsi.FileName = "$Root\start-kernel.cmd"
  $kpsi.UseShellExecute = $true
  $kpsi.WindowStyle = "Hidden"
  [void][System.Diagnostics.Process]::Start($kpsi)
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
      $r = Invoke-WebRequest "$kernelUrl/health" -UseBasicParsing -TimeoutSec 3
      if ($r.StatusCode -eq 200) { $up = $true; break }
    } catch { }
  }
  if ($up) { Fix "内核已重启" } else { Write-Host "   !! 内核仍不可用，看 kernel.err.log" -ForegroundColor Red }
} else {
  Ok "内核在运行"
}

# 3. 心跳
$loops = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "tick-loop" -and $_.Name -eq "cmd.exe" }).Count
if ($loops -eq 0) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = "$env:SystemRoot\System32\cmd.exe"
  $psi.Arguments = "/c `"`"$Root\tick-loop.cmd`"`""
  $psi.UseShellExecute = $true
  $psi.WindowStyle = "Hidden"
  [void][System.Diagnostics.Process]::Start($psi)
  Start-Sleep -Seconds 2
  $loops = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "tick-loop" -and $_.Name -eq "cmd.exe" }).Count
  Fix "心跳已启动"
} else {
  Ok "心跳在运行（$loops 个）"
}

Write-Host ""
if ($repaired.Count -eq 0) {
  Write-Host "全部正常，无需修复。" -ForegroundColor Green
} else {
  Write-Host "已修复 $($repaired.Count) 项：" -ForegroundColor Green
  $repaired | ForEach-Object { Write-Host "  - $_" }
}
