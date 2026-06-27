#!/usr/bin/env bash
# Wipe the paper-trading DB for a clean start.
#
# Everything the dashboard shows (fills, equity, signals, forecasts, arb, cache)
# lives in data/paper.db — removing it resets the whole book. On the next launch
# store.connect() recreates the schema and seeds starting_cash = BANKROLL.
#
# IMPORTANT: stop the trader daemon + dashboard FIRST, so nothing is holding the
# file open or writing mid-wipe. Then run this, then relaunch ./run_paper.sh.
#
#   pkill -f 'src.paper.trader'; pkill -f 'src.server'   # or stop your systemd/tmux
#   ./reset_paper.sh
#   ./run_paper.sh
set -euo pipefail
cd "$(dirname "$0")"

if pgrep -f 'src.paper.trader' >/dev/null 2>&1; then
  echo "!! src.paper.trader is still running — stop it first (pkill -f src.paper.trader)."
  echo "   Wiping while it runs will leave a half-written DB."
  exit 1
fi

rm -f data/paper.db data/paper.db-wal data/paper.db-shm
echo "✓ paper.db wiped. Relaunch with ./run_paper.sh — it starts fresh at \$BANKROLL,"
echo "  trades the open (June 6) markets, and holds back per MAX_DAY_FRACTION/CASH_BUFFER."
