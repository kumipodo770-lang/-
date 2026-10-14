@echo off
rem Bybit AutoTrade bot (mode is set only in config.yaml books, default paper)
cd /d "%~dp0"
title Bybit AutoTrade (verify)
".venv\Scripts\python.exe" -m app.main
pause
