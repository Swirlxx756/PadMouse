@echo off
rem Build dist\PadMouse\ (PadMouse.exe + _internal) and dist\PadMouse.zip (requires Python 3)
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --onedir --noconsole --name PadMouse ^
  --icon assets\PadMouse.ico --hidden-import pystray._win32 ^
  --exclude-module numpy --exclude-module yaml ^
  --exclude-module PIL.ImageTk --exclude-module PIL._imagingtk ^
  --exclude-module PIL._avif --exclude-module PIL._webp --exclude-module PIL._imagingcms ^
  --exclude-module PIL._imagingmath ^
  --exclude-module ssl --exclude-module _ssl --exclude-module asyncio ^
  --exclude-module multiprocessing --exclude-module unittest --exclude-module xml ^
  --exclude-module email --exclude-module http --exclude-module urllib ^
  --exclude-module decimal --exclude-module lzma --exclude-module bz2 --exclude-module sqlite3 ^
  pad_mouse.py || exit /b 1
copy /y "*.txt" "dist\PadMouse\" >nul
del /q "dist\PadMouse\requirements.txt" 2>nul
powershell -NoProfile -Command "Compress-Archive -Path 'dist\PadMouse' -DestinationPath 'dist\PadMouse.zip' -Force"
echo Done: dist\PadMouse.zip
