@echo off
setlocal enabledelayedexpansion

set REPORT_DIR=D:\Tradingbot\backtest\reports

echo.
echo  Backtest Reports
echo  ================
echo.

set count=0
for /f "delims=" %%f in ('dir /b /o-d "%REPORT_DIR%\scalper_*.html" 2^>nul') do (
    set /a count+=1
    set "file!count!=%%f"
    echo  [!count!] %%f
)

if %count%==0 (
    echo  No reports found in %REPORT_DIR%
    echo.
    pause
    exit /b
)

echo.
echo  Press ENTER to open latest, or type a number:
set /p choice=" > "

if "%choice%"=="" set choice=1

set "selected=!file%choice%!"
if "%selected%"=="" (
    echo  Invalid selection.
    pause
    exit /b
)

echo.
echo  Opening: %selected%
start "" "%REPORT_DIR%\%selected%"
