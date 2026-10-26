import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox(
 id INTEGER PRIMARY KEY, delivery TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', error TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS commits(
 repo TEXT NOT NULL, sha TEXT NOT NULL, branch TEXT NOT NULL,
 PRIMARY KEY(repo,sha));
CREATE TABLE IF NOT EXISTS binding(
 id INTEGER PRIMARY KEY CHECK(id=1), group_id TEXT NOT NULL, owner TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS bind_tokens(
 digest TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS qq_events(id TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, group_id TEXT, content TEXT NOT NULL,
 msg_id TEXT, expires REAL, repo TEXT, sha TEXT,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 due REAL NOT NULL DEFAULT 0, error TEXT, remote_id TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state,kind,id);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta VALUES('created',?)", (str(time.time()),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE outbox SET state='uncertain',error='interrupted_send' WHERE state='sending'")
            db.execute("UPDATE inbox SET state='pending' WHERE state='processing'")

    def accept(self, delivery, payload):
        with self.connect() as db:
            return bool(db.execute(
                "INSERT OR IGNORE INTO inbox(delivery,payload,created) VALUES(?,?,?)",
                (delivery, json.dumps(payload, ensure_ascii=False), time.time())).rowcount)

    def token(self):
        code = secrets.token_urlsafe(18)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM binding").fetchone():
                raise ValueError("已绑定；更换群请先停止服务并运行 unbind")
            db.execute("DELETE FROM bind_tokens")
            db.execute("INSERT INTO bind_tokens VALUES(?,?)",
                       (hashlib.sha256(code.encode()).hexdigest(), time.time() + 600))
        return code

    @staticmethod
    def counts(db):
        return {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM outbox GROUP BY state")}

    def status(self):
        with self.connect() as db:
            b = db.execute("SELECT enabled FROM binding WHERE id=1").fetchone()
            return {"bound": bool(b), "enabled": bool(b and b[0]), "outbox": self.counts(db),
                    "inbox": {r[0]: r[1] for r in db.execute(
                        "SELECT state,count(*) FROM inbox GROUP BY state")}}

    def qq_event(self, event_id, kind, data):
        """Handle commands atomically; never persist raw command text or binding secrets."""
        group = data.get("group_openid")
        if not isinstance(group, str) or not group:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("INSERT OR IGNORE INTO qq_events VALUES(?,?)",
                              (event_id, time.time())).rowcount:
                return
            if kind in {"GROUP_DEL_ROBOT", "GROUP_MSG_REJECT"}:
                db.execute("UPDATE binding SET enabled=0 WHERE group_id=?", (group,))
                return
            if kind != "GROUP_AT_MESSAGE_CREATE":
                return
            author = data.get("author") or {}
            owner = author.get("member_openid") if isinstance(author, dict) else None
            msg_id = data.get("id")
            if not isinstance(owner, str) or not owner or not isinstance(msg_id, str) or not msg_id:
                return
            content = re.sub(r"<@!?[^>]+>", "", str(data.get("content", ""))).strip()
            parts = content.split()
            command = parts[0] if parts else ""
            binding = db.execute("SELECT * FROM binding WHERE id=1").fetchone()
            reply = None
            if command == "/绑定":
                digest = hashlib.sha256(parts[1].encode()).hexdigest() if len(parts) == 2 else ""
                token = db.execute("SELECT 1 FROM bind_tokens WHERE digest=? AND expires>?",
                                   (digest, time.time())).fetchone()
                if binding:
                    reply = "机器人已绑定；请在服务器管理绑定。"
                elif not token:
                    reply = "绑定失败：口令无效或已过期。"
                else:
                    db.execute("INSERT INTO binding VALUES(1,?,?,1)", (group, owner))
                    db.execute("DELETE FROM bind_tokens")
                    reply = "绑定成功。新提交将发送到本群。请确认群成员可以查看仓库提交信息。"
            elif command == "/状态":
                if binding and binding["group_id"] == group and binding["owner"] == owner:
                    reply = ("广播已启用" if binding["enabled"] else "广播已暂停，请在服务器处理")
                    reply += "；队列：" + json.dumps(self.counts(db), ensure_ascii=False)
                else:
                    reply = "仅绑定操作者可在绑定群内查询状态。"
            elif command in {"/帮助", "/help"}:
                reply = "GitHub 提交广播：/绑定 一次性口令；/状态。口令由服务器生成，10 分钟有效。"
            if reply:
                # Replies expire before QQ's five-minute passive reply window.
                db.execute("INSERT INTO outbox(kind,group_id,content,msg_id,expires,created) "
                           "VALUES('reply',?,?,?,?,?)",
                           (group, reply, msg_id, time.time() + 240, time.time()))

    def claim(self, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE outbox SET state='expired' "
                       "WHERE kind='reply' AND state='pending' AND expires<?",
                       (now,))
            last = db.execute("SELECT value FROM meta WHERE key='next_send'").fetchone()
            if last and float(last[0]) > now:
                return None
            # Passive command replies do not get stuck behind a paused broadcast.
            row = db.execute("SELECT * FROM outbox WHERE kind='reply' AND state='pending' AND due<=? "
                             "ORDER BY id LIMIT 1", (now,)).fetchone()
            if not row:
                b = db.execute("SELECT * FROM binding WHERE enabled=1").fetchone()
                if not b:
                    return None
                # Head-of-line blocking preserves commit order, including during backoff/review.
                row = db.execute("SELECT * FROM outbox WHERE kind='commit' "
                                 "AND state NOT IN ('sent','skipped') ORDER BY id LIMIT 1").fetchone()
                if not row or row["state"] != "pending" or row["due"] > now:
                    return None
                if row["group_id"] and row["group_id"] != b["group_id"]:
                    return None
                db.execute("UPDATE outbox SET group_id=? WHERE id=?", (b["group_id"], row["id"]))
            db.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            return dict(db.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone())

    def finish(self, job_id, state, error=None, due=0, remote_id=None):
        with self.connect() as db:
            db.execute("UPDATE outbox SET state=?,error=?,due=?,remote_id=? WHERE id=?",
                       (state, error, due, remote_id, job_id))
