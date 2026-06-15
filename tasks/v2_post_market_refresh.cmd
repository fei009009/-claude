@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist outputs\logs mkdir outputs\logs
echo ==== %date% %time% post-market-refresh ====>> outputs\logs\task_post_market_refresh.log
"C:\Program Files\Python312\python.exe" -u main.py post-market-refresh >> outputs\logs\task_post_market_refresh.log 2>&1
exit /b %ERRORLEVEL%
