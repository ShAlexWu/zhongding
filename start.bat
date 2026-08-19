@echo off
title AnhuiZhongDing Launcher

REM ============================================================
REM  Start the drawing-similarity frontend and backend, then
REM  open the page.
REM   - Backend:  FastAPI (uvicorn)  http://127.0.0.1:50011
REM   - Frontend: Vite (React)       http://127.0.0.1:50009
REM  Each runs in its own window; close that window to stop it.
REM ============================================================

cd /d "%~dp0"

if "%DASHSCOPE_API_KEY%"=="" (
  echo [WARN] DASHSCOPE_API_KEY is not set.
  echo        Image/text embedding and QWEN parsing will not work.
  echo        Set it first:  set DASHSCOPE_API_KEY=sk-xxxx
  echo.
)

echo [1/3] Starting backend (FastAPI, port 50011) ...
start "ZD Backend (50011)" /d "%~dp0backend" cmd /k "uv run uvicorn app:app --port 50011"

echo [2/3] Starting frontend (Vite, port 50009) ...
start "ZD Frontend (50009)" /d "%~dp0frontend" cmd /k "npm run dev -- --port 50009"

echo [3/3] Waiting for servers to start, then opening the page ...
timeout /t 8 /nobreak >nul
start "" http://localhost:50009/

echo.
echo Started: backend window "ZD Backend (50011)", frontend window "ZD Frontend (50009)".
echo Browser will open http://localhost:50009/
echo Close those two windows to stop the services.
echo.
pause
