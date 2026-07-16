#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

# Kill old process if running
OLD_PID=$(pgrep -f "crypto_scalper/live.py" 2>/dev/null)
if [ -n "$OLD_PID" ]; then
    echo "Stopping old process (PID $OLD_PID)..."
    kill $OLD_PID 2>/dev/null
    sleep 2
fi

echo "Starting crypto scalper..."
nohup python3 crypto_scalper/live.py >> crypto_scalper/logs/live.log 2>&1 &
echo "Crypto scalper started with PID: $!"
sleep 3
tail -5 crypto_scalper/logs/live.log
