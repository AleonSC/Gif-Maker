@echo off
REM Build GifMaker.exe locally on Windows. Requires Python 3.10+ on PATH.
python -m pip install -r requirements-build.txt || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name GifMaker ^
  --collect-all imageio_ffmpeg --collect-all tkinterdnd2 run_gifmaker.py || exit /b 1
echo.
echo Built dist\GifMaker.exe
