@echo off
rem Live key check (read only, no orders)
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\live_selfcheck.py
