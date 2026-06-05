@echo off
setlocal
cd /d "%~dp0"
if not "%~1"=="" set DXFWIZ_PORT=%~1
".venv\Scripts\python.exe" -m dxfwiz.ui.app
