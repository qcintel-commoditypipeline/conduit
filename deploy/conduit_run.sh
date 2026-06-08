#!/bin/bash
# conduit_run.sh — daily Conduit pipeline run (cron entrypoint).
# Generates the dashboard, persists data, builds + pushes the morning brief.
# On failure, pings Telegram so a silent cron death can't go unnoticed.

SCRIPT_DIR=/opt/scripts/conduit
LOG=/opt/scripts/conduit.log
PYTHON=/opt/scripts/venv/bin/python3

cd "$SCRIPT_DIR" || exit 1
# Load .env so failure-alert vars are available to this shell too.
set -a; [ -f /opt/scripts/.env ] && . /opt/scripts/.env; set +a
# Gas sitrep has no relevance in the shared FLARE telegram channel — suppress the
# brief push (the cron-failure alert below is separate and still fires).
export CONDUIT_NO_PUSH=1

echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] UTC CONDUIT run triggered" >> "$LOG"
"$PYTHON" "$SCRIPT_DIR/pipeline.py" >> "$LOG" 2>&1
status=$?

if [ $status -eq 0 ]; then
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] UTC CONDUIT complete OK" >> "$LOG"
else
    echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] UTC CONDUIT FAILED (exit $status)" >> "$LOG"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -m 20 -X POST \
          "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
          --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
          --data-urlencode "text=⚠️ CONDUIT pipeline FAILED (exit $status) — see /opt/scripts/conduit.log" \
          >> "$LOG" 2>&1
    fi
fi
exit $status
