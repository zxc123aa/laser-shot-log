@echo off
rem A-side log server (default port 8765; set PORT=xxxx to override before calling)
rem Kills any previous instance listening on the port before starting.
cd /d %~dp0
if not defined PORT set PORT=8765
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1
py a_server.py
pause
