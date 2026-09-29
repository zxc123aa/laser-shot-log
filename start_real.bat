@echo off
rem Real-shot mode: b_watcher + thomson energy page + sheet auto-backup (no simulator)
title laser-shot-log B-side - REAL mode
cd /d %~dp0

echo [1/3] Starting b_watcher...
start "BWatcher" /min py b_watcher.py

echo [2/3] Starting sheet auto-backup (shotlist\date\*.xlsx)...
start "SheetBackup" /min py sheet_backup.py

echo [3/3] Starting energy page (http://127.0.0.1:8767)...
start "THelper" /min py thomson_helper.py

echo.
echo Started (real mode): watcher / auto-backup / 8767 energy page
echo (you can close this window)
timeout /t 3 >nul
