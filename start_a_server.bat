@echo off
rem A-side log server (default port 8765; set PORT=xxxx to override before calling)
cd /d %~dp0
if not defined PORT set PORT=8765
py a_server.py
pause
