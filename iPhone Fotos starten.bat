@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title iPhone Foto-Export

rem --- Python 3.10+ suchen: py-Launcher, PATH, uebliche Installationsorte ---
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
  "%%~D\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY="%%~D\python.exe""
)
if not defined PY (
  echo.
  echo  Python 3.10 oder neuer wurde nicht gefunden.
  echo  Bitte von https://www.python.org/downloads/windows/ installieren
  echo  und dabei "Add python.exe to PATH" anhaken. Danach diese Datei erneut starten.
  echo.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)

rem --- Benoetigte Bibliothek beim ersten Start installieren ---
%PY% -c "import pymobiledevice3" >nul 2>nul
if errorlevel 1 (
  echo Erster Start: installiere benoetigte Bibliothek ^(pymobiledevice3^), das dauert einige Minuten ...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  Installation fehlgeschlagen. Siehe Meldungen oben.
    pause
    exit /b 1
  )
)

rem --- Tool starten (Fenster minimiert; Schliessen des Fensters beendet das Tool) ---
start "iPhone Foto-Export" /min %PY% "%~dp0iphone_fotos.py"
