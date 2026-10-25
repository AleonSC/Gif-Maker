@echo off
REM Build GifMaker locally on Windows. Requires Python 3.10+ on PATH.
REM Makes dist\GifMaker\GifMaker.exe, and dist\GifMaker-Setup.exe if Inno Setup 6 is installed.
python -m pip install -r requirements-build.txt || exit /b 1
python -m PyInstaller --noconfirm --onedir --windowed --name GifMaker ^
  --icon assets\gifmaker.ico --add-data "assets;assets" ^
  --collect-all imageio_ffmpeg --collect-all tkinterdnd2 run_gifmaker.py || exit /b 1
echo.
echo Built dist\GifMaker\GifMaker.exe
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% /Q installer\GifMaker.iss && echo Built dist\GifMaker-Setup.exe
)
