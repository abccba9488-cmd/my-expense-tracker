@echo off
cd /d "%~dp0"
echo ============================================
echo  FinMind Data Backfill for 達人選股 (2013 ~ today)
echo ============================================
echo.

if "%FINMIND_TOKEN%"=="" (
    echo ERROR: FINMIND_TOKEN environment variable is not set.
    echo Set it once with:  setx FINMIND_TOKEN "your-token-here"
    echo then open a new terminal / re-run this script.
    pause
    exit /b 1
)

echo  Already-filled data is skipped automatically.
echo  Safe to Ctrl+C and restart anytime.
echo.
"C:\Users\user\anaconda3\python.exe" backfill_finmind.py --from-year 2013 --all
echo.
echo FinMind backfill finished. Press any key to close.
pause
