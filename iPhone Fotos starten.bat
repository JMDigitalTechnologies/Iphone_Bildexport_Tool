@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -c "import pymobiledevice3" 2>nul || (
  echo Installiere pymobiledevice3 ...
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m pip install pymobiledevice3
)
start "" "%PY%" "%~dp0iphone_fotos.py"
