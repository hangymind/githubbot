import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from qbot.app import create_app
from qbot.clients import DeliveryError, GitHubClient, QQClient
from qbot.config import Settings
from qbot.security import qq_key
from qbot.service import Worker, format_commit
from qbot.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings("1906010968", "test-secret-not-real", "owner/private",
                    "github-test-token", "webhook-test-secret", tmp_path / "test.sqlite3")


def commit(n):
    return {"id": f"{n:040x}", "message": f"commit {n}\nprivate details",
            "author": {"name": "测试作者", "email": "private@example.invalid"},
            "timestamp": "2026-10-26T12:00:00+08:00"}


def push(*numbers):
    return {"repository": {"full_name": "owner/private"}, "before": "a" * 40,
            "after": f"{numbers[-1]:040x}" if numbers else "b" * 40,
            "ref": "refs/heads/main", "commits": [commit(n) for n in numbers]}


def gh(client, settings, data, delivery="delivery-1", event="push"):
    raw = json.dumps(data).encode()
    sig = hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/github", content=raw, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": "sha256=" + sig})


def qq(client, settings, data, signed=True):
    raw = json.dumps(data).encode()
    headers = {"X-Bot-Appid": settings.app_id}
    if signed:
        ts = str(int(time.time()))
        headers.update({"X-Signature-Timestamp": ts, "X-Signature-Ed25519":
                        qq_key(settings.app_secret).sign(ts.encode() + raw).hex()})
    return client.post("/webhooks/qq", content=raw, headers=headers)


def init(settings):
    store = Store(settings.database)
    with store.connect() as db:
        db.execute("INSERT INTO meta VALUES('baseline_repository',?)", (settings.repository.lower(),))
    return store


def bind(store):
    code = store.token()
    store.qq_event("bind-event", "GROUP_AT_MESSAGE_CREATE", {
        "group_openid": "group1", "id": "message1",
        "author": {"member_openid": "owner1"}, "content": "/绑定 " + code})
    with store.connect() as db:
        db.execute("UPDATE outbox SET state='sent' WHERE kind='reply'")
    return code


