"""In-memory ring buffer of recent Qubax video API calls."""
from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_MAX = 200
_lock = threading.Lock()
_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


@dataclass(frozen=True)
class QubaxExchange:
    at: datetime
    method: str
    path: str
    status: int
    detail: str


_events: deque[QubaxExchange] = deque(maxlen=_MAX)


def clear_activity() -> None:
    with _lock:
        _events.clear()


def redact_secrets(text: str, api_key: str = "") -> str:
    cleaned = (text or "").replace("\r", " ").replace("\n", " ")
    key = (api_key or "").strip()
    if len(key) >= 8 and key in cleaned:
        cleaned = cleaned.replace(key, "…")
    return _BEARER.sub("Bearer …", cleaned)


def record_exchange(
    method: str,
    path: str,
    status: int,
    detail: str,
    *,
    api_key: str = "",
) -> None:
    event = QubaxExchange(
        at=datetime.now(timezone.utc),
        method=(method or "GET").upper()[:16],
        path=(path or "/")[:240],
        status=int(status),
        detail=redact_secrets(detail, api_key)[:500],
    )
    with _lock:
        _events.append(event)


def recent_lines(since: timedelta) -> list[str]:
    cutoff = datetime.now(timezone.utc) - since
    with _lock:
        chosen = [item for item in _events if item.at >= cutoff]
    return [
        f"{item.at.strftime('%Y-%m-%dT%H:%M:%SZ')} {item.method} {item.path} {item.status} {item.detail}".rstrip()
        for item in chosen
    ]
