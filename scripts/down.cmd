@echo off
rem Double-click to stop the argusd and worker windows started by up.cmd.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" down
timeout /t 3 >nul
