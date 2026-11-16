@echo off
rem Builds send_menu.dll, the stub exe, the icons and one unsigned sparse package per device into windows\send-menu\build\.
rem Needs VS 2022 Build Tools (C++) and the Windows 10/11 SDK (makeappx). Output is never committed.
setlocal
set "HERE=%~dp0"
set "OUT=%HERE%build"
set "SDK=C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64"
if not exist "%SDK%\makeappx.exe" echo makeappx not found & exit /b 1
if not defined VSCMD_VER call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
if exist "%OUT%" rmdir /s /q "%OUT%"
rem the files Explorer loads go in %OUT%\app (the packages' external location); each package holds only a manifest and logo
mkdir "%OUT%\app" || exit /b 1
pushd "%OUT%\app"
cl /nologo /EHsc /O2 /MT /LD /DUNICODE /D_UNICODE /Fe:send_menu.dll "%HERE%send_menu.cpp" /link /DEF:"%HERE%send_menu.def" shell32.lib shlwapi.lib ole32.lib uuid.lib user32.lib advapi32.lib || exit /b 1
cl /nologo /O2 /MT /Fe:send_stub.exe "%HERE%stub.cpp" || exit /b 1
del *.obj *.exp *.lib 2>nul
popd
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%make-assets.ps1" -Out "%OUT%\app" || exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%make-packages.ps1" -Build "%OUT%" -Sdk "%SDK%" || exit /b 1
echo Built %OUT%\app\send_menu.dll and the unsigned ArgusSend-phone.msix and ArgusSend-calypso.msix
exit /b 0
