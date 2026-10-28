@echo off
rem B机（汤姆逊谱仪）启动能量上报辅助软件
rem 防旧进程：先杀 8767 旧实例再启动（旧代码常驻曾导致靶类型覆盖事故）
cd /d %~dp0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8767" ^| findstr "LISTENING"') do (
  echo killing old helper PID %%P ...
  taskkill /F /PID %%P >nul 2>&1
)
python thomson_helper.py
pause
