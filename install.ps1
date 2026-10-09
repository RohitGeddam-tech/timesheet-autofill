# timesheet-autofill - Windows installer.
#   powershell -ExecutionPolicy Bypass -File install.ps1              set up / change settings (safe to re-run)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   stop the daily job
param([switch]$Uninstall)
# Not "Stop": Windows PowerShell 5.1 turns harmless stderr output from python/pip into fatal errors.
# Native commands are checked via $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"

$Dir     = $PSScriptRoot
$Script  = Join-Path $Dir "timesheet.py"
$Service = "timesheet-autofill"

# Full path of a real Python 3.9+ (skips the Microsoft Store "python" stub).
function Find-Python {
    $found = @()
    try { if (Get-Command py -ErrorAction SilentlyContinue)     { $found += & py -3 -c "import sys; print(sys.executable)" 2>$null } } catch {}
    try { if (Get-Command python -ErrorAction SilentlyContinue) { $found += & python -c "import sys; print(sys.executable)" 2>$null } } catch {}
    # A fresh per-user install is not on PATH until the next login.
    $found += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($exe in $found) {
        if (-not $exe) { continue }
        $exe = "$exe".Trim()
        if (Test-Path $exe) {
            & $exe -c "import sys; sys.exit(sys.version_info < (3, 9))"
            if ($LASTEXITCODE -eq 0) { return $exe }
        }
    }
    return $null
}

$Py = Find-Python
if (-not $Py) {
    Write-Host "Python 3.9+ is needed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this." -ForegroundColor Red
    exit 1
}

if ($Uninstall) {
    & $Py $Script config set SCHEDULE=off
    $user = & $Py $Script config get TS_USER
    if ($user -and (Read-Host "Also delete the saved site password from Credential Manager? [y/N]") -match '^[Yy]') {
        & $Py -m keyring del $Service $user
        Write-Host "Password deleted."
    }
    exit 0
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "The GitHub CLI is needed: install it from https://cli.github.com, run 'gh auth login', then re-run this." -ForegroundColor Red
    exit 1
}

Write-Host "Installing the 'keyring' Python package (stores your password in Windows Credential Manager)..."
& $Py -m pip install --user --quiet --disable-pip-version-check --no-warn-script-location keyring
if ($LASTEXITCODE -ne 0) { Write-Host "pip install keyring failed - see the error above." -ForegroundColor Red; exit 1 }

# `timesheet` command: a one-line wrapper next to the script, pinned to the Python found above.
[IO.File]::WriteAllText((Join-Path $Dir "timesheet.cmd"), "@`"$Py`" `"%~dp0timesheet.py`" %*`r`n")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ';') -notcontains $Dir) {
    if ((Read-Host "Add $Dir to your PATH, so 'timesheet' works from any folder? [Y/n]") -notmatch '^[Nn]') {
        [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";$Dir"), "User")
        Write-Host "Added. Open a new terminal for it to take effect."
    }
}

# Settings, password, login test and the optional daily job are all `timesheet config`.
& $Py $Script config
exit $LASTEXITCODE
