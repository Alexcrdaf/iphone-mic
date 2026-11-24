@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m server.app
pause
