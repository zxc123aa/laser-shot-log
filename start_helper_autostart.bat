@echo off
rem 汤姆逊能量填报 helper 开机自启（后台运行，无窗口）
cd /d %~dp0
"C:\Users\CLAPA\.workbuddy\binaries\python\versions\3.13.12\python.exe" thomson_helper.py
