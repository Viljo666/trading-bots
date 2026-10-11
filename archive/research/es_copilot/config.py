"""Co-pilot configuration. Keep values as Python constants — no YAML for v1."""
from pathlib import Path

IBKR_HOST = "127.0.0.1"
IBKR_PAPER_PORT = 4002

POLL_INTERVAL_SEC = 60
WATCHDOG_STALL_MIN = 10

SYMBOL = "MES"
EXCHANGE = "CME"
DOLLARS_PER_POINT = 5.0   # MES is 1/10 of ES; ES=$50/pt, MES=$5/pt

BAR_DURATION = "5 D"
BAR_SIZE = "15 mins"

STRATEGY_RTH_ONLY = True
STRATEGY_EMA_PERIOD = None
STRATEGY_MIN_SLOPE_ABS = 0.0
STRATEGY_ATR_STOP_MULT = None

ANTHROPIC_MODEL = "claude-sonnet-4-6"

LOG_DIR = Path(__file__).resolve().parent / "logs"
EVENT_LOG = LOG_DIR / "events.jsonl"
CHAT_LOG = LOG_DIR / "chat.jsonl"
