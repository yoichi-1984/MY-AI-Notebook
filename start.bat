@echo off
echo ==========================================================
echo   AI Native Local Knowledge Database 起動スクリプト
echo ==========================================================
echo.
echo 1. ブラウザでアプリケーション (http://localhost:8080) を開きます...
start "" "http://localhost:8080"
echo.
echo 2. FastAPI/Uvicorn サーバーを起動します...
echo.
.\env\Scripts\uvicorn main:app --reload --port 8080
pause
