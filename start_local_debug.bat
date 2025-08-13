@echo off
setlocal
cd /d %~dp0\app
if not exist .venv\Scripts\python.exe (
  echo Creating venv...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r ..\requirements.txt || goto :error
set PYTHONUNBUFFERED=1
if not exist logs mkdir logs
echo === starting uvicorn (logs\uvicorn.out / logs\uvicorn.err) ===
.\.venv\Scripts\python -X tracemalloc -m uvicorn bot.main:app --host 0.0.0.0 --port 8000 1> logs\uvicorn.out 2> logs\uvicorn.err
goto :eof
:error
echo [ERROR] Failed to prepare or start. See messages above.
pause
