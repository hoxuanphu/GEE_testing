@echo off
setlocal
pushd "%~dp0" || exit /b 1
where py.exe >nul 2>&1
if errorlevel 1 (
    python tools\start_workspace.py %*
) else (
    py -3.12 tools\start_workspace.py %*
)
set "result=%errorlevel%"
if not "%result%"=="0" (
    echo Satellite Workspace could not start. Check the message above and verify Python 3.12 x64 is available.
    if "%~1"=="" pause
)
popd
exit /b %result%
