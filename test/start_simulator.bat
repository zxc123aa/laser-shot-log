@echo off
rem 启动发次模拟器（TPS 谱仪落盘测试）——脚本在 test\ 子目录，回项目根目录再跑
rem tkinter 需要 D:\Python（py launcher 默认指向它，自带 tkinter）
cd /d %~dp0..
py test\shot_simulator.py
if errorlevel 1 pause
