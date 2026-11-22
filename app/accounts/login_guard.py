"""Refuse a password login before a browser starts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Account, AccountStatus, Proxy
from app.geo.country_codes import countries_mismatch
from app.proxies.pool import normalize_proxy_url

PROXY_CHECK_MAX_AGE = timedelta(hours=24)
LOGIN_COOLDOWN = timedelta(hours=12)
PASSWORD_LOGIN_GAP = timedelta(minutes=5)
PROBE_TIMEOUT_S = 10.0

_PLATFORM_URLS = {
    "instagram": "https://www.instagram.com/",
    "youtube": "https://www.youtube.com/",
    "tiktok": "https://www.tiktok.com/",
}

PROXY_NOT_READY = "Прокси не проверен или нездоров — сначала проверка прокси"
GEO_MISMATCH = "Страна прокси не совпадает с фактическим выходом"
OUTSIDE_HOURS = "Сейчас вне часов аккаунта, парольный вход не запускается"
LOGIN_TOO_SOON = "Парольный вход был меньше 5 минут назад"


class ProxyOccupied(Exception):
    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(f"Прокси уже занят аккаунтом @{username}")


@dataclass
class LoginAllowance:
    allowed: bool
    error: str = ""
    surface: str | None = None
    surface_note: str = ""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _platform(account: Account) -> str:
    platform = getattr(account, "platform", None)
    if platform is None:
        return "instagram"
    return getattr(platform, "value", platform) or "instagram"


def other_account_on_proxy(db: Session, proxy_id: int, account: Account) -> Account | None:
    stmt = select(Account).where(Account.proxy_id == proxy_id)
    if account.id is not None:
        stmt = stmt.where(Account.id != account.id)
    return db.scalars(stmt).first()


def ensure_proxy_exclusive(db: Session, proxy_id: int, account: Account) -> None:
    other = other_account_on_proxy(db, proxy_id, account)
    if other is not None:
        raise ProxyOccupied(other.username)


def locked_login_surface(account: Account, requested: str | None) -> tuple[str | None, str]:
    """Keep a stored Instagram surface. A different login argument is ignored."""
    stored = account.login_surface if account.login_surface in ("mobile", "desktop") else None
    asked = requested if requested in ("mobile", "desktop") else None
    if stored and asked and stored != asked:
        return stored, f"поверхность входа остаётся {stored}"
    if stored:
        return stored, ""
    return asked, ""


def _account_tz(account: Account) -> ZoneInfo:
    try:
        return ZoneInfo(account.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def outside_active_hours(account: Account, *, now: datetime | None = None) -> bool:
    """True when the local hour is outside [active_hour_start, active_hour_end)."""
    moment = (now or _now()).astimezone(_account_tz(account))
    start = int(account.active_hour_start)
    end = int(account.active_hour_end)
    return not (start <= moment.hour < end)


_outside_active_hours = outside_active_hours


def password_login_too_soon(db: Session, *, now: datetime | None = None) -> bool:
    latest = db.scalar(select(func.max(Account.last_login_at)))
    if latest is None:
        return False
    moment = now or _now()
    return moment - _as_utc(latest) < PASSWORD_LOGIN_GAP


def mark_password_login(account: Account, db: Session, *, now: datetime | None = None) -> None:
    account.last_login_at = now or _now()
    db.add(account)
    db.commit()


def probe_platform(proxy: Proxy, platform: str) -> str | None:
    """Return an error when the proxy cannot open the platform host."""
    url = _PLATFORM_URLS.get(platform, _PLATFORM_URLS["instagram"])
    host = urlparse(url).netloc
    try:
        with httpx.Client(
            proxy=normalize_proxy_url(proxy.url),
            timeout=PROBE_TIMEOUT_S,
            transport=httpx.HTTPTransport(retries=0),
            follow_redirects=False,
        ) as client:
            response = client.get(url)
    except (httpx.HTTPError, OSError):
        return f"Прокси не открывает {host}"
    if response.status_code >= 500:
        return f"Прокси не открывает {host}"
    return None


def assert_login_allowed(
    db: Session,
    account: Account,
    proxy: Proxy,
    *,
    surface: str | None = None,
    submitting_code: bool = False,
    probe: bool = True,
) -> LoginAllowance:
    """Checks that must pass before Chromium starts. Session reuse stays outside."""
    if not proxy.is_healthy or proxy.last_checked_at is None:
        return LoginAllowance(False, PROXY_NOT_READY)
    checked = _as_utc(proxy.last_checked_at)
    if _now() - checked > PROXY_CHECK_MAX_AGE:
        return LoginAllowance(False, PROXY_NOT_READY)
    if countries_mismatch(proxy.country, proxy.last_geo_country):
        return LoginAllowance(False, GEO_MISMATCH)
    other = other_account_on_proxy(db, proxy.id, account)
    if other is not None:
        return LoginAllowance(False, f"Прокси уже занят аккаунтом @{other.username}")
    if (
        not submitting_code
        and account.status == AccountStatus.cooldown
        and account.cooldown_until is not None
        and _as_utc(account.cooldown_until) > _now()
    ):
        until = _as_utc(account.cooldown_until).strftime("%Y-%m-%d %H:%M UTC")
        return LoginAllowance(False, f"Пауза до {until}, повторный вход не запускается")
    if not submitting_code and outside_active_hours(account):
        return LoginAllowance(False, OUTSIDE_HOURS)
    if not submitting_code and password_login_too_soon(db):
        return LoginAllowance(False, LOGIN_TOO_SOON)
    if probe:
        probed = probe_platform(proxy, _platform(account))
        if probed:
            return LoginAllowance(False, probed)
    locked, note = locked_login_surface(account, surface)
    if not submitting_code:
        mark_password_login(account, db)
    return LoginAllowance(True, surface=locked, surface_note=note)


def _challenge_text(error: str | None) -> bool:
    blob = (error or "").lower()
    return "checkpoint" in blob or "two_step_verification" in blob


def _infrastructure(error: str | None) -> bool:
    blob = (error or "").lower()
    return (
        "не установ" in blob
        or "отключ" in blob
        or "chromium не установлен" in blob
    )


def apply_challenge_cooldown(account: Account, db: Session, *, now: datetime | None = None) -> datetime:
    moment = now or _now()
    until = moment + LOGIN_COOLDOWN
    account.status = AccountStatus.cooldown
    account.cooldown_until = until
    db.add(account)
    db.commit()
    return until


def clear_login_cooldown(account: Account, db: Session) -> None:
    if account.status != AccountStatus.cooldown and account.cooldown_until is None:
        return
    account.cooldown_until = None
    if account.status == AccountStatus.cooldown:
        account.status = AccountStatus.active
    db.add(account)
    db.commit()


def record_login_outcome(
    account: Account,
    db: Session,
    *,
    ok: bool,
    challenge: bool = False,
    error: str | None = None,
    arm_cooldown: bool | None = None,
) -> None:
    """Success clears a login pause. An unanswered challenge sets 12 hours."""
    if ok:
        clear_login_cooldown(account, db)
        return
    if _infrastructure(error):
        return
    if _challenge_text(error):
        apply_challenge_cooldown(account, db)
        return
    if arm_cooldown is False:
        return
    if arm_cooldown or challenge:
        apply_challenge_cooldown(account, db)
