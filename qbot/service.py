import asyncio
import json
import logging
import re
import time
import unicodedata

from .clients import DeliveryError, GitHubClient, QQClient

log = logging.getLogger("qbot")
SHA = re.compile(r"[0-9a-f]{40}")


def clean(value, limit):
    value = "".join(c for c in str(value) if not unicodedata.category(c).startswith("C"))
    return value[:limit]


def format_commit(settings, c, branch):
    title = str(c["message"]).splitlines()
    author = c.get("author") or {}
    text = (f"GitHub 新提交\n仓库：{settings.repository}\n分支：{clean(branch, 100)}"
            f"\n提交：{c['id'][:10]}\n作者：{clean(author.get('name', '未知'), 80)}"
            f"\n时间：{clean(c.get('timestamp', '未知'), 50)}"
            f"\n标题：{clean(title[0] if title else '（无标题）', 300)}")
    if settings.include_url:
        text += f"\nhttps://github.com/{settings.repository}/commit/{c['id']}"
    return text


class Worker:
    def __init__(self, settings, store, http):
        self.settings, self.store = settings, store
        self.github = GitHubClient(settings, http)
        self.qq = QQClient(settings, http)

    async def ingest_once(self):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return
            # Failed events block subsequent ingestion so operator can preserve event order.
            if db.execute("SELECT 1 FROM inbox WHERE state='failed' AND id<?", (row["id"],)).fetchone():
                return
            db.execute("UPDATE inbox SET state='processing' WHERE id=?", (row["id"],))
        try:
            p = json.loads(row["payload"])
            branch = p["ref"][len("refs/heads/"):]
            commits = await self.github.commits(p)
            for c in commits:
                if not isinstance(c, dict) or not SHA.fullmatch(str(c.get("id", ""))):
                    raise ValueError("invalid_commit_sha")
                if not isinstance(c.get("message"), str) or not isinstance(c.get("author"), dict):
                    raise ValueError("invalid_commit_fields")
            # All commits in one transaction: a retry cannot lose the second half of a push.
            with self.store.connect() as db:
                for c in commits:
                    new = db.execute("INSERT OR IGNORE INTO commits VALUES(?,?,?)",
                                     (self.settings.repository, c["id"], branch)).rowcount
                    if new:
                        db.execute("INSERT INTO outbox(kind,content,repo,sha,created) "
                                   "VALUES('commit',?,?,?,?)",
                                   (format_commit(self.settings, c, branch),
                                    self.settings.repository, c["id"], time.time()))
                db.execute("UPDATE inbox SET state='done',error=NULL WHERE id=?", (row["id"],))
        except Exception as exc:
            # Never log payloads, exception messages, request URLs or credentials.
            code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
                ("github_", "large_new_", "invalid_commit_")) else type(exc).__name__
            with self.store.connect() as db:
                db.execute("UPDATE inbox SET state='failed',error=? WHERE id=?", (code, row["id"]))
            log.warning("inbox id=%s failed code=%s", row["id"], code)

    async def send_once(self):
        job = self.store.claim(time.time())
        if not job:
            return
        # Persist the rate limit before network I/O; it survives restarts.
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES('next_send',?)",
                       (str(time.time() + self.settings.send_interval),))
        try:
            remote = await self.qq.send(job)
        except DeliveryError as exc:
            delay = max(exc.delay, min(3600, 2 ** min(job["attempts"], 11)))
            self.store.finish(job["id"], exc.state, exc.code, time.time() + delay)
            log.warning("outbox id=%s state=%s code=%s", job["id"], exc.state, exc.code)
        except Exception:
            self.store.finish(job["id"], "uncertain", "unexpected_send_error")
            log.warning("outbox id=%s requires review", job["id"])
        else:
            self.store.finish(job["id"], "sent", remote_id=remote)

    async def loop(self, action):
        while True:
            try:
                await action()
            except Exception:
                log.error("worker cycle failed; inspect database and disk availability")
            await asyncio.sleep(0.5)
