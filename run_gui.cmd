@echo off
setlocal
rem Use run_gui.vbs for a completely hidden launcher. This CMD wrapper is kept for troubleshooting.
wscript.exe "%~dp0run_gui.vbs" %*
exit /b %errorlevel%
