"""In-memory ring buffer of recent in-app VNC browser actions."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_MAX = 200
_lock = threading.Lock()


@dataclass(frozen=True)
class BrowserAction:
    at: datetime
    username: str
    step: str
    message: str


_events: deque[BrowserAction] = deque(maxlen=_MAX)


def clear_activity() -> None:
    with _lock:
        _events.clear()


def record_action(username: str, step: str, message: str) -> None:
    text = " ".join((message or "").split())
    event = BrowserAction(
        at=datetime.now(timezone.utc),
        username=(username or "browser").strip()[:80] or "browser",
        step=(step or "browser").strip()[:40] or "browser",
        message=text[:300],
    )
    with _lock:
        _events.append(event)


def recent_lines(since: timedelta) -> list[str]:
    cutoff = datetime.now(timezone.utc) - since
    with _lock:
        chosen = [item for item in _events if item.at >= cutoff]
    return [
        f"{item.at.strftime('%Y-%m-%dT%H:%M:%SZ')} {item.username} {item.step} {item.message}".rstrip()
        for item in chosen
    ]
