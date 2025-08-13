@echo off
cd /d %~dp0
echo Stopping Wildfire MVP...
for /f "tokens=2" %%a in ('tasklist ^| findstr /i "uvicorn"') do taskkill /PID %%a /F
for /f "tokens=2" %%a in ('tasklist ^| findstr /i "python"') do taskkill /PID %%a /F
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /PID %%p /F
echo Done.
pause
