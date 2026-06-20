@echo off
echo ==========================================================
echo   AI Native Local Knowledge Database - Start Script
echo ==========================================================
echo.

echo Checking and stopping existing server process on port 8080...
powershell -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
taskkill /f /im uvicorn.exe >nul 2>&1
echo.

echo 1. Opening application in browser (http://localhost:8080)...
start "" "http://localhost:8080"
echo.
echo 2. Starting FastAPI/Uvicorn server...
echo.
.\env\Scripts\uvicorn main:app --reload --port 8080
pause
