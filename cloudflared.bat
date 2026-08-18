@echo off
set PROJ=C:\Users\user\Documents\claude\VSCode\stock-analysis

echo [1/2] Checking Flask server...
netstat -an 2>nul | findstr ":5000 " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo Starting Flask server...
    start "stock-server" /min "%PROJ%\_server.bat"
    ping 127.0.0.1 -n 6 >nul
) else (
    echo Flask already running.
)

echo [2/2] Starting Cloudflare Quick Tunnel...
echo.
echo When you see the trycloudflare.com URL, copy that https link.
echo No account needed. No bandwidth limit. URL changes each time you restart this.
echo Press Ctrl+C to stop the tunnel.
echo.
"%PROJ%\cloudflared.exe" tunnel --url http://localhost:5000
