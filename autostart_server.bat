@echo off
set PROJ=C:\Users\user\Documents\claude\VSCode\stock-analysis

netstat -an 2>nul | findstr ":5000 " | findstr "LISTENING" >nul
if not errorlevel 1 exit /b 0

start "stock-server" /min "%PROJ%\_server.bat"
exit /b 0
