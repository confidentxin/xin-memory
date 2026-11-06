@echo off
REM Heartbeat for proactive reminders.
REM `wake` = tick (fire due triggers) + write one JSON per fired intent into
REM the desktop inbox, which the DSH plugin polls and turns into a followup.
REM `tick` alone only records an episode and never reaches the conversation.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
:loop
".venv\Scripts\memory-kernel.exe" wake --user default >> tick.log 2>&1
ping -n 31 127.0.0.1 >nul
goto loop
