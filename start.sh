#!/bin/bash
set -e

PORT="${PORT:-8080}"

echo "Starting Polymarket Trading Agent..."
python agent.py &
AGENT_PID=$!
echo "Agent started (PID $AGENT_PID)"

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
