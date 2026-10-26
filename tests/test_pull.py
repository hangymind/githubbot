import asyncio
import json
import time

import httpx
import pytest
from test_management import cli

from qbot.config import Settings
from qbot.service import Worker
from qbot.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings("1906010968", "fake-secret", "owner/private", "fake-token", "fake-hook", tmp_path / "db")


def make_store(config, target):
    store = Store(config.database)
    with store.connect() as db:
        db.execute("INSERT INTO binding(id,group_id,owner,enabled,target_type) VALUES(1,'g','u',0,?)",
                   (target,))
        for i in range(5):
            state, error = ("failed", "qq_40034105") if i == 0 else ("pending", None)
            db.execute("INSERT INTO outbox(kind,content,state,error,created) VALUES('commit',?,?,?,0)",
                       (f"提交{i}", state, error))
    return store


def request(store, target, mid="m", owner="u"):
    private = target == "private"
    store.qq_event(mid, "C2C_MESSAGE_CREATE" if private else "GROUP_AT_MESSAGE_CREATE", {
        "id": mid, "content": "/更新", "group_openid": "g",
        "author": {"user_openid": owner if owner != "u" else "g", "member_openid": owner}})


@pytest.mark.parametrize("target", ["group", "private"])
def test_pull_success_bounded_authorized_passive(settings, target):
    store = make_store(settings, target)
    if target == "private":
        with store.connect() as db:
            db.execute("UPDATE binding SET owner='g'")
    request(store, target, mid="stranger-message", owner="stranger")
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM pull_items").fetchone()
        db.execute("UPDATE outbox SET state='sent' WHERE kind='reply'")
    request(store, target)
    request(store, target)  # event dedupe
    request(store, target, "m2")  # no second reservation
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM pull_items").fetchone()[0] == 2
    calls = []
    def handler(r):
        calls.append(json.loads(r.content))
        return httpx.Response(200, json={"id": "remote"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            w = Worker(settings, store, http)
            w.qq.cached_token, w.qq.expiry = "fake", time.time() + 100
            await w.send_once()
    asyncio.run(run())
    assert calls[0]["msg_id"] == "m" and calls[0]["msg_seq"] == 1
    assert "提交0" in calls[0]["content"] and "提交1" in calls[0]["content"]
    assert "提交2" not in calls[0]["content"]
    with store.connect() as db:
        count = db.execute("SELECT count(*) FROM outbox WHERE kind='commit' AND state='sent'").fetchone()[0]
        assert count == 2
        assert not db.execute("SELECT 1 FROM pull_items").fetchone()


def test_pull_failure_expiration_and_uncertain(settings):
    store = make_store(settings, "group")
    request(store, "group")
    job = store.claim(time.time())
    store.finish(job["id"], "failed", "qq_40034005")
    with store.connect() as db:
        assert db.execute("SELECT state FROM outbox WHERE id=1").fetchone()[0] == "failed"
        assert not db.execute("SELECT 1 FROM pull_items").fetchone()
    request(store, "group", "m2")
    job = store.claim(time.time())
    store.recover()
    request(store, "group", "m3")
    with store.connect() as db:
        assert db.execute("SELECT state FROM outbox WHERE id=?", (job["id"],)).fetchone()[0] == "uncertain"
        assert db.execute("SELECT count(*) FROM pull_items").fetchone()[0] == 2
    store.finish(job["id"], "sent", remote_id="manually-confirmed")
    request(store, "group", "m4")
    with store.connect() as db:
        db.execute("UPDATE outbox SET expires=0 WHERE kind='pull' AND state='pending'")
    store.claim(time.time())
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM pull_items").fetchone()
        assert db.execute("SELECT state FROM outbox WHERE id=3").fetchone()[0] == "pending"


def test_pull_does_not_bypass_uncertain_or_other_errors(settings):
    store = make_store(settings, "group")
    with store.connect() as db:
        db.execute("UPDATE outbox SET state='uncertain' WHERE id=1")
    request(store, "group")
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM pull_items").fetchone()
        assert "待核查" in db.execute("SELECT content FROM outbox ORDER BY id DESC").fetchone()[0]


def test_resolve_pull_retry_requires_new_command(settings, monkeypatch):
    store = make_store(settings, "group")
    request(store, "group")
    job = store.claim(time.time())
    store.finish(job["id"], "uncertain", "timeout")
    cli(monkeypatch, settings, "resolve", str(job["id"]), "--as", "retry", "--accept-duplicate-risk")
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM pull_items").fetchone()
        assert db.execute("SELECT state FROM outbox WHERE id=?", (job["id"],)).fetchone()[0] == "expired"
        assert db.execute("SELECT state FROM outbox WHERE id=1").fetchone()[0] == "failed"
