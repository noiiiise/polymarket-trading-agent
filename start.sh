#!/bin/bash
set -e

PORT="${PORT:-8080}"

echo "Starting Polymarket Trading Agent..."

restart_agent() {
    while true; do
        python agent.py
        EXIT_CODE=$?
        echo "Agent exited with code $EXIT_CODE — restarting in 10s..."
        sleep 10
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
