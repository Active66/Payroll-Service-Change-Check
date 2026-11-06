@echo off
REM ---------------------------------------------------------------
REM  Payroll change check - local demo (employee chat + admin console)
REM
REM  Double-click this file to:
REM    1) start the FastAPI service on 127.0.0.1:8000 (minimised window)
REM    2) open the login page in your default browser
REM
REM  Demo accounts:
REM    admin / Admin12345    (administrator: knowledge base, prompts, audit)
REM    alice / Alice12345    (employee: chat only)
REM
REM  ADMIN_SEED_PASSWORD / demo accounts below are for LOCAL DEMO ONLY.
REM  For a real deployment: leave ADMIN_SEED_PASSWORD empty so a random
REM  password is printed once at startup, and create accounts with
REM  `payroll-check user add`.
REM
REM  To stop: close the minimised window titled "payroll-check server".
REM
REM  Kept ASCII-only on purpose: cmd.exe renders UTF-8 comments as
REM  garbled text under the default GBK code page.
REM ---------------------------------------------------------------
setlocal

set "ROOT=%~dp0.."
REM --- pick an interpreter that actually has the deps -----------------
REM Hard-coding one path means a broken/upgraded Python silently kills the
REM launcher: the minimised window flashes and closes, user sees nothing.
REM So probe candidates in order and keep the first that can import the stack.
set "PY="
for %%P in (
    "%ROOT%\.venv\Scripts\python.exe"
    "C:\Users\ROG\.workbuddy\binaries\python\envs\payroll311\Scripts\python.exe"
    "C:\Users\ROG\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) do (
    if not defined PY if exist "%%~P" (
        "%%~P" -c "import fastapi, jieba, numpy; fastapi.FastAPI; jieba.lcut; numpy.zeros(1)" >nul 2>&1 && set "PY=%%~P"
    )
)
if not defined PY (
    where python >nul 2>&1 && python -c "import fastapi, jieba, numpy; fastapi.FastAPI; jieba.lcut; numpy.zeros(1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] No Python with project dependencies was found.
    echo         Install them once with:
    echo             python -m pip install -r requirements.txt
    echo         Then run this file again.
    pause
    exit /b 1
)
echo Using Python: %PY%

set "PYTHONPATH=%ROOT%\src"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
REM Only used on the very first start (empty user table)
if not defined ADMIN_SEED_PASSWORD set "ADMIN_SEED_PASSWORD=Admin12345"
cd /d "%ROOT%"

REM --- already running? just open the browser ---------------------
netstat -ano -p TCP | findstr /c:"127.0.0.1:8000" | findstr /i "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Service already running on http://127.0.0.1:8000/login
    if /i not "%SKIP_BROWSER%"=="1" start "" http://127.0.0.1:8000/login
    exit /b 0
)

echo Starting service on http://127.0.0.1:8000/ ...
echo A minimised window "payroll-check server" will keep it alive.
echo Close that window to stop the service.
echo.

REM --- run in its own window so this launcher can exit -------------
start "payroll-check server" /min "%PY%" -m my_tool serve --host 127.0.0.1 --port 8000

REM --- give uvicorn a moment, then open the login page --------------
timeout /t 6 /nobreak >nul
if /i not "%SKIP_BROWSER%"=="1" start "" http://127.0.0.1:8000/login

endlocal
