@echo off
cd /d "%~dp0"
.\.venv\Scripts\python.exe -X utf8 -m sui_bot 1> bot_run_out.log 2> bot_run_err.log
