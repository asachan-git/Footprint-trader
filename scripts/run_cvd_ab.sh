#!/bin/sh
# launchd wrapper — daily CVD-div gate A/B report (appends to the daily log)
cd /Users/aniteksachan/Strategies/FootprintBiot || exit 1
echo "----- $(date '+%Y-%m-%d %H:%M:%S %Z') -----" >> data/reports/cvd_ab_daily.log
PYTHONPATH=. .venv/bin/python scripts/cvd_ab_report.py >> data/reports/cvd_ab_daily.log 2>&1
