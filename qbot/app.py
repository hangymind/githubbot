import asyncio
import contextlib
import json
import logging
import sqlite3
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from filelock import FileLock, Timeout

from .config import Settings
from .security import github_signature, qq_key, qq_signature
from .service import SHA, Worker
from .store import Store


def create_app(settings=None, *, background=True, http=None):
    settings = settings or Settings.load()
    store = Store(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        lock = FileLock(str(settings.database) + ".service.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise RuntimeError("同一数据库只能运行一个 qbot 实例") from None
        client = http or httpx.AsyncClient(timeout=20, follow_redirects=False)
        tasks = []
        try:
            with store.connect() as db:
                baseline = db.execute("SELECT value FROM meta WHERE key='baseline_repository'").fetchone()
                if baseline and baseline[0] != settings.repository.lower():
                    raise RuntimeError("配置仓库与数据库不一致；请使用新数据库，避免误发旧仓库消息")
            store.recover()
            worker = Worker(settings, store, client)
            app.state.worker = worker
            if background:
                tasks = [asyncio.create_task(worker.loop(worker.ingest_once)),
                         asyncio.create_task(worker.loop(worker.send_once))]
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if http is None:
                await client.aclose()
            lock.release()

    app = FastAPI(title="qbot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        from fastapi.responses import JSONResponse
        logging.getLogger("qbot").error("database unavailable")
        return JSONResponse({"detail": "storage unavailable"}, status_code=503)

    async def body(request, limit):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise HTTPException(413, "payload too large")
        return bytes(raw)

    def parse(raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid JSON object") from None

    @app.get("/healthz")
    async def health():
        with store.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/webhooks/github")
    async def github(request: Request):
        raw = await body(request, 26 * 1024 * 1024)
        if not github_signature(settings.webhook_secret, raw, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(401, "invalid signature")
        p = parse(raw)
        repo = p.get("repository")
        if (not isinstance(repo, dict)
                or str(repo.get("full_name", "")).lower() != settings.repository.lower()):
            raise HTTPException(403, "repository not allowed")
        event = request.headers.get("x-github-event")
        if event == "ping":
            return {"ok": True}
        if event != "push":
            return {"ignored": True}
        delivery = request.headers.get("x-github-delivery", "")
        if not delivery or len(delivery) > 200:
            raise HTTPException(400, "missing delivery id")
        if not isinstance(p.get("ref"), str):
            raise HTTPException(400, "missing ref")
        if p.get("deleted") or not p["ref"].startswith("refs/heads/"):
            return {"ignored": True}
        if not isinstance(p.get("commits"), list) or not all(
                SHA.fullmatch(str(p.get(k, ""))) for k in ("before", "after")):
            raise HTTPException(400, "invalid push")
        with store.connect() as db:
            baseline = db.execute("SELECT value FROM meta WHERE key='baseline_repository'").fetchone()
            if not baseline or baseline[0] != settings.repository.lower():
                raise HTTPException(503, "run baseline for the configured repository first")
        # Retain only data required for broadcasting and completeness checks; omit diff and emails.
        commits = []
        for c in p["commits"]:
            if not isinstance(c, dict):
                raise HTTPException(400, "invalid commit")
            a = c.get("author")
            message = c.get("message")
            if isinstance(message, str):
                lines = message.splitlines()
                message = lines[0][:300] if lines else ""
            commits.append({"id": c.get("id"), "message": message,
                            "author": {"name": a.get("name")} if isinstance(a, dict) else None,
                            "timestamp": c.get("timestamp")})
        saved = {k: p.get(k) for k in ("before", "after", "ref", "created", "forced")}
        saved["commits"] = commits
        return {"accepted": store.accept(delivery, saved)}

    @app.post("/webhooks/qq")
    async def qq(request: Request):
        raw = await body(request, 1024 * 1024)
        if request.headers.get("x-bot-appid") != settings.app_id:
            raise HTTPException(401, "invalid app id")
        p = parse(raw)
        if p.get("op") == 13:
            # Official validation requests need not have a signature header.
            d = p.get("d")
            if not isinstance(d, dict) or not all(isinstance(d.get(k), str) and 0 < len(d[k]) <= 256
                                                 for k in ("plain_token", "event_ts")):
                raise HTTPException(400, "invalid challenge")
            return {"plain_token": d["plain_token"], "signature": qq_key(settings.app_secret).sign(
                (d["event_ts"] + d["plain_token"]).encode()).hex()}
        if not qq_signature(settings.app_secret, raw, request.headers.get("x-signature-timestamp", ""),
                            request.headers.get("x-signature-ed25519", "")):
            raise HTTPException(401, "invalid signature")
        if p.get("op") == 0:
            if not isinstance(p.get("d"), dict):
                raise HTTPException(400, "invalid event")
            # Message ID is used too: the same message can arrive under another event delivery.
            identity = (p["d"].get("id") if p.get("t") in {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}
                        else p.get("id"))
            if not isinstance(identity, str) or not identity:
                raise HTTPException(400, "missing event identity")
            store.qq_event(str(p.get("t")) + ":" + identity, p.get("t"), p["d"])
        return {"op": 12}

    return app
