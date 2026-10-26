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
        self.qq = QQClient(settings, http, store.audit)
        self.http = http

    async def diagnostic(self, job):
        self.qq.job_id = job["id"]
        lines = ["联通诊断（脱敏）", "QQ 回调：已收到并通过 AppID/签名校验",
                 f"AppID={self.settings.app_id} 环境={self.settings.qq_environment}",
                 f"目标={job['target_type']}；本报告使用 msg_id 被动回复"]
        try:
            await self.qq.token()
            lines.append("QQ access_token：可用（缓存或刚获取；不输出凭证）")
        except DeliveryError as exc:
            lines.append(f"QQ access_token：{exc.code}")
        try:
            response = await self.http.get(
                f"https://api.github.com/repos/{self.settings.repository}/commits",
                params={"per_page": 1}, headers={"Authorization": "Bearer " + self.settings.github_token,
                    "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
            lines.append(f"GitHub 读取提交：HTTP {response.status_code}（不显示仓库/提交内容）")
        except Exception:
            lines.append("GitHub 读取提交：网络请求失败（不输出原始异常）")
        collection = "users/{user_openid}" if job["target_type"] == "private" else "groups/{group_openid}"
        lines.append(f"QQ POST /v2/{collection}/messages；msg_type=0；Authorization=QQBot [隐藏]")
        lines.append(f"请求头 X-Union-Appid={self.settings.app_id}；"
                     "Content-Type=application/json; charset=utf-8")
        lines.append("主动请求仅含 content/msg_type，不携带 msg_id/event_id；被动回复含 msg_id/msg_seq=1。")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT d.job_id,d.stage,d.details FROM api_diagnostics d JOIN outbox o ON o.id=d.job_id "
                "WHERE o.group_id=? AND o.target_type=? ORDER BY d.id DESC LIMIT 3",
                (job["group_id"], job["target_type"])).fetchall()
            errors = db.execute(
                "SELECT id,state,error FROM outbox WHERE group_id=? AND target_type=? "
                "AND error IS NOT NULL ORDER BY id DESC LIMIT 3",
                (job["group_id"], job["target_type"])).fetchall()
            counts = self.store.counts(db)
        lines.append("队列=" + json.dumps(counts))
        for row in rows:
            d = json.loads(row["details"])
            lines.append(f"任务{row['job_id']} {row['stage']} HTTP={d['http']} code={d['code']} "
                         f"TraceID={d['trace']} {d['ms']}ms")
        for row in errors:
            lines.append(f"错误记录：任务{row['id']} {row['state']} {row['error']}")
        lines.extend(["40034105：平台拒绝主动发送；回复成功不代表主动权限可用。",
                      "GROUP_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE 是接收事件，不是主动发送授权。",
                      "核对目标通知开关、单聊/群聊主动权限、发布/测试范围、环境。",
                      "可发送 /测试 主动 单独验证主动消息；报告可读仅证明被动回复成功。"])
        return "\n".join(lines)

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
            if job["kind"] == "diagnostic":
                job["content"] = await self.diagnostic(job)
            remote = await self.qq.send(job)
        except DeliveryError as exc:
            delay = max(exc.delay, min(3600, 2 ** min(job["attempts"], 11)))
            state = "failed" if job["kind"] == "probe" and exc.state == "pending" else exc.state
            self.store.finish(job["id"], state, exc.code, time.time() + delay)
            log.warning("outbox id=%s state=%s code=%s", job["id"], state, exc.code)
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
