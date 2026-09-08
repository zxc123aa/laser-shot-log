@echo off
rem A机（主控机）启动实验日志服务器
rem 8765 被其他服务占用，改用 8766
cd /d %~dp0
set PORT=8766
python a_server.py
pause
