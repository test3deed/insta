"""Background password login started from the panel button."""
from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy import select

from app.api.schemas import DiagnosticLogLineOut, FirstLoginOut
from app.diagnostics.collector import DiagnosticCollector

_lock = threading.Lock()


class AccountMissing(Exception):
    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(username)


@dataclass
class _LoginJob:
    username: str
    run_id: int
    collector: DiagnosticCollector
    thread: threading.Thread
    result: FirstLoginOut | None = None


_jobs: dict[str, _LoginJob] = {}


def _platform(account) -> str:
    platform = getattr(account, "platform", None)
    if platform is None:
        return "instagram"
    return getattr(platform, "value", platform) or "instagram"


def _pending(job: _LoginJob) -> FirstLoginOut:
    return FirstLoginOut(
        run_id=job.run_id,
        ok=False,
        pending=True,
        logs=[DiagnosticLogLineOut(**line.__dict__) for line in job.collector.lines],
    )


def _failed(job: _LoginJob, message: str) -> FirstLoginOut:
    return FirstLoginOut(
        run_id=job.run_id,
        ok=False,
        pending=False,
        error=message,
        logs=[DiagnosticLogLineOut(**line.__dict__) for line in job.collector.lines],
    )


def start_panel_login(username: str, *, surface: str | None = None) -> FirstLoginOut:
    """Start one login thread per username. A second call reuses the running one."""
    with _lock:
        current = _jobs.get(username)
        if current is not None and current.thread.is_alive():
            return current.result if current.result is not None else _pending(current)

        from app.db import Account, DiagnosticRunKind, SessionLocal

        db = SessionLocal()
        try:
            account = db.scalar(select(Account).where(Account.username == username))
            if account is None:
                raise AccountMissing(username)
            platform = _platform(account)
            kind = (
                DiagnosticRunKind.studio_login
                if platform in ("youtube", "tiktok")
                else DiagnosticRunKind.first_login
            )
            collector = DiagnosticCollector(db, kind=kind, account_id=account.id)
            account_id = account.id
        except Exception:
            db.close()
            raise  # noqa: BLE001 - close the session, then surface the original error
        job = _LoginJob(
            username=username,
            run_id=collector.run_id,
            collector=collector,
            thread=threading.current_thread(),
        )

        def work() -> None:
            try:
                row = db.get(Account, account_id)
                if row is None:
                    job.result = _failed(job, f"Аккаунт «{username}» не найден")
                    return
                if platform in ("youtube", "tiktok"):
                    from app.accounts.studio_login_service import run_studio_login

                    out = run_studio_login(row, db, collector=collector)
                else:
                    from app.accounts.first_login_service import run_account_first_login

                    out = run_account_first_login(row, db, surface=surface, collector=collector)
                from app.accounts.account_hints import enrich_first_login_out

                finished = enrich_first_login_out(out, row)
                job.result = finished.model_copy(update={"pending": False})
            except Exception as exc:  # noqa: BLE001 - the panel must see a finished result
                job.result = _failed(job, str(exc))
            finally:
                db.close()

        thread = threading.Thread(target=work, name=f"login-{username}", daemon=True)
        job.thread = thread
        _jobs[username] = job
        thread.start()
        return job.result if job.result is not None else _pending(job)


def current_panel_login(username: str) -> FirstLoginOut | None:
    with _lock:
        job = _jobs.get(username)
        if job is None:
            return None
        if job.result is not None:
            return job.result
        if job.thread.is_alive():
            return _pending(job)
        return _failed(job, "Вход прервался")
