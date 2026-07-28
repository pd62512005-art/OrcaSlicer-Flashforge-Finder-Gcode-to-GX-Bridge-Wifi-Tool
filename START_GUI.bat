@echo off
cd /d "%~dp0"
py -3 finder_orca_gui.py
if errorlevel 1 pause
