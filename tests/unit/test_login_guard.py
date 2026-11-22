"""Login is refused before a browser starts."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException

from app.accounts.login_guard import (
    LOGIN_COOLDOWN,
    assert_login_allowed,
    probe_platform,
    record_login_outcome,
)
from app.api.schemas import AccountIn, AccountProxyUpdate
from app.behavior.warmup_playwright import FirstLoginResult
from app.db.models import AccountStatus


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _refuse_browser(monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("browser must not start")

    monkeypatch.setattr("app.accounts.first_login_service.playwright_browsers_available", lambda: True)
    monkeypatch.setattr("app.accounts.first_login_service.first_login", _boom)
    monkeypatch.setattr("app.accounts.first_login_service.storage_state_has_session", lambda username: False)


def test_unhealthy_proxy_refuses_before_browser(db_session, make_account, make_proxy, monkeypatch):
    from app.accounts.first_login_service import run_account_first_login

    proxy = make_proxy(label="sick", is_healthy=False)
    account = make_account(username="sickuser", proxy=proxy)
    _refuse_browser(monkeypatch)
    out = run_account_first_login(account, db_session)
    assert out.ok is False
    assert out.error == "Прокси не проверен или нездоров — сначала проверка прокси"


def test_unchecked_proxy_refuses(db_session, make_account, make_proxy):
    proxy = make_proxy(label="stale", checked=False)
    account = make_account(username="staleuser", proxy=proxy)
    allowance = assert_login_allowed(db_session, account, proxy, probe=False)
    assert allowance.allowed is False
    assert "не проверен" in allowance.error


def test_geo_mismatch_refuses(db_session, make_account, make_proxy):
    proxy = make_proxy(label="geo", country="US")
    proxy.last_geo_country = "DE"
    db_session.commit()
    account = make_account(username="geouser", proxy=proxy)
    allowance = assert_login_allowed(db_session, account, proxy, probe=False)
    assert allowance.allowed is False
    assert allowance.error == "Страна прокси не совпадает с фактическим выходом"


def test_shared_proxy_refuses(db_session, make_account, make_proxy):
    proxy = make_proxy(label="shared")
    make_account(username="owner", proxy=proxy)
    other = make_account(username="guest", proxy=proxy)
    allowance = assert_login_allowed(db_session, other, proxy, probe=False)
    assert allowance.allowed is False
    assert allowance.error == "Прокси уже занят аккаунтом @owner"


def test_active_cooldown_refuses(db_session, make_account, make_proxy):
    proxy = make_proxy(label="pause")
    account = make_account(username="paused", proxy=proxy, status=AccountStatus.cooldown)
    account.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=3)
    db_session.commit()
    allowance = assert_login_allowed(db_session, account, proxy, probe=False)
    assert allowance.allowed is False
    assert allowance.error.startswith("Пауза до ")
    assert "повторный вход не запускается" in allowance.error


def test_code_submission_skips_cooldown(db_session, make_account, make_proxy):
    proxy = make_proxy(label="code")
    account = make_account(username="coder", proxy=proxy, status=AccountStatus.cooldown)
    account.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=3)
    db_session.commit()
    allowance = assert_login_allowed(
        db_session,
        account,
        proxy,
        probe=False,
        submitting_code=True,
    )
    assert allowance.allowed is True


def test_probe_timeout_refuses(db_session, make_account, make_proxy, monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("app.accounts.login_guard.httpx.Client", _Client)
    proxy = make_proxy(label="dead-exit")
    account = make_account(username="tikdead", proxy=proxy, platform="tiktok")
    allowance = assert_login_allowed(db_session, account, proxy)
    assert allowance.allowed is False
    assert allowance.error == "Прокси не открывает www.tiktok.com"


def test_probe_5xx_refuses(make_proxy, monkeypatch):
    class _Response:
        status_code = 503

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return _Response()

    monkeypatch.setattr("app.accounts.login_guard.httpx.Client", _Client)
    proxy = make_proxy(label="five")
    assert probe_platform(proxy, "instagram") == "Прокси не открывает www.instagram.com"


def test_stored_surface_is_not_replaced(db_session, make_account, make_proxy, monkeypatch):
    from app.accounts.first_login_service import run_account_first_login

    proxy = make_proxy(label="surface")
    account = make_account(username="surface", proxy=proxy)
    account.login_surface = "mobile"
    db_session.commit()
    captured: dict = {}

    monkeypatch.setattr("app.accounts.first_login_service.playwright_browsers_available", lambda: True)
    monkeypatch.setattr("app.accounts.first_login_service.storage_state_has_session", lambda username: False)

    def _fake(**kwargs):
        captured.update(kwargs)
        return FirstLoginResult(ok=True, session_id="b" * 40)

    monkeypatch.setattr("app.accounts.first_login_service.first_login", _fake)
    monkeypatch.setattr(
        "app.accounts.first_login_service.import_playwright_session_to_account",
        lambda db, acc, **kwargs: True,
    )
    out = run_account_first_login(account, db_session, surface="desktop")
    assert out.ok is True
    assert captured["surface"] == "mobile"
    assert any("поверхность входа остаётся mobile" in line.message for line in out.logs)
    db_session.refresh(account)
    assert account.login_surface == "mobile"


def test_challenge_sets_twelve_hour_pause(db_session, make_account, make_proxy, monkeypatch):
    from app.accounts.first_login_service import run_account_first_login

    proxy = make_proxy(label="chal")
    account = make_account(username="chaluser", proxy=proxy)
    monkeypatch.setattr("app.accounts.first_login_service.playwright_browsers_available", lambda: True)
    monkeypatch.setattr("app.accounts.first_login_service.storage_state_has_session", lambda username: False)
    monkeypatch.setattr(
        "app.accounts.first_login_service.first_login",
        lambda **kwargs: FirstLoginResult(ok=False, challenge=True, error="checkpoint"),
    )
    out = run_account_first_login(account, db_session)
    assert out.ok is False
    assert out.challenge is True
    db_session.refresh(account)
    assert account.status == AccountStatus.cooldown
    until = _aware(account.cooldown_until)
    delta = until - datetime.now(timezone.utc)
    assert LOGIN_COOLDOWN - timedelta(minutes=5) < delta < LOGIN_COOLDOWN + timedelta(minutes=5)


def test_success_clears_pause(db_session, make_account, make_proxy):
    proxy = make_proxy(label="clear")
    account = make_account(username="clearuser", proxy=proxy, status=AccountStatus.cooldown)
    account.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=12)
    db_session.commit()
    record_login_outcome(account, db_session, ok=True)
    db_session.refresh(account)
    assert account.status == AccountStatus.active
    assert account.cooldown_until is None


def test_plain_failure_does_not_set_pause(db_session, make_account, make_proxy):
    proxy = make_proxy(label="plain")
    account = make_account(username="plainuser", proxy=proxy)
    record_login_outcome(
        account,
        db_session,
        ok=False,
        challenge=True,
        error="Headless: нужно подтверждение или ручной вход",
        arm_cooldown=False,
    )
    db_session.refresh(account)
    assert account.status == AccountStatus.active
    assert account.cooldown_until is None


def test_saved_session_does_not_set_pause(db_session, make_account, make_proxy, monkeypatch):
    from app.accounts.first_login_service import run_account_first_login

    proxy = make_proxy(label="reuse")
    account = make_account(username="reuseuser", proxy=proxy)
    account.session_id = "session-already"
    db_session.commit()
    _refuse_browser(monkeypatch)
    out = run_account_first_login(account, db_session)
    assert out.ok is True
    db_session.refresh(account)
    assert account.status == AccountStatus.active
    assert account.cooldown_until is None
    assert account.last_login_at is None


def _hours_excluding(hour: int) -> tuple[int, int]:
    if hour == 0:
        return 1, 23
    return 0, hour


def test_password_login_refuses_outside_local_hours(db_session, make_account, make_proxy, monkeypatch):
    from app.accounts import login_guard

    monkeypatch.setattr(login_guard, "outside_active_hours", login_guard._outside_active_hours)
    hour = datetime.now(timezone.utc).hour
    start, end = _hours_excluding(hour)
    proxy = make_proxy(label="night")
    account = make_account(
        username="nightuser",
        proxy=proxy,
        timezone="UTC",
        active_hour_start=start,
        active_hour_end=end,
    )
    refused = assert_login_allowed(db_session, account, proxy, probe=False)
    assert refused.allowed is False
    assert refused.error == "Сейчас вне часов аккаунта, парольный вход не запускается"
    allowed = assert_login_allowed(
        db_session,
        account,
        proxy,
        probe=False,
        submitting_code=True,
    )
    assert allowed.allowed is True


def test_password_logins_wait_five_minutes(db_session, make_account, make_proxy):
    first_proxy = make_proxy(label="gap-a")
    second_proxy = make_proxy(label="gap-b")
    first = make_account(username="gap-first", proxy=first_proxy)
    second = make_account(username="gap-second", proxy=second_proxy)
    opened = assert_login_allowed(db_session, first, first_proxy, probe=False)
    assert opened.allowed is True
    blocked = assert_login_allowed(db_session, second, second_proxy, probe=False)
    assert blocked.allowed is False
    assert blocked.error == "Парольный вход был меньше 5 минут назад"
    first.last_login_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    db_session.commit()
    again = assert_login_allowed(db_session, second, second_proxy, probe=False)
    assert again.allowed is True
    coded = assert_login_allowed(
        db_session,
        first,
        first_proxy,
        probe=False,
        submitting_code=True,
    )
    assert coded.allowed is True


def test_chromium_uses_proxy_for_webrtc_and_quic():
    from app.behavior.playwright_surface import browser_launch_args, studio_vnc_launch_args

    flag = "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
    for args in (browser_launch_args("mobile"), browser_launch_args("desktop"), studio_vnc_launch_args()):
        assert flag in args
        assert "--disable-quic" in args
        assert "--no-sandbox" in args


def test_session_rotation_waits_out_challenge_and_cooldown():
    from types import SimpleNamespace

    from app.scheduler.worker import _session_rotation_allowed

    account = SimpleNamespace(status=AccountStatus.cooldown)
    assert _session_rotation_allowed(account) is False
    account.status = AccountStatus.challenge
    assert _session_rotation_allowed(account) is False
    account.status = AccountStatus.active
    assert _session_rotation_allowed(account) is True


def test_exclusive_proxy_assignment(db_session, make_account, make_proxy):
    from app.accounts.login_guard import ProxyOccupied, ensure_proxy_exclusive
    from app.api.routes.accounts import create_account, update_account_proxy
    from app.assistant.tools import ToolError, attach_proxy

    proxy = make_proxy(label="only")
    owner = make_account(username="holder", proxy=proxy)
    other = make_proxy(label="free")
    guest = make_account(username="guest2", proxy=other)

    with pytest.raises(ProxyOccupied, match="holder"):
        ensure_proxy_exclusive(db_session, proxy.id, guest)

    with pytest.raises(ToolError, match="@holder"):
        attach_proxy(db_session, guest.id, "only")

    attach_proxy(db_session, owner.id, "only")
    db_session.refresh(owner)
    assert owner.proxy_id == proxy.id

    with pytest.raises(HTTPException) as caught:
        update_account_proxy(guest.username, AccountProxyUpdate(proxy_label="only"), db_session)
    assert caught.value.status_code == 409
    assert "@holder" in caught.value.detail

    spare = make_proxy(label="spare")
    with pytest.raises(HTTPException) as created:
        create_account(
            AccountIn(username="third", password="secret", proxy_label="only"),
            db_session,
        )
    assert created.value.status_code == 409
    db_session.rollback()
    update_account_proxy(guest.username, AccountProxyUpdate(proxy_label="spare"), db_session)
    db_session.refresh(guest)
    assert guest.proxy_id == spare.id
