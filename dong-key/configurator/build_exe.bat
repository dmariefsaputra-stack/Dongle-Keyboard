@echo off
REM Builds DongKey-Configurator.exe and a release zip with its SHA-256 hash.
REM Run from this folder on Windows with Python 3 installed.

set NAME=DongKey-Configurator
set VERSION=v1.0

python -m pip install -r requirements.txt pyinstaller || goto :error
python -m PyInstaller --onefile --windowed --clean --name %NAME% Magicbutton4.py || goto :error

if exist release rmdir /s /q release
mkdir release
powershell -NoProfile -Command "Compress-Archive -Path 'dist\%NAME%.exe' -DestinationPath 'release\%NAME%-%VERSION%.zip' -Force" || goto :error

certutil -hashfile "release\%NAME%-%VERSION%.zip" SHA256 > "release\%NAME%-%VERSION%.zip.sha256.txt"
type "release\%NAME%-%VERSION%.zip.sha256.txt"

echo.
echo Done. Upload release\%NAME%-%VERSION%.zip to a GitHub Release,
echo and paste the SHA-256 above into the release notes.
goto :eof

:error
echo.
echo Build failed.
exit /b 1
