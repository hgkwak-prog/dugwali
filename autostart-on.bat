@echo off
rem Start Dugwali automatically when you sign in to Windows. No admin rights needed.
setlocal
set "TARGET=%~dp0run.bat"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Dugwali.lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath=$env:TARGET; $s.WorkingDirectory=(Split-Path $env:TARGET); $s.WindowStyle=7; $s.Description='Dugwali work-log agent'; $s.Save()"
if exist "%LNK%" (
    echo Registered. Dugwali will start minimized at your next sign-in.
    echo Shortcut: %LNK%
) else (
    echo Could not create the startup shortcut.
)
pause
