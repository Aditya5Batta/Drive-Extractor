# Figure Data Extractor - Setup Script (PowerShell)
Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  Figure Data Extractor - Setup" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# Remove broken venv if exists
if (Test-Path "venv") {
    Write-Host "Removing old broken venv..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "venv"
}

Write-Host "Creating virtual environment (--without-pip first)..." -ForegroundColor Green
python -m venv venv --without-pip

Write-Host "Activating venv..." -ForegroundColor Green
& .\venv\Scripts\Activate.ps1

Write-Host "Installing pip manually..." -ForegroundColor Green
python -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', 'get-pip.py')"
python get-pip.py
Remove-Item -Force "get-pip.py" -ErrorAction SilentlyContinue

Write-Host "Installing dependencies..." -ForegroundColor Green
pip install -r requirements.txt

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  IMPORTANT: Edit .env file and add your" -ForegroundColor Yellow
Write-Host "  ANTHROPIC_API_KEY before running." -ForegroundColor Yellow
Write-Host ""
Write-Host "  To run:" -ForegroundColor Cyan
Write-Host "    .\venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "    python server.py" -ForegroundColor White
Write-Host "======================================" -ForegroundColor Cyan
