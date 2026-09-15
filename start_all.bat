@echo off
rem Demo mode: start b_watcher + energy page + shot simulator
title laser-shot-log B-side launcher (demo mode with simulator)
cd /d %~dp0

echo [1/3] Starting b_watcher...
start "BWatcher" /min py b_watcher.py

echo [2/3] Starting energy page (http://127.0.0.1:8767)...
start "THelper" /min py thomson_helper.py

echo [3/3] Starting shot simulator...
start "" py shot_simulator.py

echo.
echo All started: watcher / 8767 energy page / simulator
echo (you can close this window)
timeout /t 3 >nul
