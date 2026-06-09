@echo off
taskkill /fi "WINDOWTITLE eq 台股分析後端*" /f >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":5000 " ^| findstr "LISTENING"') do taskkill /pid %%p /f >nul 2>&1
taskkill /im ngrok.exe /f >nul 2>&1
echo Server and ngrok stopped.
timeout /t 1 /nobreak >nul
