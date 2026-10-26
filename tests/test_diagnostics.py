import asyncio
import json
import time

import httpx
import pytest

from qbot.config import Settings
from qbot.service import Worker
from qbot.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings("1906010968", "secret-not-for-output", "owner/private", "gh-private-token",
                    "hook-private-secret", tmp_path / "db")


def setup(settings, target="private"):
    store = Store(settings.database)
    code = store.token(target)
    event = "C2C_MESSAGE_CREATE" if target == "private" else "GROUP_AT_MESSAGE_CREATE"
    data = {"id": "bind", "content": "/绑定 " + code,
            "author": {"user_openid": "u", "member_openid": "u"}, "group_openid": "g"}
    store.qq_event("bind", event, data)
    with store.connect() as db:
        db.execute("UPDATE outbox SET state='sent'")
        # A failed commit must not block the diagnostic reply.
        db.execute("INSERT INTO outbox(kind,content,group_id,state,error,created,target_type) "
                   "VALUES('commit','private-content',?,'failed','qq_40034105',0,?)",
                   ("u" if target == "private" else "g", target))
    return store, event, data


@pytest.mark.parametrize("target", ["group", "private"])
def test_active_denial_still_reports_passive_success(settings, target, caplog):
    store, event, data = setup(settings, target)
    data.update(id="diagnostic", content="<@123> /测试 主动")
    store.qq_event("diag", event, data)
    sent = []
    def handler(request):
        if request.url.host == "api.github.com":
            assert request.url.params["per_page"] == "1"
            return httpx.Response(200, json=[{"private": "never-output"}])
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "private-access-token", "expires_in": 7200})
        payload = json.loads(request.content)
        assert request.headers["X-Union-Appid"] == settings.app_id
        assert request.headers["Content-Type"] == "application/json; charset=utf-8"
        assert request.headers["Authorization"] == "QQBot private-access-token"
        sent.append(payload)
        collection = "users/u" if target == "private" else "groups/g"
        assert request.url.path == f"/v2/{collection}/messages"
        if "msg_id" not in payload:
            assert set(payload) == {"content", "msg_type"}
            assert payload["msg_type"] == 0
            return httpx.Response(200, json={"err_code": 40034105, "message": settings.app_secret},
                                  headers={"X-Tps-trace-ID": "trace-1234"})
        return httpx.Response(200, json={"id": "report-sent"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            worker = Worker(settings, store, http)
            await worker.send_once()
            with store.connect() as db:
                db.execute("DELETE FROM meta WHERE key='next_send'")
            await worker.send_once()
    asyncio.run(run())
    assert len(sent) == 2
    report = sent[1]["content"]
    assert sent[1]["msg_id"] == "diagnostic"
    assert "40034105" in report and "trace-1234" in report
    assert "GitHub 读取提交：HTTP 200" in report
    assert "不代表主动权限" in report
    assert "X-Union-Appid=1906010968" in report
    assert "不携带 msg_id/event_id" in report
    assert "是接收事件，不是主动发送授权" in report
    with store.connect() as db:
        audit = " ".join(r[0] for r in db.execute("SELECT details FROM api_diagnostics"))
        assert db.execute("SELECT state FROM outbox WHERE kind='diagnostic'").fetchone()[0] == "sent"
    for secret in (settings.app_secret, settings.github_token, settings.webhook_secret,
                   settings.repository, "private-access-token", "private-content", "never-output"):
        assert secret not in report + audit + caplog.text


def test_test_command_access_control_and_cooldown(settings):
    store, event, data = setup(settings)
    store.qq_event("stranger", event, {**data, "id": "s", "content": "/测试 主动",
                                      "author": {"user_openid": "stranger"}})
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM outbox WHERE kind='probe'").fetchone()
    store.qq_event("diag1", event, {**data, "id": "d1", "content": "/测试"})
    store.qq_event("diag2", event, {**data, "id": "d2", "content": "/测试 主动"})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE kind='diagnostic'").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM outbox WHERE kind='probe'").fetchone()
        assert "冷却" in db.execute("SELECT content FROM outbox ORDER BY id DESC").fetchone()[0]


def test_probe_rate_limit_does_not_hide_report(settings):
    store, event, data = setup(settings)
    store.qq_event("diag", event, {**data, "id": "d", "content": "/测试 主动"})
    def handler(request):
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 7200})
        return httpx.Response(429, json={"err_code": 40034100})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await Worker(settings, store, http).send_once()
    asyncio.run(run())
    with store.connect() as db:
        assert db.execute("SELECT state FROM outbox WHERE kind='probe'").fetchone()[0] == "failed"
        db.execute("DELETE FROM meta WHERE key='next_send'")
    assert store.claim(time.time())["kind"] == "diagnostic"
