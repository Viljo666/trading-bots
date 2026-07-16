#!/bin/bash
# ============================================
#  TRADING BOT RESTART SCRIPT
#  Just double-click this file to restart all bots.
#  A black window will open — that's normal!
#  Wait until you see "ALL DONE!" before closing it.
# ============================================

cd "$(dirname "$0")"

echo ""
echo "============================================"
echo "  RESTARTING TRADING BOTS — please wait..."
echo "============================================"
echo ""

# Activate Python environment
source .venv/bin/activate

# --- Bot 1: Main Bot (Alpaca) ---
echo "▶️  Starting Main Bot..."
pkill -f "python.*main.py" 2>/dev/null
sleep 1
nohup python3 main.py >> logs/$(date +%Y-%m-%d).log 2>&1 &
MAIN_PID=$!
sleep 2
if ps -p $MAIN_PID > /dev/null 2>&1; then
    echo "   ✅ Main Bot is running (PID $MAIN_PID)"
else
    echo "   ⚠️  Main Bot may not have started — tell Dad"
fi

echo ""

# --- Bot 2: Crypto Scalper ---
echo "▶️  Starting Crypto Scalper..."
pkill -f "crypto_scalper/live.py" 2>/dev/null
sleep 1
nohup python3 crypto_scalper/live.py >> crypto_scalper/logs/live.log 2>&1 &
CRYPTO_PID=$!
sleep 2
if ps -p $CRYPTO_PID > /dev/null 2>&1; then
    echo "   ✅ Crypto Scalper is running (PID $CRYPTO_PID)"
else
    echo "   ⚠️  Crypto Scalper may not have started — tell Dad"
fi

echo ""

# --- Bot 3: Monitor ---
echo "▶️  Starting Monitor..."
pkill -f "bot_monitor.py" 2>/dev/null
sleep 1
nohup python3 bot_monitor.py >> monitor.out.log 2>&1 &
MON_PID=$!
sleep 2
if ps -p $MON_PID > /dev/null 2>&1; then
    echo "   ✅ Monitor is running (PID $MON_PID)"
else
    echo "   ⚠️  Monitor may not have started — tell Dad"
fi

echo ""
echo "============================================"
echo "  ALL DONE! You can close this window now."
echo "  Thank you! 😊"
echo "============================================"
echo ""
