import asyncio
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from test_bot import commit, init, push, qq
from test_management import cli

from qbot.app import create_app
from qbot.clients import DeliveryError, QQClient
from qbot.config import Settings
from qbot.service import Worker
from qbot.store import SCHEMA, Store


@pytest.fixture
def settings(tmp_path):
    return Settings("1906010968", "fake-secret", "owner/private", "fake-token", "fake-hook", tmp_path / "db")


def message(text, user="user1", mid="message1"):
    return {"id": mid, "author": {"user_openid": user}, "content": text}


def test_private_webhook_bind_dedupe_and_identity(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        code = app.state.store.token("private")
        event = {"op": 0, "id": "delivery1", "t": "C2C_MESSAGE_CREATE",
                 "d": message("/绑定 " + code)}
        assert qq(client, settings, event, signed=False).status_code == 401
        assert qq(client, settings, event).json() == {"op": 12}
        event["id"] = "delivery2"
        qq(client, settings, event)
        assert app.state.store.status()["target_type"] == "private"
        with app.state.store.connect() as db:
            b = db.execute("SELECT * FROM binding").fetchone()
            assert (b["group_id"], b["owner"]) == ("user1", "user1")
            rows = db.execute("SELECT * FROM outbox").fetchall()
            assert len(rows) == 1 and rows[0]["target_type"] == "private"
            assert code not in rows[0]["content"]
        event["d"] = message("/状态", mid="message2")
        qq(client, settings, event)
        event["d"] = message("/状态", user="stranger", mid="message3")
        qq(client, settings, event)
        with app.state.store.connect() as db:
            rows = db.execute("SELECT content FROM outbox ORDER BY id").fetchall()
            assert "广播已启用" in rows[1][0]
            assert "仅绑定操作者" in rows[2][0]
        # Even matching OpenID strings cannot authorize a different conversation type.
        app.state.store.qq_event("group-status", "GROUP_AT_MESSAGE_CREATE", {
            "id": "group-status", "group_openid": "user1", "author": {"member_openid": "user1"},
            "content": "/状态"})
        with app.state.store.connect() as db:
            assert "仅绑定操作者" in db.execute("SELECT content FROM outbox ORDER BY id DESC").fetchone()[0]
        event["d"] = {"id": "bad", "author": {"id": "user2"}, "content": "/帮助"}
        qq(client, settings, event)
        with app.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 4


def test_private_expired_token(settings):
    store = Store(settings.database)
    code = store.token("private")
    with store.connect() as db:
        db.execute("UPDATE bind_tokens SET expires=0")
    store.qq_event("expired", "C2C_MESSAGE_CREATE", message("/绑定 " + code))
    assert not store.status()["bound"]


def test_scoped_token_and_single_destination(settings):
    store = Store(settings.database)
    code = store.token("private")
    store.qq_event("wrong", "GROUP_AT_MESSAGE_CREATE", {
        "id": "group-msg", "group_openid": "g", "author": {"member_openid": "u"},
        "content": "/绑定 " + code})
    assert not store.status()["bound"]
    store.qq_event("right", "C2C_MESSAGE_CREATE", message("/绑定 " + code))
    assert store.status()["target_type"] == "private"
    with pytest.raises(ValueError):
        store.token("group")
    store.qq_event("other", "C2C_MESSAGE_CREATE", message("/绑定 " + code, "user2", "m2"))
    with store.connect() as db:
        assert db.execute("SELECT owner FROM binding").fetchone()[0] == "user1"


@pytest.mark.parametrize("kind", ["C2C_MSG_REJECT", "FRIEND_DEL"])
def test_private_pause_does_not_match_group_openid(settings, kind):
    store = Store(settings.database)
    code = store.token("private")
    store.qq_event("bind", "C2C_MESSAGE_CREATE", message("/绑定 " + code))
    store.qq_event("group-reject", "GROUP_MSG_REJECT", {"group_openid": "user1"})
    assert store.status()["enabled"]
    store.qq_event("wrong-user", kind, {"openid": "user2"})
    assert store.status()["enabled"]
    store.qq_event("reject", kind, {"openid": "user1"})
    assert not store.status()["enabled"]
    store.qq_event("receive", "C2C_MSG_RECEIVE", {"openid": "user1"})
    assert not store.status()["enabled"]


def test_private_commit_send_and_restart(settings):
    store = init(settings)
    code = store.token("private")
    store.qq_event("bind", "C2C_MESSAGE_CREATE", message("/绑定 " + code))
    store.accept("push1", push(1, 2))
    calls = []
    def handler(request):
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 7200})
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": f"remote{len(calls)}"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            worker = Worker(settings, store, http)
            await worker.ingest_once()
            for _ in range(3):
                with store.connect() as db:
                    db.execute("DELETE FROM meta WHERE key='next_send'")
                await worker.send_once()
    asyncio.run(run())
    assert [url for url, _ in calls] == ["/v2/users/user1/messages"] * 3
    assert calls[0][1]["msg_id"] == "message1"
    assert all("msg_id" not in payload for _, payload in calls[1:])
    assert all("is_wakeup" not in payload for _, payload in calls)
    restarted = Store(settings.database)
    restarted.recover()
    assert restarted.status()["outbox"] == {"sent": 3}
    assert restarted.status()["target_type"] == "private"
    with restarted.connect() as db:
        rows = db.execute("SELECT sha,target_type FROM outbox WHERE kind='commit' ORDER BY id").fetchall()
        assert [(r[0], r[1]) for r in rows] == [(commit(n)["id"], "private") for n in (1, 2)]


