import asyncio
import json
import sys
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from qbot.app import create_app
from qbot.cli import baseline, main
from qbot.config import Settings
from qbot.service import Worker
from qbot.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings("1906010968", "fake-secret", "owner/private", "fake-token", "fake-hook", tmp_path / "db")


def cli(monkeypatch, settings, *args):
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["qbot", *args])
    main()


def test_baseline_atomic_and_no_history_notifications(settings, monkeypatch):
    store = Store(settings.database)
    real_client = httpx.AsyncClient
    def handler(r):
        if r.url.path.endswith("branches"):
            return httpx.Response(200, json=[{"commit": {"sha": "a" * 40}}])
        return httpx.Response(200, json=[{"sha": "a" * 40}, {"sha": "b" * 40}])
    monkeypatch.setattr("qbot.cli.httpx.AsyncClient", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(handler)))
    asyncio.run(baseline(settings, store))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        row = db.execute("SELECT value FROM meta WHERE key='baseline_repository'").fetchone()
        assert row[0] == "owner/private"


def test_baseline_api_failure_does_not_activate(settings, monkeypatch):
    store = Store(settings.database)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("qbot.cli.httpx.AsyncClient", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "private"}))))
    with pytest.raises(ValueError, match="403"):
        asyncio.run(baseline(settings, store))
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM meta WHERE key='baseline_repository'").fetchone()


def test_cli_retry_uncertain_resolution_backup_and_unbind(settings, monkeypatch, tmp_path):
    store = Store(settings.database)
    with store.connect() as db:
        db.execute("INSERT INTO binding VALUES(1,'g','u',1)")
        db.execute("INSERT INTO outbox(kind,content,state,created) VALUES('commit','private','uncertain',0)")
    with pytest.raises(SystemExit):
        cli(monkeypatch, settings, "retry", "1")
    with pytest.raises(SystemExit):
        cli(monkeypatch, settings, "resolve", "1", "--as", "retry")
    cli(monkeypatch, settings, "resolve", "1", "--as", "sent")
    cli(monkeypatch, settings, "backup", str(tmp_path / "backup.sqlite3"))
    assert (tmp_path / "backup.sqlite3").exists()
    cli(monkeypatch, settings, "unbind", "--discard-pending")
    assert not store.status()["bound"]


def test_exclusive_service_lock_and_config_repo_guard(settings, monkeypatch):
    app = create_app(settings, background=False)
    with TestClient(app):
        with pytest.raises(RuntimeError, match="一个"):
            with TestClient(create_app(settings, background=False)):
                pass
        with pytest.raises(SystemExit):
            cli(monkeypatch, settings, "resume")
    with app.state.store.connect() as db:
        db.execute("INSERT INTO meta VALUES('baseline_repository','owner/other')")
    with pytest.raises(RuntimeError, match="仓库与数据库"):
        with TestClient(create_app(settings, background=False)):
            pass


def test_worker_failed_send_retains_and_redacts(settings, caplog):
    store = Store(settings.database)
    with store.connect() as db:
        db.execute("INSERT INTO binding VALUES(1,'g','u',1)")
        db.execute("INSERT INTO outbox(kind,content,created) VALUES('commit','private-message-body',0)")
    def handler(r):
        return httpx.Response(200, json={"err_code": 40034105, "message": "private-message-body"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            worker = Worker(settings, store, http)
            worker.qq.cached_token, worker.qq.expiry = "sensitive-token", time.time() + 100
            await worker.send_once()
    asyncio.run(run())
    assert store.status()["outbox"] == {"failed": 1}
    assert "40034105" in caplog.text
    assert "private-message-body" not in caplog.text
    assert "sensitive-token" not in caplog.text


def test_json_validation_payload_limits_and_qt_pause(settings):
    with TestClient(create_app(settings, background=False)) as client:
        assert client.post("/webhooks/qq", content="{}" * 600000).status_code == 413
        assert client.post("/webhooks/qq", content="not-json", headers={
            "X-Bot-Appid": settings.app_id}).status_code == 400
    store = Store(settings.database)
    with store.connect() as db:
        db.execute("INSERT INTO binding VALUES(1,'g','u',1)")
    store.qq_event("reject", "GROUP_MSG_REJECT", {"group_openid": "g"})
    assert not store.status()["enabled"]
    assert "group" not in json.dumps(store.status())


def test_repo_changed_rejected_before_worker(settings):
    store = Store(settings.database)
    with store.connect() as db:
        db.execute("INSERT INTO meta VALUES('baseline_repository','owner/private')")
    with pytest.raises(RuntimeError):
        with TestClient(create_app(replace(settings, repository="owner/another"), background=False)):
            pass


def test_retry_removes_disabled_commit_link(settings, monkeypatch):
    store = Store(settings.database)
    sha = "a" * 40
    with store.connect() as db:
        db.execute("INSERT INTO outbox(kind,content,sha,state,created) VALUES('commit',?,?,'failed',0)",
                   (f"标题：hello\nhttps://github.com/owner/private/commit/{sha}", sha))
    cli(monkeypatch, settings, "retry", "1")
    with store.connect() as db:
        row = db.execute("SELECT content,state FROM outbox WHERE id=1").fetchone()
        assert row["content"] == "标题：hello"
        assert row["state"] == "pending"
