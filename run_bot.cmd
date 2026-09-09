@echo off
cd /d "%~dp0"
rem Windows uses a local proxy (mihomo/clash) for Telegram; plain Python ignores it.
set "HTTP_PROXY=http://127.0.0.1:10808"
set "HTTPS_PROXY=http://127.0.0.1:10808"
set "NO_PROXY=localhost,127.0.0.1,tnt.traviann.ir"
set "ALL_PROXY="
.\.venv\Scripts\python.exe -X utf8 -m sui_bot 1> bot_run_out.log 2> bot_run_err.log