def test_old_database_migration_preserves_group_and_pending(settings):
    with sqlite3.connect(settings.database) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO binding VALUES(1,'old-group','old-owner',1)")
        db.execute("INSERT INTO outbox(kind,group_id,content,created) VALUES('commit','old-group','old',0)")
        db.execute("INSERT INTO outbox(kind,content,created) VALUES('commit','unassigned',0)")
        db.execute("INSERT INTO bind_tokens VALUES('old-digest',0)")
    store = Store(settings.database)
    Store(settings.database)
    assert store.status()["target_type"] == "group"
    job = store.claim(time.time())
    assert job["group_id"] == "old-group" and job["target_type"] == "group"
    with store.connect() as db:
        assert db.execute("SELECT target_type FROM bind_tokens").fetchone()[0] == "group"
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2


def test_cli_private_token_and_safe_switch(settings, monkeypatch, capsys):
    cli(monkeypatch, settings, "bind-token", "--target", "private")
    code = capsys.readouterr().out.splitlines()[-1]
    store = Store(settings.database)
    store.qq_event("bind", "C2C_MESSAGE_CREATE", message("/绑定 " + code))
    with store.connect() as db:
        db.execute("INSERT INTO outbox(kind,group_id,content,created,target_type) "
                   "VALUES('commit','user1','private',0,'private')")
    cli(monkeypatch, settings, "unbind", "--discard-pending")
    cli(monkeypatch, settings, "bind-token", "--target", "group")
    code = capsys.readouterr().out.splitlines()[-1]
    store.qq_event("new-bind", "GROUP_AT_MESSAGE_CREATE", {
        "id": "new-message", "group_openid": "g", "author": {"member_openid": "u"},
        "content": "/绑定 " + code})
    with store.connect() as db:
        assert db.execute("SELECT state FROM outbox WHERE kind='commit'").fetchone()[0] == "skipped"
    assert store.status()["target_type"] == "group"


def test_private_permission_failure_and_rate_limit(settings):
    async def run():
        for response, expected in [(httpx.Response(200, json={"err_code": 40054013}), "failed"),
                                   (httpx.Response(200, json={"err_code": 40034100}), "pending")]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
                client = QQClient(settings, http)
                client.cached_token, client.expiry = "fake", time.time() + 100
                with pytest.raises(DeliveryError) as exc:
                    await client.send({"target_type": "private", "group_id": "u", "msg_id": None,
                                       "content": "test", "attempts": 1})
                assert exc.value.state == expected
    asyncio.run(run())
