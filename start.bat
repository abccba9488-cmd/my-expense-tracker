@echo off
set PROJ=C:\Users\user\Documents\claude\VSCode\stock-analysis

netstat -an 2>nul | findstr ":5000 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    start http://localhost:5000
    exit /b 0
)

start "stock-server" /min "%PROJ%\_server.bat"
ping 127.0.0.1 -n 6 >nul
start http://localhost:5000
exit /b 0
