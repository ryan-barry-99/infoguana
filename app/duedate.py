"""Due-date parsing + display-state helpers shared between web/MCP/templates.

`due_date` on a plan/task is stored as an ISO 'YYYY-MM-DD' string with no
time/TZ component. Comparison against "today" is done in the timezone set
by `INFOGUANA_DUEDATE_TZ` (IANA name, e.g. 'America/New_York'), defaulting
to UTC."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _resolve_default_tz() -> ZoneInfo:
    name = (os.environ.get("INFOGUANA_DUEDATE_TZ") or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


DEFAULT_TZ = _resolve_default_tz()


def today_local(tz: ZoneInfo = DEFAULT_TZ) -> date:
    return datetime.now(tz).date()


def parse_due_input(s: Optional[str], tz: ZoneInfo = DEFAULT_TZ) -> Optional[str]:
    """Accept ISO 'YYYY-MM-DD' or simple relative phrases ('today',
    'tomorrow', 'in 3 days', 'in 2 weeks'). Returns ISO date string or None
    if the input is empty/falsy. Raises ValueError on unparseable input."""
    if s is None:
        return None
    raw = s.strip().lower()
    if not raw:
        return None
    today = today_local(tz)
    if raw in ("today", "tod"):
        return today.isoformat()
    if raw in ("tomorrow", "tmrw", "tmr"):
        return (today + timedelta(days=1)).isoformat()
    if raw == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    # "in N day(s)" / "in N week(s)"
    parts = raw.split()
    if len(parts) == 3 and parts[0] == "in" and parts[2].rstrip("s") in ("day", "week"):
        try:
            n = int(parts[1])
        except ValueError:
            raise ValueError(f"could not parse due_date: {s!r}")
        unit_days = 1 if parts[2].startswith("day") else 7
        return (today + timedelta(days=n * unit_days)).isoformat()
    # ISO YYYY-MM-DD
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as e:
        raise ValueError(f"could not parse due_date: {s!r}") from e


def days_until(due: str, tz: ZoneInfo = DEFAULT_TZ) -> int:
    """Whole-day delta: positive = future, 0 = today, negative = overdue."""
    d = datetime.strptime(due, "%Y-%m-%d").date()
    return (d - today_local(tz)).days


def state_bucket(due: Optional[str], tz: ZoneInfo = DEFAULT_TZ) -> Optional[str]:
    """One of: 'overdue' | 'today' | 'soon' | 'later'. None when no due date.
    Buckets pair with the UI color thresholds: today/overdue=red, soon
    (1..7d)=amber, later (>7d)=neutral."""
    if not due:
        return None
    n = days_until(due, tz)
    if n < 0:
        return "overdue"
    if n == 0:
        return "today"
    if n <= 7:
        return "soon"
    return "later"


def display(due: Optional[str], tz: ZoneInfo = DEFAULT_TZ) -> Optional[dict]:
    """Bundle for templates / MCP responses: {due_date, days_until, bucket,
    label}. Returns None when no date is set."""
    if not due:
        return None
    n = days_until(due, tz)
    bucket = state_bucket(due, tz)
    if n < -1:
        label = f"{-n}d overdue"
    elif n == -1:
        label = "yesterday"
    elif n == 0:
        label = "today"
    elif n == 1:
        label = "tomorrow"
    else:
        label = f"in {n}d"
    return {
        "due_date": due,
        "days_until": n,
        "bucket": bucket,
        "label": label,
    }
