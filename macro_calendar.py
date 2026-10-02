"""Small, advisory macro calendar for future research tagging.

Only high-impact US events relevant to the futures basket are listed.  The
calendar is intentionally *not* a trading filter: it tags new shadow setups and
surfaces upcoming events in the morning digest so their effects can be measured
before any rule is considered.  Dates are sourced manually from official Fed
and BLS annual release schedules and must be refreshed annually.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

# Remaining 2026 dates.  CPI and Employment Situation are 08:30 ET; FOMC
# decisions are normally 14:00 ET on the final meeting day.
EVENTS: tuple[tuple[str, str, str], ...] = (
    ("2026-09-04T08:30:00", "Employment Situation", "NFP"),
    ("2026-09-11T08:30:00", "Consumer Price Index", "CPI"),
    ("2026-09-16T14:00:00", "FOMC decision", "FOMC"),
    ("2026-10-02T08:30:00", "Employment Situation", "NFP"),
    ("2026-10-14T08:30:00", "Consumer Price Index", "CPI"),
    ("2026-10-28T14:00:00", "FOMC decision", "FOMC"),
    ("2026-11-06T08:30:00", "Employment Situation", "NFP"),
    ("2026-11-10T08:30:00", "Consumer Price Index", "CPI"),
    ("2026-12-04T08:30:00", "Employment Situation", "NFP"),
    ("2026-12-09T14:00:00", "FOMC decision", "FOMC"),
    ("2026-12-10T08:30:00", "Consumer Price Index", "CPI"),
)


def events_between(start: datetime, end: datetime) -> list[dict[str, object]]:
    """Upcoming events in a UTC or timezone-aware time window."""
    start_et = start.astimezone(ET) if start.tzinfo else start.replace(tzinfo=ET)
    end_et = end.astimezone(ET) if end.tzinfo else end.replace(tzinfo=ET)
    found: list[dict[str, object]] = []
    for local, name, tag in EVENTS:
        event_at = datetime.fromisoformat(local).replace(tzinfo=ET)
        if start_et <= event_at <= end_et:
            found.append({"at": event_at, "name": name, "tag": tag})
    return found


def context_at(when: datetime, window_hours: int = 24) -> str:
    """Compact tag persisted with a new setup, or an empty string."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    earlier = when - timedelta(hours=window_hours)
    later = when + timedelta(hours=window_hours)
    tags = [str(event["tag"]) for event in events_between(earlier, later)]
    return "+".join(tags)


def briefing(now: datetime | None = None, horizon_hours: int = 48) -> str:
    """Telegram-ready advisory. It makes no recommendation to skip a trade."""
    now = now or datetime.now(timezone.utc)
    events = events_between(now, now + timedelta(hours=horizon_hours))
    if not events:
        return "📅 *Macro calendar* — no CPI, NFP, or FOMC event in the next 48h."
    lines = ["📅 *Macro calendar* — advisory; no bot rule changes"]
    for event in events:
        at = event["at"]
        lines.append(f"  · {event['tag']} — {event['name']}, {at:%a %d %b %H:%M ET}")
    lines.append("_New dry setups are tagged so this can be tested before use._")
    return "\n".join(lines)


if __name__ == "__main__":
    print(briefing())
