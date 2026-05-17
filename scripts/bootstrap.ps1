# Council — one-command bootstrap for Windows PowerShell.
# Run from the repo root:
#   .\scripts\bootstrap.ps1

$ErrorActionPreference = "Stop"

Write-Host "[Council] Checking Python..."
$pyVer = (python --version) 2>&1
if (-not ($pyVer -match "Python 3\.(1[2-9]|[2-9]\d)")) {
    Write-Host "Python 3.12+ required. Found: $pyVer" -ForegroundColor Red
    exit 1
}

Write-Host "[Council] Setting up venv..."
if (-not (Test-Path .venv)) {
    python -m venv .venv
}
.\.venv\Scripts\Activate.ps1

Write-Host "[Council] Installing dependencies..."
python -m pip install --upgrade pip
pip install -e ".[dev]"

Write-Host "[Council] Done. Start the server with:"
Write-Host "  python server.py" -ForegroundColor Green
Write-Host "Then open http://localhost:8765"
