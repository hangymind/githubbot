import asyncio
import logging
import re
import time
from urllib.parse import quote

import httpx


class DeliveryError(Exception):
    def __init__(self, state, code, delay=60):
        self.state, self.code, self.delay = state, str(code), delay
        super().__init__(self.code)


class QQClient:
    def __init__(self, settings, http, audit=None):
        self.settings, self.http = settings, http
        self.audit = audit
        self.job_id = 0
        self.cached_token = None
        self.expiry = 0
        self.lock = asyncio.Lock()

    def record(self, stage, response, started):
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        code = str(data.get("err_code", data.get("code", 0)))
        trace = str(response.headers.get("X-Tps-trace-ID", data.get("trace_id", "")))
        details = {"http": response.status_code,
                   "code": code if re.fullmatch(r"[0-9]{1,12}", code) else "unknown",
                   "trace": trace if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", trace) else "-",
                   "ms": int((time.monotonic() - started) * 1000)}
        if self.audit:
            self.audit(self.job_id, stage, details)
        logging.getLogger("qbot").info("api job=%s stage=%s http=%s code=%s trace=%s ms=%s",
                                       self.job_id, stage, *details.values())

    async def token(self):
        async with self.lock:
            if self.cached_token and time.time() < self.expiry:
                return self.cached_token
            try:
                started = time.monotonic()
                r = await self.http.post("https://api.bot.qq.com/app/getAppAccessToken",
                                         json={"appId": self.settings.app_id,
                                               "clientSecret": self.settings.app_secret})
                self.record("token", r, started)
                data = r.json()
                if not isinstance(data, dict):
                    raise ValueError
                if r.status_code == 429 or str(data.get("code", "")) == "100001":
                    raise DeliveryError("pending", "token_rate_limit", 60)
                if r.status_code >= 500:
                    raise DeliveryError("pending", "token_server_error", 60)
                if r.status_code != 200 or str(data.get("code", 0)) != "0" or not data.get("access_token"):
                    raise DeliveryError("failed", "token_authentication")
                lifetime = int(data["expires_in"])
                if lifetime <= 0:
                    raise ValueError
                if not isinstance(data["access_token"], str):
                    raise ValueError
                self.cached_token = data["access_token"]
                self.expiry = time.time() + max(1, lifetime - min(55, lifetime / 2))
                return self.cached_token
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                # Token requests cannot send a group message; safe to retry.
                raise DeliveryError("pending", "token_unavailable") from None

    async def send(self, job):
        self.job_id = job.get("id", 0)
        token = await self.token()
        payload = {"msg_type": 0, "content": job["content"]}
        if job["msg_id"]:
            payload.update(msg_id=job["msg_id"], msg_seq=1)
        target_type = job.get("target_type", "group")
        if target_type not in {"group", "private"}:
            raise DeliveryError("failed", "invalid_target_type")
        collection = "users" if target_type == "private" else "groups"
        url = self.settings.qq_base + f"/v2/{collection}/" + quote(job["group_id"], safe="") + "/messages"
        try:
            started = time.monotonic()
            r = await self.http.post(url, json=payload, headers={"Authorization": "QQBot " + token})
            self.record(target_type + ("_reply" if job["msg_id"] else "_active"), r, started)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise DeliveryError("pending", "connect_failed") from None
        except httpx.HTTPError:
            raise DeliveryError("uncertain", "send_transport_uncertain") from None
        try:
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise DeliveryError("uncertain", "invalid_send_response") from None
        code = str(data.get("err_code", data.get("code", 0)))
        if r.status_code == 401:
            self.cached_token = None
            raise DeliveryError("pending", "token_expired", 5)
        if r.status_code == 429 or code in {"40034100", "1100100"}:
            # May be a minute or daily quota. Do not spin against a daily limit.
            raise DeliveryError("pending", "qq_" + code, 3600 if job["attempts"] > 3 else 60)
        if r.status_code >= 500 or r.status_code in {201, 202} or code in {"304023", "304024"}:
            raise DeliveryError("uncertain", "qq_" + code)
        if 200 <= r.status_code < 300 and code == "0" and data.get("id"):
            return str(data["id"])
        # A success-shaped but incomplete response is not proof of non-delivery.
        if 200 <= r.status_code < 300 and code == "0":
            raise DeliveryError("uncertain", "missing_message_id")
        raise DeliveryError("failed", "qq_" + code)


class GitHubClient:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    async def commits(self, payload):
        commits = payload.get("commits", [])
        if len(commits) < 2048:
            return commits
        before, after = payload["before"], payload["after"]
        if payload.get("created") or set(before) == {"0"}:
            # Listing all ancestors here would wrongly broadcast historical commits.
            raise ValueError("large_new_branch_requires_manual_review")
        result, total = [], None
        for page in range(1, 10001):
            r = await self.http.get(
                f"https://api.github.com/repos/{self.settings.repository}/compare/{before}...{after}",
                params={"per_page": 100, "page": page},
                headers={"Authorization": "Bearer " + self.settings.github_token,
                         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
            if r.status_code != 200:
                raise ValueError("github_compare_http_" + str(r.status_code))
            data = r.json()
            if total is None:
                total = data["total_commits"]
            if total != data["total_commits"] or not isinstance(total, int):
                raise ValueError("github_compare_changed")
            batch = data["commits"]
            for c in batch:
                result.append({"id": c["sha"], "message": c["commit"]["message"],
                               "author": {"name": c["commit"]["author"]["name"]},
                               "timestamp": c["commit"]["committer"]["date"]})
            if len(result) >= total or not batch:
                break
        shas = {c["id"] for c in result}
        if len(result) != total or len(shas) != total or after not in shas:
            raise ValueError("github_compare_incomplete")
        if not {c["id"] for c in commits}.issubset(shas):
            raise ValueError("github_compare_payload_mismatch")
        return result
