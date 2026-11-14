# Unreal Harness uninstaller (Windows PowerShell)
#   .\uninstall.ps1            keeps chats + settings
#   .\uninstall.ps1 -Purge     deletes chats + settings too
param([switch]$Purge, [string]$InstallDir = "$env:LOCALAPPDATA\UnrealHarness\app")

$data = Join-Path $env:APPDATA "UnrealHarness"
$bin = Join-Path $env:LOCALAPPDATA "UnrealHarness\bin"

Write-Host "Removing launcher and PATH entry..." -ForegroundColor Cyan
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $bin "uh.cmd")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -like "*$bin*") {
  $clean = ($userPath.Split(";") | Where-Object { $_ -and $_ -notlike "*UnrealHarness\bin*" }) -join ";"
  [Environment]::SetEnvironmentVariable("Path", $clean, "User")
}

Write-Host "Removing desktop shortcut..." -ForegroundColor Cyan
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath("Desktop")) "Unreal Harness.lnk")

Write-Host "Removing app ($InstallDir)..." -ForegroundColor Cyan
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $InstallDir
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Split-Path $InstallDir -Parent)

if ($Purge) {
  Write-Host "Removing data ($data)..." -ForegroundColor Cyan
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $data
} else {
  Write-Host "Kept chats and settings in $data (re-run with -Purge to delete)" -ForegroundColor Yellow
}
Write-Host "Done. The UEHarness plugin can also be deleted from <YourProject>\Plugins\UEHarness." -ForegroundColor Green
