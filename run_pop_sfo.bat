@echo off
chcp 65001 >nul
cd /d "%~dp0"
cls
echo ====================================================================
echo  FLIGHT MONITOR: Puerto Plata (POP) to San Francisco (SFO)
echo  Strict 1 Stop Only ^| Airlines: American (AA), JetBlue (B6), United (UA), Copa (CM)
echo  Window: Departure >= May 20, Return <= July 5 ^| Step: 1 day
echo  Stay: 14 to 30 nights (17 options)
echo ====================================================================

echo.
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe tracker_pop_sfo.py --step-days 1 --threshold 700 --browser %*
) else (
    python tracker_pop_sfo.py --step-days 1 --threshold 700 --browser %*
)
echo.
echo ====================================================================
echo  Scan completed. Results saved to database and opened in Chrome.
echo ====================================================================
pause
