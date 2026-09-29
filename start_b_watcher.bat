@echo off
rem B机（数据机）启动实验数据监视器
cd /d %~dp0
python b_watcher.py
pause
