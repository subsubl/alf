@echo off
REM Build and flash esp32_face_os for Waveshare ESP32-S3-Touch-LCD-1.28-B
setlocal
set ZEPHYR_BASE=C:\zephyrproject\zephyr
set ZEPHYR_SDK_INSTALL_DIR=C:\zephyr-sdk-0.16.8
set ZEPHYR_TOOLCHAIN_VARIANT=zephyr
set COM_PORT=COM3
set NO_PAUSE=0

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--no-pause" set NO_PAUSE=1
if /I "%~1"=="--batch" set NO_PAUSE=1
shift
goto parse_args
:args_done

call C:\zephyrproject\.venv\Scripts\activate.bat
cd /d C:\zephyrproject

echo Building esp32_face_os...
west build -p auto -b esp32s3_touch_lcd_1_28/esp32s3/procpu C:\Users\User\Desktop\Tamagci\esp32_face_os
set ERR=%ERRORLEVEL%
if %ERR% NEQ 0 (
    echo Build failed!
    if "%NO_PAUSE%"=="0" pause
    exit /b %ERR%
)

echo Flashing to %COM_PORT%...
west flash --esp-device=%COM_PORT% --esp-tool=C:\zephyrproject\.venv\Scripts\esptool.exe
set ERR=%ERRORLEVEL%
if %ERR% NEQ 0 (
    echo Flash failed!
    if "%NO_PAUSE%"=="0" pause
    exit /b %ERR%
)

echo Done!
if "%NO_PAUSE%"=="0" pause
exit /b 0
