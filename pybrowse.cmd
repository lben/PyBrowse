@echo off
rem Start PyBrowse (Windows). The first run creates a private Python
rem environment in .venv next to this script and installs the dependencies.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv"
set "PY=%VENV%\Scripts\python.exe"

rem (Re)install when the environment is missing, a previous install failed,
rem or requirements.txt changed since the last install.
fc /b "%HERE%requirements.txt" "%VENV%\requirements.installed" >nul 2>&1 && goto run

set "BOOT=python"
where py >nul 2>&1 && set "BOOT=py -3"
%BOOT% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 || (
  echo PyBrowse needs Python 3.10 or newer: https://www.python.org/downloads/
  exit /b 1
)
echo Setting up PyBrowse in %VENV% (first run only)...
if not exist "%PY%" %BOOT% -m venv "%VENV%" || exit /b 1
"%PY%" -m pip install --quiet --disable-pip-version-check -r "%HERE%requirements.txt" || exit /b 1
copy /y "%HERE%requirements.txt" "%VENV%\requirements.installed" >nul

:run
"%PY%" "%HERE%main.py" %*
exit /b %errorlevel%
