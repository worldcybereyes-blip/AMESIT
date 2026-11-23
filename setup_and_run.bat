@echo off
REM ===========================================================================
REM  AMESIT - one-click setup and run for Windows 10 / 11
REM  Creates venv, installs requirements.txt, generates sample data,
REM  runs the full pipeline and opens the dashboard.
REM  Usage:  setup_and_run.bat            (normal)
REM          setup_and_run.bat --reinstall (force dependency reinstall)
REM          setup_and_run.bat --nobrowser (do not open the dashboard)
REM  No WSL, Docker or bash needed.
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title AMESIT - The Information Operations Engine

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "MPLBACKEND=Agg"
set "REINSTALL=0"
set "OPENFLAG=--open"
for %%A in (%*) do (
  if /I "%%A"=="--reinstall" set "REINSTALL=1"
  if /I "%%A"=="--nobrowser" set "OPENFLAG="
)

echo.
echo ==========================================================================
echo   AMESIT - The Information Operations Engine : setup and run
echo ==========================================================================

REM ---- 1. locate Python 3.10+ ------------------------------------------------
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
  )
)
if not defined PYEXE (
  echo.
  echo [ERROR] Python 3.10 or newer was not found.
  echo         Install it from https://www.python.org/downloads/windows/
  echo         and tick "Add python.exe to PATH", then run this file again.
  goto :fail
)
for /f "delims=" %%V in ('%PYEXE% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%V"
echo [1/5] Using Python %PYVER%  (%PYEXE%)

REM ---- 2. virtual environment ------------------------------------------------
if not exist "venv\Scripts\python.exe" (
  echo [2/5] Creating virtual environment in venv\ ...
  %PYEXE% -m venv venv
  if errorlevel 1 (
    echo [ERROR] Could not create the virtual environment.
    goto :fail
  )
  set "REINSTALL=1"
) else (
  echo [2/5] Virtual environment found.
)
set "VPY=%~dp0venv\Scripts\python.exe"

REM ---- 3. dependencies -------------------------------------------------------
if not exist "venv\.amesit_deps_ok" set "REINSTALL=1"
if "%REINSTALL%"=="1" (
  echo [3/5] Installing dependencies from requirements.txt ^(first run takes a few minutes^) ...
  "%VPY%" -m pip install --upgrade pip >nul 2>nul
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [WARN] Pinned install failed ^(often a very new Python version^). Retrying with unpinned versions ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content -Encoding UTF8 'requirements.txt') | ForEach-Object { $_ -replace '==.*$','' } | Set-Content -Encoding ASCII 'venv\requirements.relaxed.txt'"
    "%VPY%" -m pip install -r "venv\requirements.relaxed.txt"
    if errorlevel 1 (
      echo [ERROR] Dependency installation failed. Check your internet connection / proxy and retry.
      goto :fail
    )
  )
  echo ok> "venv\.amesit_deps_ok"
) else (
  echo [3/5] Dependencies already installed ^(use --reinstall to force^).
)

REM ---- 4. sample data --------------------------------------------------------
echo [4/5] Generating synthetic sample dataset ...
"%VPY%" main.py generate-data
if errorlevel 1 (
  echo [ERROR] Sample data generation failed.
  goto :fail
)

REM ---- 5. pipeline -----------------------------------------------------------
echo [5/5] Running the AMESIT pipeline ...
"%VPY%" main.py run %OPENFLAG%
if errorlevel 1 (
  echo [ERROR] The pipeline reported an error. See output\amesit.log
  goto :fail
)

echo.
echo ==========================================================================
echo   Finished. Outputs are in the "output" folder:
echo     output\dashboard.html                    (opened automatically)
echo     output\AMESIT_Intelligence_Dossier.pdf
echo     output\graphs\amesit_network.html        (interactive graph)
echo   Run again later with:  venv\Scripts\python main.py run --open
echo ==========================================================================
echo.
pause
endlocal
exit /b 0

:fail
echo.
echo Setup did not complete. Please read the messages above.
echo.
pause
endlocal
exit /b 1
