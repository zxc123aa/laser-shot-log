@echo off
rem 启动发次模拟器（TPS 谱仪落盘测试）
rem 使用系统 Python 3.10（自带 tkinter；managed Python 3.13 无 tkinter）
cd /d %~dp0
"D:\Program Files\Python310\python.exe" shot_simulator.py
if errorlevel 1 pause
