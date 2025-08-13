@echo off
cd /d %~dp0\app
if not exist logs\uvicorn.err echo No logs yet. && exit /b 0
powershell -NoProfile -Command "Get-Content -Path 'logs\uvicorn.err' -Tail 200 -Wait"
