@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Ambient Music Player

rem  Find a Python that already has mutagen + numpy.
rem  PATH order is not reliable: bundled/miniconda interpreters may lack them.
set "PYEXE="
for /f "delims=" %%P in ('where python 2^>nul') do call :probe "%%P"
if not defined PYEXE for /f "delims=" %%P in ('where py 2^>nul') do call :probe "%%P"
if not defined PYEXE call :probe "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not defined PYEXE goto :nopy

echo.
echo   Music Player starting...
echo   Python: %PYEXE%
echo   URL   : http://127.0.0.1:8760/
echo   Press Ctrl+C to stop. Keep this window open while playing.
echo.
"%PYEXE%" app.py %*
if errorlevel 1 (
  echo.
  echo   [ERROR] Player stopped. Read the traceback above.
  echo   Common causes: port 8760 already in use, or missing audio files.
  pause
)
exit /b 0

:probe
if defined PYEXE exit /b 0
if not exist %1 exit /b 0
%1 -c "import mutagen,numpy" >nul 2>&1 || exit /b 0
set "PYEXE=%~1"
exit /b 0

:nopy
echo   [WARN] No Python with mutagen + numpy found. Installing dependencies...
for /f "delims=" %%P in ('where python 2^>nul') do (
  "%%P" -m pip install -r requirements.txt
  goto :rerun
)
echo   [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
pause
exit /b 1

:rerun
for /f "delims=" %%P in ('where python 2^>nul') do call :probe "%%P"
if not defined PYEXE (
  echo   [ERROR] Install failed. Run manually: python -m pip install -r requirements.txt
  pause
  exit /b 1
)
"%PYEXE%" app.py %*
if errorlevel 1 pause
exit /b 0
