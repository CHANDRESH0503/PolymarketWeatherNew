#!/usr/bin/env bash
# Launch the week-long paper-trading run: the trader daemon (scans + fills +
# marks every 15 min) and the dashboard server, together.
#
#   ./run_paper.sh
#   then open http://127.0.0.1:8000
#
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

INTERVAL="${1:-900}"   # seconds between paper-trading ticks (default 15 min)

echo "starting paper trader (every ${INTERVAL}s) + dashboard on :8000"
python -m src.paper.trader --loop "$INTERVAL" &
TRADER_PID=$!
python -m src.server &
SERVER_PID=$!

trap 'echo; echo "stopping…"; kill $TRADER_PID $SERVER_PID 2>/dev/null' INT TERM
wait
