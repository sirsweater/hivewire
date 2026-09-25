@echo off
rem Double-click to flash an ESP32-C6 plugged into this PC.
rem Opens the Flash page in your browser; close this window to stop it.
cd /d "%~dp0hive_admin"
python -c "import serial" 2>nul || python -m pip install --user pyserial
python -c "import esptool" 2>nul || python -m pip install --user esptool
start "" http://127.0.0.1:8090/
python hive_admin.py --flash-only --http-port 8090 --data "%USERPROFILE%\hive_data"
pause
