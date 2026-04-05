#!/bin/bash
set -e

PORT="${PORT:-8080}"

echo "Starting Polymarket Trading Agent..."

restart_agent() {
    FAIL_COUNT=0
    MAX_BACKOFF=300  # cap at 5 minutes
    while true; do
        python agent.py
        EXIT_CODE=$?
        if [ "$EXIT_CODE" -eq 0 ]; then
            # Clean exit (e.g. SIGTERM) — reset failure counter and restart quickly
            FAIL_COUNT=0
            DELAY=5
        else
            FAIL_COUNT=$((FAIL_COUNT + 1))
            # Exponential backoff: 10s, 20s, 40s, 80s, 160s, then cap at 300s
            DELAY=$(( 10 * (1 << (FAIL_COUNT - 1)) ))
            if [ "$DELAY" -gt "$MAX_BACKOFF" ]; then
                DELAY=$MAX_BACKOFF
            fi
        fi
        echo "Agent exited with code $EXIT_CODE (failure #$FAIL_COUNT) — restarting in ${DELAY}s..."
        sleep "$DELAY"
    done
}

restart_agent &
AGENT_PID=$!
echo "Agent supervisor started (PID $AGENT_PID)"

echo "Starting dashboard on port $PORT..."
exec gunicorn \
  --bind "0.0.0.0:$PORT" \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --log-level info \
  "dashboard:app"
