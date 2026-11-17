@echo off
rem Stop starting Dugwali automatically at sign-in.
setlocal
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Dugwali.lnk"
if exist "%LNK%" (
    del "%LNK%"
    echo Removed. Dugwali will no longer start at sign-in.
) else (
    echo Auto-start was not registered.
)
pause
