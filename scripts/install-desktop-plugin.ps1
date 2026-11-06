# Merge the memory plugin into the DeepSeek Harness desktop profile.
# The CLI refuses profile name "desktop", so this copies the package and
# appends one insert row. Restart the desktop app after it runs.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install-desktop-plugin.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Plugin = Join-Path $Root "integrations\dsh-memory-plugin"
$DshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE ".dsh" }
$ProfileDir = Join-Path $DshHome "profiles\desktop"
$Dest = Join-Path $ProfileDir "node_modules\@memory-kernel\dsh-memory"
$Patch = Join-Path $ProfileDir "cordis.patch.yml"
$SnippetPath = Join-Path $Root "integrations\dsh\desktop-profile.snippet.yml"

Write-Host "DSH_HOME=$DshHome"
Write-Host "profile=$ProfileDir"
Write-Host "KERNEL_URL=$($env:KERNEL_URL)"
Write-Host "MEMORY_USER_ID=$($env:MEMORY_USER_ID)"

if (-not (Test-Path $Plugin)) {
  throw "plugin package not found: $Plugin"
}
New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
if (Test-Path $Dest) {
  Remove-Item -Recurse -Force $Dest
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item -Recurse -Force (Join-Path $Plugin "src") (Join-Path $Dest "src")
Copy-Item -Force (Join-Path $Plugin "package.json") (Join-Path $Dest "package.json")
Copy-Item -Force (Join-Path $Plugin "cordis.patch.yml") (Join-Path $Dest "cordis.patch.yml")
Copy-Item -Force (Join-Path $Plugin "README.md") (Join-Path $Dest "README.md")

$snippet = Get-Content -Raw -Encoding UTF8 $SnippetPath
if (-not (Test-Path $ProfileDir)) {
  New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
}
if (-not (Test-Path $Patch)) {
  [IO.File]::WriteAllText($Patch, $snippet, (New-Object System.Text.UTF8Encoding $false))
  Write-Host "wrote $Patch"
} else {
  $existing = Get-Content -Raw -Encoding UTF8 $Patch
  if ($existing -match "id:\s*memory-kernel-plugin") {
    Write-Host "cordis.patch.yml already has memory-kernel-plugin; left it unchanged"
  } else {
    $suffix = "`n" + $snippet.TrimEnd() + "`n"
    Add-Content -Path $Patch -Value $suffix -Encoding UTF8
    Write-Host "appended memory-kernel-plugin to $Patch"
  }
}

Write-Host "Restart the DeepSeek Harness desktop app so the plugin loads."
Write-Host "Optional: set WAKE_SESSION_ID to the open chat id (session-...)."
Write-Host "Wake: `$env:WAKE_TARGET='desktop'; `$env:KERNEL_URL='http://127.0.0.1:8741'; `$env:MEMORY_USER_ID='default'; memory-kernel wake"
