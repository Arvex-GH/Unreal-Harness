# Unreal Harness installer (Windows PowerShell)
#
#   From the extracted folder:
#     .\install.ps1 -Project "C:\Games\MyGame\MyGame.uproject"
#
#   If PowerShell blocks the script, run it in one shot instead:
#     powershell -ExecutionPolicy Bypass -File .\install.ps1 -Project "..."
#
#   Once the repo is public you can also do:
#     irm https://raw.githubusercontent.com/Arvex-GH/Unreal-Harness/main/install.ps1 | iex
[CmdletBinding()]
param(
  [string]$Project = "",
  [string]$InstallDir = "$env:LOCALAPPDATA\UnrealHarness\app",
  [string]$Repo = "https://github.com/Arvex-GH/Unreal-Harness.git",
  [string]$Branch = "main",
  [switch]$NoBridge,
  [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is only set when this runs as a file. When piped through
# `iex` (irm ... | iex) it is empty, so fall back to the current directory.
$scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
function Info($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  v $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  x $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  +--------------------------------------+" -ForegroundColor Magenta
Write-Host "  |  Unreal Harness installer            |" -ForegroundColor Magenta
Write-Host "  +--------------------------------------+" -ForegroundColor Magenta
Write-Host ""

# --- python -----------------------------------------------------------------
$py = $null
foreach ($cand in @("py -3.13", "py -3.12", "py -3.11", "py -3.10", "python")) {
  $parts = $cand.Split(" ")
  $exe = $parts[0]; $rest = @($parts | Select-Object -Skip 1)
  if (Get-Command $exe -ErrorAction SilentlyContinue) {
    try {
      $ver = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ([version]$ver -ge [version]"3.10") { $py = $cand; break }
    } catch {}
  }
}
if (-not $py) { Fail "Python 3.10+ not found. Install it from python.org (tick 'Add to PATH') and re-run." }
Ok "python: $py"
$pyExe, $pyArgs = $py.Split(" ")[0], @($py.Split(" ") | Select-Object -Skip 1)

# --- fetch ------------------------------------------------------------------
$localSource = Join-Path $scriptRoot "pyproject.toml"
if ((Test-Path $localSource) -and -not (Test-Path (Join-Path $InstallDir ".git"))) {
  Info "installing from local folder: $scriptRoot"
  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  robocopy $scriptRoot $InstallDir /MIR /XD .git .venv __pycache__ releases /NFL /NDL /NJH /NJS /NP | Out-Null
} elseif (Test-Path (Join-Path $InstallDir ".git")) {
  Info "updating existing install"
  & git -C $InstallDir fetch --all --prune
  & git -C $InstallDir pull --ff-only origin $Branch
} else {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is required. Install Git for Windows, or download the release zip and run install.ps1 from inside it."
  }
  Info "cloning $Repo"
  & git clone --branch $Branch $Repo $InstallDir
}
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) { Fail "fetch failed" }

# --- venv -------------------------------------------------------------------
$venv = Join-Path $InstallDir ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
  Info "creating virtualenv"
  & $pyExe @pyArgs -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"
Info "installing dependencies"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -e $InstallDir
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
Ok "dependencies installed"

# --- launcher ---------------------------------------------------------------
$binDir = Join-Path $env:LOCALAPPDATA "UnrealHarness\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$launcher = Join-Path $binDir "uh.cmd"
@"
@echo off
"$vpy" -m harness %*
"@ | Set-Content -Path $launcher -Encoding ASCII
Ok "launcher: $launcher"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
  Ok "added $binDir to your PATH (restart your terminal)"
}

# --- desktop shortcut -------------------------------------------------------
if (-not $NoShortcut) {
  try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Unreal Harness.lnk"))
    $lnk.TargetPath = $launcher
    $lnk.WorkingDirectory = $InstallDir
    $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $lnk.Description = "AI developer with full control of Unreal Engine 5"
    $lnk.Save()
    Ok "desktop shortcut created"
  } catch { Warn "could not create shortcut: $_" }
}

# --- engine bridge ----------------------------------------------------------
if (-not $NoBridge) {
  if (-not $Project) {
    $found = Get-ChildItem -Path (Get-Location) -Filter *.uproject -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $Project = $found.FullName }
  }
  if ($Project -and (Test-Path $Project)) {
    Info "installing engine bridge into $(Split-Path $Project -Parent)"
    & $vpy -m harness install-bridge --project $Project
    if ($LASTEXITCODE -ne 0) { Warn "bridge install failed - retry from Settings in the app" }
  } else {
    Warn "no .uproject found - install the bridge later from Settings"
  }
}

Write-Host ""
Write-Host "  Done." -ForegroundColor Green
Write-Host ""
Write-Host "  Start:     uh            (or the desktop shortcut)"
Write-Host "  Diagnose:  uh doctor"
Write-Host "  Update:    uh update"
Write-Host "  Data:      $env:APPDATA\UnrealHarness"
Write-Host "  Install:   $InstallDir"
Write-Host ""
Write-Host "  1. LM Studio -> Developer -> Start Server"
Write-Host "  2. Run uh -> UI opens at http://127.0.0.1:8787"
Write-Host "  3. Settings -> Install engine bridge -> restart the editor"
Write-Host ""