def test_github_auth_filter_dedupe_and_baseline(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.post("/webhooks/github", json=push(1)).status_code == 401
        assert gh(client, settings, push(1)).status_code == 503
        with app.state.store.connect() as db:
            db.execute("INSERT INTO meta VALUES('baseline_repository','owner/private')")
        assert gh(client, settings, push(1, 2)).json() == {"accepted": True}
        assert gh(client, settings, push(1, 2)).json() == {"accepted": False}
        other = push(3)
        other["repository"]["full_name"] = "owner/other"
        assert gh(client, settings, other).status_code == 403
        assert gh(client, settings, push(), event="ping").status_code == 200
        p = push(4)
        p["deleted"] = True
        assert gh(client, settings, p).json() == {"ignored": True}
        p["deleted"] = False
        p["ref"] = "refs/tags/v1"
        assert gh(client, settings, p).json() == {"ignored": True}
        with app.state.store.connect() as db:
            payload = db.execute("SELECT payload FROM inbox").fetchone()[0]
            assert "private@example" not in payload


def test_official_challenge_vector(settings):
    settings = replace(settings, app_secret="DG5g3B4j9X2KOErG")
    with TestClient(create_app(settings, background=False)) as client:
        r = qq(client, settings, {"op": 13, "d": {"plain_token": "Arq0D5A61EgUu4OxUvOp",
                                                     "event_ts": "1725442341"}}, signed=False)
        assert r.status_code == 200
        assert r.json()["signature"] == (
            "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01b"
            "cfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706")


def test_qq_auth_binding_dedupe_and_status_privacy(settings):
    app = create_app(settings, background=False)
    with TestClient(app) as client:
        code = app.state.store.token()
        event = {"op": 0, "id": "event1", "t": "GROUP_AT_MESSAGE_CREATE",
                 "d": {"id": "msg1", "group_openid": "group1",
                       "author": {"member_openid": "owner1"}, "content": "<@123> /绑定 " + code}}
        assert qq(client, settings, event, signed=False).status_code == 401
        assert qq(client, settings, event).json() == {"op": 12}
        event["id"] = "event2"
        assert qq(client, settings, event).status_code == 200
        with app.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
            assert code not in json.dumps([tuple(r) for r in db.execute("SELECT * FROM outbox")])
        event["d"].update(id="msg2", content="/状态", author={"member_openid": "stranger"})
        qq(client, settings, event)
        with app.state.store.connect() as db:
            assert "仅绑定操作者" in db.execute("SELECT content FROM outbox ORDER BY id DESC").fetchone()[0]
        event["d"].update(id="msg3", content="/绑定 " + code, group_openid="other-group")
        qq(client, settings, event)
        with app.state.store.connect() as db:
            assert db.execute("SELECT group_id FROM binding").fetchone()[0] == "group1"


def test_token_expiry_and_single_use(settings):
    store = init(settings)
    code = store.token()
    with store.connect() as db:
        db.execute("UPDATE bind_tokens SET expires=0")
    data = {"group_openid": "g", "id": "m", "author": {"member_openid": "u"},
            "content": "/绑定 " + code}
    store.qq_event("e", "GROUP_AT_MESSAGE_CREATE", data)
    assert not store.status()["bound"]
    bind(store)
    with pytest.raises(ValueError):
        store.token()


def test_ingestion_dedupe_force_merge_and_restart(settings):
    store = init(settings)
    bind(store)
    with store.connect() as db:
        db.execute("INSERT INTO commits VALUES(?,?,?)", (settings.repository, commit(1)["id"], "baseline"))
    store.accept("one", push(1, 2, 3))
    p = push(2, 4)
    p.update(ref="refs/heads/feature", forced=True)
    store.accept("two", p)
    async def run():
        async with httpx.AsyncClient() as http:
            worker = Worker(settings, store, http)
            await worker.ingest_once()
            await worker.ingest_once()
    asyncio.run(run())
    with store.connect() as db:
        rows = db.execute("SELECT * FROM outbox WHERE kind='commit' ORDER BY id").fetchall()
        assert [r["sha"] for r in rows] == [commit(n)["id"] for n in (2, 3, 4)]
        assert "分支：main" in rows[0]["content"]
        assert "private details" not in rows[0]["content"]
        db.execute("UPDATE outbox SET state='sending' WHERE id=?", (rows[0]["id"],))
    restarted = Store(settings.database)
    restarted.recover()
    assert restarted.status()["outbox"]["uncertain"] == 1
    assert restarted.claim(time.time()) is None


def test_commit_format(settings):
    c = commit(1)
    c["message"] = "x" * 10000 + "\nsecret body"
    assert len(format_commit(settings, c, "main")) < 600
    assert "https://" not in format_commit(settings, c, "main")
    assert "https://github.com/" in format_commit(replace(settings, include_url=True), c, "main")


def test_send_success_cache_refresh_rate_and_business_error(settings):
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "test-token", "expires_in": "7200"})
            assert request.headers["Authorization"] == "QQBot test-token"
            assert "msg_id" not in json.loads(request.content)
            return httpx.Response(200, json={"id": "sent-1"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            qqc = QQClient(settings, http)
            job = {"content": "test", "group_id": "g", "msg_id": None, "attempts": 1}
            assert await qqc.send(job) == "sent-1"
            await qqc.send(job)
            assert calls.count("/app/getAppAccessToken") == 1
            qqc.expiry = 0
            await qqc.send(job)
            assert calls.count("/app/getAppAccessToken") == 2
        for response, expected in [(httpx.Response(200, json={"err_code": 40034100}), "pending"),
                                   (httpx.Response(200, json={"err_code": 40034105}), "failed"),
                                   (httpx.Response(500, json={"code": 1}), "uncertain"),
                                   (httpx.Response(200, json={}), "uncertain")]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
                qqc = QQClient(settings, http)
                qqc.cached_token, qqc.expiry = "cached", time.time() + 100
                with pytest.raises(DeliveryError) as e:
                    await qqc.send(job)
                assert e.value.state == expected
    asyncio.run(run())


def test_timeout_no_automatic_retry_and_token_error(settings):
    async def run():
        def handler(r):
            raise httpx.ReadTimeout("sensitive content must not be logged", request=r)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            q = QQClient(settings, http)
            q.cached_token, q.expiry = "cached", time.time() + 100
            with pytest.raises(DeliveryError) as e:
                await q.send({"content": "test", "group_id": "g", "msg_id": None, "attempts": 1})
            assert e.value.state == "uncertain"
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"code": 100016}))) as http:
            with pytest.raises(DeliveryError) as e:
                await QQClient(settings, http).token()
            assert e.value.state == "failed"
    asyncio.run(run())


def test_large_push_pagination_and_incomplete(settings):
    p = push(*range(1, 2049))
    p["after"] = commit(2050)["id"]
    async def run():
        def handler(r):
            page = int(r.url.params["page"])
            numbers = range((page - 1) * 100 + 1, min(page * 100, 2050) + 1)
            cs = [{"sha": commit(n)["id"], "commit": {"message": "m",
                   "author": {"name": "a"}, "committer": {"date": "today"}}} for n in numbers]
            return httpx.Response(200, json={"total_commits": 2050, "commits": cs})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert len(await GitHubClient(settings, http).commits(p)) == 2050
            with pytest.raises(ValueError, match="large_new_branch"):
                await GitHubClient(settings, http).commits({**p, "created": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json={"total_commits": 2050, "commits": []}))) as http:
            with pytest.raises(ValueError, match="incomplete"):
                await GitHubClient(settings, http).commits(p)
    asyncio.run(run())


def test_failed_ingestion_retained_and_blocks_next(settings):
    store = init(settings)
    p = push(1)
    p["commits"][0]["id"] = "invalid"
    store.accept("bad", p)
    store.accept("good", push(2))
    async def run():
        async with httpx.AsyncClient() as http:
            w = Worker(settings, store, http)
            await w.ingest_once()
            await w.ingest_once()
    asyncio.run(run())
    assert store.status()["inbox"] == {"failed": 1, "pending": 1}


def test_worker_spacing_restart_and_logging(settings, caplog):
    store = init(settings)
    bind(store)
    store.accept("one", push(1, 2))
    def handler(r):
        if r.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "sensitive-token", "expires_in": 7200})
        return httpx.Response(200, json={"id": "sent"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            w = Worker(settings, store, http)
            await w.ingest_once()
            await w.send_once()
            await w.send_once()
    asyncio.run(run())
    assert store.status()["outbox"]["pending"] == 1
    assert "sensitive-token" not in caplog.text
    store.recover()
    assert store.claim(time.time()) is None
