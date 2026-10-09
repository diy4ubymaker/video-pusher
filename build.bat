@echo off
REM Builds two single-file executables, dist\VideoPusher.exe and dist\VideoReceiver.exe.
REM Each one has ffmpeg.exe, ffprobe.exe and ffplay.exe bundled inside, plus the assets folder,
REM so it runs on its own (no Python or FFmpeg install needed on the target PC).
REM
REM Requires: pip install -r requirements-dev.txt, and bin\ffmpeg.exe, bin\ffprobe.exe, bin\ffplay.exe
setlocal
cd /d "%~dp0"

for %%T in (ffmpeg ffprobe ffplay) do (
    if not exist "bin\%%T.exe" (
        echo ERROR: bin\%%T.exe not found. See bin\README.md
        exit /b 1
    )
)

call :build VideoPusher main.py || exit /b 1
call :build VideoReceiver receiver.py || exit /b 1

echo.
echo Built single-file executables (ffmpeg, ffprobe and ffplay inside):
echo   dist\VideoPusher.exe
echo   dist\VideoReceiver.exe
exit /b 0

:build
pyinstaller --noconfirm --clean --onefile --windowed --name %1 ^
    --add-binary "bin\ffmpeg.exe;bin" ^
    --add-binary "bin\ffprobe.exe;bin" ^
    --add-binary "bin\ffplay.exe;bin" ^
    --add-data "assets;assets" ^
    %2
exit /b %errorlevel%
