@echo off
rem 真实打靶模式启动：只开打靶监测 + 能量填报页，不开发次模拟器
rem （模拟器只用于测试/演示，真实打靶时 TPS 谱仪自己往监视目录写图）
title 打靶日志系统 B机 · 真实打靶模式
cd /d %~dp0

echo [1/2] 启动打靶监测 b_watcher...
start "BWatcher" /min "C:\Users\CLAPA\.workbuddy\binaries\python\versions\3.13.12\python.exe" b_watcher.py

echo [2/2] 启动能量填报页 (http://127.0.0.1:8767)...
start "THelper" /min "C:\Users\CLAPA\.workbuddy\binaries\python\versions\3.13.12\python.exe" thomson_helper.py

echo.
echo 已启动（真实打靶模式）：监测中 / 8767 能量页 / 未启动模拟器
echo (本窗口可以关闭)
timeout /t 3 >nul
