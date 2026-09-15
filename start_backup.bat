@echo off
rem Auto-backup A-server sheets to local shotlist\<date>\ as xlsx
cd /d %~dp0
py sheet_backup.py
pause
