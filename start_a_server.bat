@echo off
rem A机（主控机）启动实验日志服务器
rem 默认 8765；若本机 8765 被占用，可在调用前 set PORT=xxxx 覆盖
cd /d %~dp0
if not defined PORT set PORT=8765
python a_server.py
pause
