#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
echo "Starting main bot..."
nohup python3 main.py >> logs/$(date +%Y-%m-%d).log 2>&1 &
echo "Main bot started with PID: $!"
sleep 3
tail -5 logs/$(date +%Y-%m-%d).log
