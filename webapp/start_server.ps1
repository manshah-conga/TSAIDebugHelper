# start_server.ps1 -- start the TS Intelligent Debug Helper web app.
#
#   .\start_server.ps1                          # localhost only, port 8000
#   .\start_server.ps1 -Listen All              # reachable from other machines
#   .\start_server.ps1 -Listen All -Port 8000
#   .\start_server.ps1 -Install                 # create .venv, install deps, then start
#
# Run it from anywhere -- it cd's to its own folder, which is what makes the
# "app.main:app" import path resolve.

param(
    [int]$Port = 8000,
    [ValidateSet('Local', 'All')]
    [string]$Listen = 'Local',
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

function Resolve-BasePython {
    # Bare `uvicorn` is deliberately never used: on Windows pip's Scripts folder
    # (where uvicorn.exe lands) is often not on PATH, which is the usual cause of
    # "'uvicorn' is not recognized...". Everything goes through `python -m`.
    foreach ($candidate in @('python', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            # The Microsoft Store stub named python.exe exits without running anything.
            try { & $candidate -c "import sys" 2>$null | Out-Null } catch { continue }
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}

# ---- create the venv and install dependencies, if asked ----------------------

if ($Install) {
    $base = Resolve-BasePython
    if (-not $base) {
        Write-Error "Neither 'python' nor 'py' is a working Python on PATH. Install Python 3.10+ from python.org (tick 'Add python.exe to PATH') and re-run."
    }
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Cyan
        & $base -m venv .venv
    }
    Write-Host "Installing dependencies into .venv ..." -ForegroundColor Cyan
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
    Write-Host "Dependencies installed." -ForegroundColor Green
    Write-Host ""
}

# ---- pick the interpreter ----------------------------------------------------

if (Test-Path $venvPython) {
    $py = $venvPython
} else {
    $py = Resolve-BasePython
}
if (-not $py) {
    Write-Error "Neither 'python' nor 'py' is a working Python on PATH. Install Python 3.10+ from python.org (tick 'Add python.exe to PATH') and re-run."
}

# ---- verify uvicorn is installed *for this interpreter* ----------------------
#
# "No module named uvicorn" almost always means the dependencies were installed
# for a different Python than the one being used to launch -- e.g. `pip install`
# hit a Store Python or a venv while `python` resolves elsewhere. Checking here
# turns that into a message that names the interpreter instead of a traceback.

& $py -c "import uvicorn, fastapi" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dependencies are missing for this interpreter:" -ForegroundColor Red
    & $py -c "import sys; print('    ' + sys.executable)"
    Write-Host ""
    Write-Host "Fix it with either:" -ForegroundColor Yellow
    Write-Host "    .\start_server.ps1 -Install        (recommended -- uses an isolated .venv)"
    Write-Host "    start_server.bat install           (same, from cmd.exe)"
    Write-Host "or install into that exact interpreter yourself:"
    Write-Host "    `"$py`" -m pip install -r requirements.txt"
    Write-Host ""
    Write-Host "Note: `"pip install`" on its own can target a different Python than the" -ForegroundColor DarkGray
    Write-Host "one above. Always install with `"<python> -m pip install`" to be sure." -ForegroundColor DarkGray
    exit 1
}

# ---- run ---------------------------------------------------------------------

$bindHost = if ($Listen -eq 'All') { '0.0.0.0' } else { '127.0.0.1' }

Write-Host ""
Write-Host "TS Intelligent Debug Helper" -ForegroundColor Cyan
& $py -c "import sys; print('  python   : ' + sys.executable)"
Write-Host "  binding  : ${bindHost}:$Port"
Write-Host "  open     : http://127.0.0.1:$Port"
if ($Listen -eq 'All') {
    Write-Host ""
    Write-Host "  Reachable from other machines. Windows Firewall and (on AWS) the" -ForegroundColor Yellow
    Write-Host "  instance's Security Group must both allow inbound TCP $Port." -ForegroundColor Yellow
    Write-Host "  There is no HTTPS here -- restrict the Security Group source to your" -ForegroundColor Yellow
    Write-Host "  own IP or corporate CIDR, never 0.0.0.0/0." -ForegroundColor Yellow
}
Write-Host ""

& $py -m uvicorn app.main:app --host $bindHost --port $Port
