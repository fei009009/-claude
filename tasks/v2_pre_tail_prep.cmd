@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist outputs\logs mkdir outputs\logs
echo ==== %date% %time% pre-tail-prep ====>> outputs\logs\task_pre_tail_prep.log
"C:\Program Files\Python312\python.exe" -u main.py pre-tail-prep >> outputs\logs\task_pre_tail_prep.log 2>&1
exit /b %ERRORLEVEL%
