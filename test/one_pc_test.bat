@echo off
rem 单机演示：一台电脑同时扮演 A 机 + B 机
cd /d %~dp0..
py test\one_pc_test.py
pause
