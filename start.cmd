@echo off
rem D2R Run Ledger -- starts the tracker and opens the page.
rem Close this window to stop tracking. Data: %~dp0data\history.json
title D2R Run Ledger
cd /d "%~dp0"
start "" http://127.0.0.1:8777
py tracker.py %*
pause
