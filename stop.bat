@echo off
echo ==========================================================
echo   AI Native Local Knowledge Database - Stop Script
echo ==========================================================
echo.
echo Stopping Uvicorn process on port 8080...

powershell -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
taskkill /f /im uvicorn.exe >nul 2>&1

echo.
echo Server stopped successfully.
echo.
pause
