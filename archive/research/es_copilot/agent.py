"""Claude-powered chat co-pilot.

Wraps the Anthropic SDK with a small set of read-only tools backed by the
shared CopilotState the orchestrator maintains. The agent never places orders
and never sees IBKR directly — it only sees what the state object exposes.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from .config import ANTHROPIC_MODEL
from .state import CopilotState, UserPosition


SYSTEM_PROMPT = """You are an experienced ES/MES day-trading co-pilot.

The trader is watching live MES on NinjaTrader on their own monitor and asks you for input on entries, bias, and risk. They execute manually in NT — you do NOT place orders.

Important context to internalize:
- Your data is from IBKR with a 15-20 minute delay. You react to CONFIRMED 15-minute bar closes, not live ticks. The trader has the live price on their NT chart — defer to that for current price.
- The strategy you watch is Tori-style trendline-break on 15m bars: scan for a trendline broken on bar close, wait for pullback to the broken line, enter on touch. Initial stop = last swing pivot; trails via "safety line" once new opposite pivots form.
- The trader has roughly €1k risk capital and is trading 1 MES contract. MES is $5/point, $1.25/tick. A 4-point stop = $20 risk. Keep that in mind when sizing your opinions on whether to take a trade.

How to behave:
- Be brief. Trading is fast — paragraphs are noise.
- Use your tools to get fresh data before commenting (`get_snapshot`, `get_active_setups`, `get_levels`, `get_recent_events`). Don't guess.
- When asked "what do you see?" or "should I take this?": state the active setup (if any), where price sits vs the trendline/levels, and a concise opinion. Don't hedge endlessly. Don't FOMO either.
- Flag risk honestly. If a setup is weak (small range, against trend, near key resistance), say so.
- When the trader tells you they've entered a position (verbally or via /long /short), call `log_position` to remember it. When they exit, call `clear_position`.
- If they ask about live price action and your snapshot is 15+ minutes old, remind them you can't see real-time and ask what they're seeing on NT.
"""


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_snapshot",
        "description": "Get the latest market snapshot: most recent 15m bar close, timestamp, IBKR connection state, and the current strategy state (idle / waiting_pullback / in_position).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_active_setups",
        "description": "Get the current strategy view: any pending trendline break, the line value at the latest bar, where the entry would be, and any open position the strategy is tracking internally (separate from the user's actual position).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_levels",
        "description": "Get prior-day high/low (PDH/PDL) and overnight high/low (ONH/ONL) for the current US session.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_events",
        "description": "Get the N most recent strategy events (break confirmed, entry signal, invalidated, safety line hit).",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max events to return (default 10).", "default": 10},
            },
        },
    },
    {
        "name": "get_user_position",
        "description": "Get the position the user has told you they're holding (logged via /long, /short, or by voice).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_position",
        "description": "Remember a position the user just entered manually in NinjaTrader. Use this when the user tells you they've gone long or short.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["long", "short"]},
                "quantity": {"type": "integer", "description": "Number of MES contracts (usually 1).", "default": 1},
                "entry_price": {"type": "number"},
                "stop": {"type": "number"},
                "target": {"type": "number", "description": "Optional profit target."},
            },
            "required": ["direction", "entry_price", "stop"],
        },
    },
    {
        "name": "clear_position",
        "description": "Forget the user's logged position (they've exited).",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class Copilot:
    def __init__(self, state: CopilotState, max_history_turns: int = 20):
        self.state = state
        self.client = anthropic.Anthropic()
        self.messages: list[dict[str, Any]] = []
        self.max_history_turns = max_history_turns

    def ask(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        self._trim_history()

        while True:
            response = self.client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=self.messages,
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                self.messages.append({"role": "assistant", "content": response.content})
                self.messages.append({"role": "user", "content": tool_results})
                continue

            self.messages.append({"role": "assistant", "content": response.content})
            return "".join(b.text for b in response.content if b.type == "text").strip()

    def _trim_history(self) -> None:
        # Keep most-recent N user/assistant pairs. Each "turn" = one user msg
        # followed by one (or more, for tool use) assistant/user blocks.
        max_msgs = self.max_history_turns * 4   # rough cap, includes tool_use round-trips
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]
            # Ensure first message is role=user (Claude requirement)
            while self.messages and self.messages[0]["role"] != "user":
                self.messages.pop(0)

    def _execute_tool(self, name: str, args: dict) -> Any:
        s = self.state
        if name == "get_snapshot":
            return {
                "last_price": s.last_price,
                "last_bar_ts": s.last_bar_ts,
                "connected": s.connected,
                "last_poll_at": s.last_poll_at.isoformat() if s.last_poll_at else None,
                "strategy_state": s.monitor.state if s.monitor else "not_initialized",
            }
        if name == "get_active_setups":
            return s.monitor.to_dict() if s.monitor else {"state": "not_initialized"}
        if name == "get_levels":
            return s.levels.to_dict() if s.levels else {"error": "no levels computed yet"}
        if name == "get_recent_events":
            limit = int(args.get("limit", 10))
            return [e.to_dict() for e in s.recent_events[-limit:]]
        if name == "get_user_position":
            return s.user_position.to_dict() if s.user_position else None
        if name == "log_position":
            s.user_position = UserPosition(
                direction=args["direction"],
                quantity=int(args.get("quantity", 1)),
                entry_price=float(args["entry_price"]),
                stop=float(args["stop"]),
                target=float(args["target"]) if args.get("target") is not None else None,
            )
            return {"ok": True, "logged": s.user_position.to_dict()}
        if name == "clear_position":
            s.user_position = None
            return {"ok": True}
        return {"error": f"unknown tool: {name}"}
