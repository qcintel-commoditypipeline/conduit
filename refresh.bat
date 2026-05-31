@echo off
cd /d "%~dp0"
python gas_dashboard.py --no-browser >> refresh.log 2>&1
