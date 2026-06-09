@echo off
cd /d "%~dp0"
echo ============================================
echo  Historical Data Backfill (2011 ~ today)
echo ============================================
echo.
echo  Estimated time:
echo    Daily prices   : ~1.5 hours
echo    Monthly revenue: ~21  hours
echo    Quarterly      : ~8.5 hours
echo.
echo  Already-filled data is skipped automatically.
echo  Safe to Ctrl+C and restart anytime.
echo.
"C:\Users\user\anaconda3\python.exe" backfill.py --from-year 2011 --prices --revenue --quarterly
echo.
echo Backfill finished. Press any key to close.
pause
