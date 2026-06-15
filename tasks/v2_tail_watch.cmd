@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist outputs\logs mkdir outputs\logs
echo ==== %date% %time% tail-watch ====>> outputs\logs\task_tail_watch.log
"C:\Program Files\Python312\python.exe" -u main.py tail-watch --push >> outputs\logs\task_tail_watch.log 2>&1
exit /b %ERRORLEVEL%
