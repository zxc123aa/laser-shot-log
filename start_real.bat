@echo off
rem Real-shot mode one-click (re)start: b_watcher + sheet auto-backup + thomson helper
rem Already-running instances are killed first, so this bat is always safe to re-run.
chcp 65001 >nul
title laser-shot-log B-side - REAL mode
cd /d %~dp0

echo [0/3] Stopping already-running watcher / backup / helper ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'b_watcher\.py|sheet_backup\.py|thomson_helper\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

timeout /t 2 >nul

echo [1/3] Starting b_watcher (report + target-type reconcile)...
start "BWatcher" /min py b_watcher.py

echo [2/3] Starting sheet auto-backup (shotlist\date\*.xlsx)...
start "SheetBackup" /min py sheet_backup.py

echo [3/3] Starting 8767 energy / target-type page...
start "THelper" /min py thomson_helper.py

echo.
echo All started (real mode). You can close this window.
echo Page: http://127.0.0.1:8767
timeout /t 3 >nul
