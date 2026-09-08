@echo off
rem 一键启动打靶日志系统 B 机全部组件（本机 = B-DAQ-01）
rem 双击即可：打靶监测 + 能量页(8767) + 模拟器GUI
title 打靶日志系统 B机 启动器
cd /d %~dp0

echo [1/3] 启动打靶监测 b_watcher...
start "BWatcher" /min "C:\Users\CLAPA\.workbuddy\binaries\python\versions\3.13.12\python.exe" b_watcher.py

echo [2/3] 启动能量填报页 (http://127.0.0.1:8767)...
start "THelper" /min "C:\Users\CLAPA\.workbuddy\binaries\python\versions\3.13.12\python.exe" thomson_helper.py

echo [3/3] 启动打靶模拟器...
start "" "D:\Program Files\Python310\python.exe" shot_simulator.py

echo.
echo 全部已启动：监测中 / 8767 能量页 / 模拟器
echo (本窗口可以关闭)
timeout /t 3 >nul
