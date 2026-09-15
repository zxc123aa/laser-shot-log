@echo off
rem Fake shot generator: Enter=1 shot, N=N shots, a=auto 15s, q=quit
title Fake Shot Generator
cd /d %~dp0
py test_shot_gen.py
pause
