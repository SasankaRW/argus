@echo off
rem Double-click to start Argus: Ollama, argusd, a worker, then Helios in your browser.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" up
if errorlevel 1 pause
