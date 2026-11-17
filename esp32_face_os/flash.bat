@echo off
REM Build and flash esp32_face_os for Waveshare ESP32-S3-Touch-LCD-1.28-B
set ZEPHYR_BASE=C:\zephyrproject\zephyr
set ZEPHYR_SDK_INSTALL_DIR=C:\zephyr-sdk-0.16.8
set ZEPHYR_TOOLCHAIN_VARIANT=zephyr
set COM_PORT=COM3

call C:\zephyrproject\.venv\Scripts\activate.bat
cd /d C:\zephyrproject

echo Building esp32_face_os...
west build -b esp32s3_touch_lcd_1_28/esp32s3/procpu C:\Users\User\Desktop\Tamagci\esp32_face_os
if %ERRORLEVEL% NEQ 0 (
    echo Build failed!
    pause
    exit /b %ERRORLEVEL%
)

echo Flashing to %COM_PORT%...
west flash --esp-device=%COM_PORT% --esp-tool=C:\zephyrproject\.venv\Scripts\esptool.exe
if %ERRORLEVEL% NEQ 0 (
    echo Flash failed!
    pause
    exit /b %ERRORLEVEL%
)

echo Done!
pause
