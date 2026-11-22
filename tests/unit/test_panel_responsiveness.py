"""Panel responsiveness: account list in one pass, login button in the background."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import event

from app.api.routes.accounts import list_accounts
from app.api.schemas import FirstLoginOut
from app.db import DailyStat


def test_list_accounts_loads_proxy_and_stats_without_per_row_queries(
    db_session, make_account, make_proxy, monkeypatch
):
    from app.api.routes import accounts as accounts_routes

    accounts_routes._session_file_cache.clear()
    proxy = make_proxy(label="listed-proxy")
    other = make_proxy(label="listed-proxy-b")
    first = make_account(username="listed-a", proxy=proxy)
    second = make_account(username="listed-b", proxy=other)
    today = datetime.now(timezone.utc).date().isoformat()
    db_session.add(DailyStat(account_id=first.id, date_key=today, posts_count=2, failures_count=1))
    db_session.add(DailyStat(account_id=second.id, date_key=today, posts_count=4, failures_count=0))
    db_session.commit()

    counts = {"stats": 0, "proxy": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        text = statement.lower()
        if "daily_stats" in text:
            counts["stats"] += 1
        if "proxies" in text and text.lstrip().startswith("select"):
            counts["proxy"] += 1

    disk = {"n": 0}
    real = accounts_routes._account_session_fields

    def _wrapped(*args, **kwargs):
        disk["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(accounts_routes, "_account_session_fields", _wrapped)
    event.listen(db_session.bind, "before_cursor_execute", _before)
    try:
        rows = list_accounts(db_session)
        again = list_accounts(db_session)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", _before)

    by_name = {row.username: row for row in rows}
    assert by_name["listed-a"].proxy_label == "listed-proxy"
    assert by_name["listed-a"].posts_today == 2
    assert by_name["listed-b"].proxy_label == "listed-proxy-b"
    assert by_name["listed-b"].posts_today == 4
    assert again[0].posts_today == rows[0].posts_today
    assert counts["stats"] == 2
    assert counts["proxy"] == 2
    assert disk["n"] == 2


def test_second_panel_login_reuses_the_running_one(db_session, make_account, monkeypatch):
    from app.accounts.login_jobs import current_panel_login, start_panel_login

    make_account(username="bglogin")
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def _login(account, db, **kwargs):
        calls["n"] += 1
        started.set()
        assert release.wait(3)
        return FirstLoginOut(run_id=kwargs["collector"].run_id, ok=True, logs=[])

    monkeypatch.setattr("app.accounts.first_login_service.run_account_first_login", _login)
    try:
        first = start_panel_login("bglogin")
        assert first.pending is True
        assert started.wait(3)
        second = start_panel_login("bglogin")
        assert second.pending is True
        assert second.run_id == first.run_id
        assert calls["n"] == 1
    finally:
        release.set()

    finished = None
    for _ in range(40):
        finished = current_panel_login("bglogin")
        if finished is not None and not finished.pending:
            break
        time.sleep(0.05)
    assert finished is not None
    assert finished.pending is False
    assert finished.ok is True
    assert calls["n"] == 1
