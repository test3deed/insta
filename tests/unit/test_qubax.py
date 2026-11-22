"""Qubax client and API routes."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services import qubax as qx


def test_filter_video_models_includes_presets_and_video_slug():
    rows = [{"id": "gpt-5", "owned_by": "openai"}, {"id": "seedance-2-fast-t2v", "owned_by": "bytedance"}]
    out = qx.filter_video_models(rows)
    ids = [r["id"] for r in out]
    assert "kling-v3-standard-text-to-video" in ids
    assert "seedance-2-fast-t2v" in ids
    assert "gpt-5" not in ids


def test_duration_token():
    assert qx.duration_token(5) == "5s"
    assert qx.duration_token(10.4) == "10s"


def test_parse_duration_seconds():
    assert qx.parse_duration_seconds("5s") == 5
    assert qx.parse_duration_seconds("10") == 10


def test_estimate_video_cost_job_and_second():
    assert qx.estimate_video_cost(price=0.25, unit="job", duration="5s") == 0.25
    assert qx.estimate_video_cost(price=0.02, unit="second", duration="5s") == 0.1
    assert qx.estimate_video_cost(price=None, unit="job", duration="5s") is None


@patch("app.services.qubax.http_client")
def test_market_model_caches(mock_client_factory):
    qx._market_model_cache.clear()
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "video-gen",
        "price": 0.12,
        "unit": "job",
        "discount_pct": 50,
    }
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_factory.return_value = mock_client

    row = qx.market_model("video-gen", refresh=True)
    assert row["price"] == 0.12
    assert row["unit"] == "job"


@patch("app.services.qubax.http_client")
def test_create_video_posts_payload(mock_client_factory):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "job-1", "status": "pending"}
    mock_client.post.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_factory.return_value = mock_client

    body = qx.create_video("key", model="video-gen", prompt="test", duration="5s", aspect_ratio="9:16")
    assert body["id"] == "job-1"
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0].endswith("/video/generations")
    payload = call_kwargs[1]["json"]
    assert payload["model"] == "video-gen"
    assert payload["duration"] == "5s"


@patch("app.services.qubax.http_client")
def test_create_video_prefers_generation_id(mock_client_factory):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "request-1", "generation_id": "gen-9", "status": "pending"}
    mock_client.post.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_factory.return_value = mock_client

    body = qx.create_video("key", model="video-gen", prompt="test")
    assert body["id"] == "gen-9"


@pytest.fixture()
def auth_client(app_env, monkeypatch):
    from app.api import deps as api_deps
    from app.api.main import create_app
    from app.config import settings

    monkeypatch.setattr(settings, "admin_token", "secret-token", raising=True)
    monkeypatch.setattr(settings, "qubax_api_key", "qbx_test_key", raising=True)
    app = create_app()

    def _override_get_db():
        db = app_env.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    return __import__("fastapi.testclient").testclient.TestClient(app)


def _h():
    return {"Authorization": "Bearer secret-token"}


def test_status_without_key(app_env, monkeypatch):
    from app.api import deps as api_deps
    from app.api.main import create_app
    from app.config import settings
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "admin_token", "secret-token", raising=True)
    monkeypatch.setattr(settings, "qubax_api_key", "", raising=True)
    app = create_app()

    def _override_get_db():
        db = app_env.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    client = TestClient(app)
    r = client.get("/api/qubax/status", headers=_h())
    assert r.status_code == 200
    assert r.json()["configured"] is False


@patch("app.services.qubax.billing_balance", return_value=12.5)
def test_status_with_key(mock_bal, auth_client):
    r = auth_client.get("/api/qubax/status", headers=_h())
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is True
    assert data["balance"] == 12.5


@patch("app.services.qubax.billing_balance", return_value=12.5)
def test_status_suggested_public_origin(mock_bal, auth_client):
    r = auth_client.get(
        "/api/qubax/status",
        headers={**_h(), "x-forwarded-host": "panel.example.com", "x-forwarded-proto": "https"},
    )
    assert r.status_code == 200
    settings = r.json()["settings"]
    assert settings["suggested_api_public_base_url"] == "https://panel.example.com"
    assert settings["docs_url"] == "https://qubax.ai/docs"


@patch("app.services.qubax.billing_balance", return_value=10.0)
@patch("app.services.qubax.estimate_jobs")
def test_estimate_endpoint(mock_estimate, mock_bal, auth_client):
    mock_estimate.return_value = (
        [
            {
                "model": "video-gen",
                "unit": "job",
                "unit_price": 0.2,
                "line_cost": 0.2,
                "duration": "5s",
            }
        ],
        0.2,
        1,
    )
    r = auth_client.post(
        "/api/qubax/estimate",
        headers=_h(),
        json={"include_single": True, "model": "video-gen", "duration": "5s"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["jobs_count"] == 1
    assert data["total_usd"] == 0.2
    assert data["sufficient"] is True


@patch("app.services.qubax.create_video")
@patch("app.services.qubax.billing_balance", return_value=1.0)
def test_create_job(mock_bal, mock_create, auth_client):
    mock_create.return_value = {"id": "ext-99", "status": "processing"}
    r = auth_client.post(
        "/api/qubax/jobs",
        headers=_h(),
        json={"model": "video-gen", "prompt": "forest", "duration": "5s", "aspect_ratio": "16:9"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["external_id"] == "ext-99"
    assert data["model"] == "video-gen"


@patch("app.services.qubax.create_video")
def test_batch_jobs(mock_create, auth_client):
    mock_create.side_effect = [
        {"id": "a", "status": "pending"},
        {"id": "b", "status": "pending"},
    ]
    r = auth_client.post(
        "/api/qubax/jobs/batch",
        headers=_h(),
        json={
            "prompt": "same",
            "variants": [
                {"model": "video-gen", "duration": "5s"},
                {"model": "kling-v3-standard-text-to-video", "duration": "5s"},
            ],
        },
    )
    assert r.status_code == 200
    assert r.json()["total"] == 2


@pytest.fixture()
def auth_client_no_qubax(app_env, monkeypatch):
    from app.api import deps as api_deps
    from app.api.main import create_app
    from app.config import settings

    monkeypatch.setattr(settings, "admin_token", "secret-token", raising=True)
    monkeypatch.setattr(settings, "qubax_api_key", "", raising=True)
    app = create_app()

    def _override_get_db():
        db = app_env.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[api_deps.get_db] = _override_get_db
    return __import__("fastapi.testclient").testclient.TestClient(app)


def test_registry_has_qubax_backend():
    from app.generation.registry import _BACKENDS

    assert "qubax" in _BACKENDS


def test_billing_balance_usd_and_micros():
    with patch("app.services.qubax.http_client") as mock_factory:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"balance_usd": 15.0, "balance_micros": 15000000}
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_factory.return_value = mock_client
        assert qx.billing_balance("k") == 15.0


def test_extract_video_url_nested():
    body = {"status": "completed", "output": {"video_url": "https://cdn.example/v.mp4"}}
    assert qx.extract_video_url(body) == "https://cdn.example/v.mp4"


def test_normalize_job_reads_artifact_and_succeeded():
    body = qx.normalize_job(
        {
            "id": "request-1",
            "generation_id": "gen-9",
            "status": "succeeded",
            "result": {"artifacts": [{"url": "https://cdn.example/done.mp4"}]},
        }
    )
    assert body["id"] == "gen-9"
    assert body["status"] == "completed"
    assert body["video_url"] == "https://cdn.example/done.mp4"


def test_raise_http_402():
    response = MagicMock()
    response.status_code = 402
    response.json.side_effect = ValueError()
    response.text = ""
    with pytest.raises(qx.QubaxError) as exc:
        qx._raise_http(response, context="submit")
    assert exc.value.status_code == 402
    assert "402" in str(exc.value)


@patch("app.services.qubax.billing_balance", return_value=1.0)
def test_put_form_persists(mock_bal, auth_client, app_env):
    r = auth_client.put(
        "/api/qubax/form",
        headers=_h(),
        json={
            "mode": "t2v",
            "model": "video-gen",
            "prompt": "saved draft",
            "duration": "5s",
            "aspect_ratio": "9:16",
            "api_public_base_url": "https://panel.example.com",
        },
    )
    assert r.status_code == 200
    assert r.json()["last_form"]["prompt"] == "saved draft"
    db = app_env.SessionLocal()
    try:
        from app.panel.settings_store import get_qubax_last_form

        raw = get_qubax_last_form(db)
        assert raw is not None
        assert raw.get("api_public_base_url") == "https://panel.example.com"
    finally:
        db.close()


def test_create_job_without_key(auth_client_no_qubax):
    r = auth_client_no_qubax.post(
        "/api/qubax/jobs",
        headers=_h(),
        json={"model": "video-gen", "prompt": "x"},
    )
    assert r.status_code == 400


def test_nested_not_found_error_is_a_missing_generation():
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {
        "error": {"type": "invalid_request_error", "message": "Video generation not found."}
    }
    with pytest.raises(qx.QubaxError) as caught:
        qx._raise_http(response, context="status")
    assert str(caught.value) == "Video generation not found."
    assert qx.is_missing_generation(caught.value)


def _qubax_http(status_code: int, body: dict):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = body
    mock_response.text = ""
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


@patch("app.services.qubax.http_client")
def test_refresh_marks_missing_generation_failed(mock_client_factory, auth_client, app_env):
    from datetime import datetime, timedelta, timezone

    from app.db.models import QubaxVideoJob

    db = app_env.SessionLocal()
    fresh = QubaxVideoJob(
        external_id="fresh-1",
        model="kling-v3-standard-text-to-video",
        prompt="forest",
        status="pending",
    )
    old = QubaxVideoJob(
        external_id="gone-1",
        model="kling-v3-standard-text-to-video",
        prompt="forest",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=61),
    )
    db.add(fresh)
    db.add(old)
    db.commit()
    fresh_id = fresh.id
    old_id = old.id
    db.close()

    mock_client = _qubax_http(
        400,
        {"error": {"type": "invalid_request_error", "message": "Video generation not found."}},
    )
    mock_client_factory.return_value = mock_client

    first = auth_client.get("/api/qubax/jobs?sync_active=true", headers=_h())
    assert first.status_code == 200
    items = {job["id"]: job for job in first.json()["items"]}
    assert items[fresh_id]["status"] == "pending"
    assert not items[fresh_id]["last_error"]
    assert items[old_id]["status"] == "failed"
    assert items[old_id]["last_error"] == qx.MISSING_GENERATION_TEXT
    assert mock_client.get.call_count == 2

    second = auth_client.get("/api/qubax/jobs?sync_active=true", headers=_h())
    assert second.status_code == 200
    again = {job["id"]: job for job in second.json()["items"]}
    assert again[fresh_id]["status"] == "pending"
    assert not again[fresh_id]["last_error"]
    assert again[old_id]["status"] == "failed"
    assert again[old_id]["last_error"] == qx.MISSING_GENERATION_TEXT
    assert mock_client.get.call_count == 3


@patch("app.services.qubax.http_client")
def test_refresh_does_not_poll_a_job_already_missing(
    mock_client_factory, auth_client, app_env
):
    from app.db.models import QubaxVideoJob

    db = app_env.SessionLocal()
    row = QubaxVideoJob(
        external_id="done-1",
        model="kling-v3-standard-text-to-video",
        prompt="forest",
        status="failed",
        last_error=qx.MISSING_GENERATION_TEXT,
    )
    db.add(row)
    db.commit()
    job_id = row.id
    db.close()

    mock_client = _qubax_http(
        200,
        {"status": "completed", "video_url": "https://cdn.example/v.mp4"},
    )
    mock_client_factory.return_value = mock_client

    response = auth_client.get("/api/qubax/jobs?sync_active=true", headers=_h())
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == job_id
    assert item["status"] == "failed"
    assert item["last_error"] == qx.MISSING_GENERATION_TEXT
    assert mock_client.get.call_count == 0


@patch("app.api.routes.qubax.probe_video")
@patch("app.api.routes.qubax.download_url")
@patch("app.services.qubax.http_client")
def test_completed_job_is_saved_and_listed_as_render(
    mock_client_factory, _download, mock_probe, auth_client, app_env
):
    from app.db.models import QubaxVideoJob
    from app.services.media import Probe

    db = app_env.SessionLocal()
    row = QubaxVideoJob(
        external_id="saved-1",
        model="kling-v3-standard-text-to-video",
        prompt="forest at dawn",
        status="pending",
    )
    db.add(row)
    db.commit()
    db.close()

    mock_probe.return_value = Probe(duration_s=5, width=720, height=1280)
    mock_client = _qubax_http(
        200,
        {"status": "completed", "video_url": "https://cdn.example/v.mp4"},
    )
    mock_client_factory.return_value = mock_client

    first = auth_client.get("/api/qubax/jobs?sync_active=true", headers=_h())
    assert first.status_code == 200
    job = first.json()["items"][0]
    assert job["status"] == "completed"
    assert job["media_asset_id"]
    assert job["media_filename"].endswith(".mp4")

    renders = auth_client.get("/api/generations", headers=_h())
    assert renders.status_code == 200
    qubax_rows = [item for item in renders.json() if item["backend"] == "qubax"]
    assert len(qubax_rows) == 1
    assert qubax_rows[0]["status"] == "done"
    assert qubax_rows[0]["prompt"] == "forest at dawn"
    assert qubax_rows[0]["asset_filename"] == job["media_filename"]

    second = auth_client.get("/api/qubax/jobs?sync_active=true", headers=_h())
    assert second.status_code == 200
    again = auth_client.get("/api/generations", headers=_h())
    qubax_again = [item for item in again.json() if item["backend"] == "qubax"]
    assert len(qubax_again) == 1
    assert qubax_again[0]["id"] == qubax_rows[0]["id"]


@patch("app.api.routes.qubax.probe_video")
@patch("app.api.routes.qubax.download_url")
@patch("app.api.routes.qubax.http_client")
def test_studio_media_download_sends_the_api_key(mock_http, _download, mock_probe, auth_client, app_env):
    from app.db.models import QubaxVideoJob
    from app.services.media import Probe

    db = app_env.SessionLocal()
    row = QubaxVideoJob(
        external_id="gen-1",
        model="kling-v3-standard-text-to-video",
        prompt="ridge",
        status="completed",
        video_url="https://api.qubax.ai/studio/media/job/0",
    )
    db.add(row)
    db.commit()
    db.close()

    mock_probe.return_value = Probe(duration_s=5, width=720, height=1280)
    client = MagicMock()
    mock_http.return_value.__enter__.return_value = client
    mock_http.return_value.__exit__.return_value = False

    response = auth_client.get("/api/qubax/jobs?sync_active=true", headers=_h())
    assert response.status_code == 200
    assert response.json()["items"][0]["media_filename"].endswith(".mp4")
    assert mock_http.call_args.kwargs["headers"]["Authorization"] == "Bearer qbx_test_key"


def test_worker_keeps_polling_until_a_missing_generation_appears():
    from app.config import get_settings
    from app.generation.qubax import QubaxVideoBackend

    cfg = get_settings().model_copy(
        update={"generation_timeout_seconds": 30, "generation_poll_seconds": 0}
    )
    backend = QubaxVideoBackend(cfg)
    missing = qx.QubaxError("Video generation not found.", status_code=400, missing=True)
    calls = {"n": 0}

    def side_effect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise missing
        return {"status": "completed", "video_url": "https://cdn.example/v.mp4"}

    with patch("app.services.qubax.get_video_job", side_effect=side_effect):
        body = backend._await_result("key", "late-1", lambda _progress: None)
    assert body["status"] == "completed"
    assert calls["n"] == 2


def test_worker_fails_a_missing_generation_after_the_timeout():
    from app.config import get_settings
    from app.generation.base import GenerationError
    from app.generation.qubax import QubaxVideoBackend

    cfg = get_settings().model_copy(
        update={"generation_timeout_seconds": 0, "generation_poll_seconds": 0}
    )
    backend = QubaxVideoBackend(cfg)
    missing = qx.QubaxError("Video generation not found.", status_code=400, missing=True)
    with patch("app.services.qubax.get_video_job", side_effect=missing):
        with pytest.raises(GenerationError) as caught:
            backend._await_result("key", "gone-1", lambda _progress: None)
    assert caught.value.retryable is False
    assert qx.MISSING_GENERATION_TEXT in str(caught.value)


def test_qubax_activity_redacts_the_key_and_keeps_the_exchange():
    from datetime import timedelta

    from app.services.qubax_activity import clear_activity, recent_lines, record_exchange

    clear_activity()
    record_exchange(
        "GET",
        "/video/generations/stored-1",
        400,
        "Bearer secret-key-value Video generation not found.",
        api_key="secret-key-value",
    )
    lines = recent_lines(timedelta(minutes=5))
    assert len(lines) == 1
    assert "secret-key-value" not in lines[0]
    assert "Bearer …" in lines[0]
    assert "GET /video/generations/stored-1 400" in lines[0]

