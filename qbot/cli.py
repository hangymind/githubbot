import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

import httpx
import uvicorn
from filelock import FileLock, Timeout

from .config import Settings
from .store import Store


async def baseline(settings, store):
    """Snapshot all reachable commits at immutable branch tips, before enabling the webhook."""
    headers = {"Authorization": "Bearer " + settings.github_token,
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    base = "https://api.github.com/repos/" + settings.repository
    tips, shas = [], set()
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=False) as http:
        async def pages(path, params=None):
            items = []
            for page in range(1, 100001):
                r = await http.get(base + path, params={**(params or {}), "per_page": 100, "page": page})
                if r.status_code != 200:
                    raise ValueError(f"GitHub HTTP {r.status_code}；检查仓库、token 权限或 API 配额")
                data = r.json()
                if not isinstance(data, list):
                    raise ValueError("GitHub 返回格式异常")
                items.extend(data)
                if 'rel="next"' not in r.headers.get("link", ""):
                    return items
            raise ValueError("分页过多；基线未完成")
        tips = [b["commit"]["sha"] for b in await pages("/branches")]
        for index, tip in enumerate(tips, 1):
            for c in await pages("/commits", {"sha": tip}):
                shas.add(c["sha"])
            print(f"已扫描分支 {index}/{len(tips)}，累计 {len(shas)} 个历史 SHA", flush=True)
    # Commit only after every API request succeeds; a partial baseline is never activated.
    with store.connect() as db:
        db.executemany("INSERT OR IGNORE INTO commits VALUES(?,?,?)",
                       ((settings.repository, sha, "__baseline__") for sha in shas))
        db.execute("INSERT INTO meta VALUES('baseline_repository',?)", (settings.repository.lower(),))
    print("基线已完成。现在启动服务并启用 GitHub Webhook，之后才开始推送新提交。")


def main():
    parser = argparse.ArgumentParser(description="QQ GitHub 广播管理；在项目目录运行，自动读取 .env")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "baseline", "status", "jobs", "events", "resume", "diagnostics"):
        sub.add_parser(name)
    token_parser = sub.add_parser("bind-token")
    token_parser.add_argument("--target", choices=["group", "private"], default="group",
                              help="group 群聊（兼容旧版）；private 私聊")
    retry = sub.add_parser("retry", help="重试明确失败的发送任务（不包括 uncertain）")
    retry.add_argument("id", type=int)
    event = sub.add_parser("retry-event")
    event.add_argument("id", type=int)
    resolve = sub.add_parser("resolve", help="人工核查 uncertain 任务后处理")
    resolve.add_argument("id", type=int)
    resolve.add_argument("--as", dest="resolution", choices=["sent", "skipped", "retry"], required=True)
    resolve.add_argument("--accept-duplicate-risk", action="store_true")
    unbind = sub.add_parser("unbind")
    unbind.add_argument("--discard-pending", action="store_true")
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings.load()
        store = Store(settings.database)
        if args.command == "serve":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
            from .app import create_app
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                        workers=1, access_log=False)
            return
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return
        if args.command == "diagnostics":
            with store.connect() as db:
                rows = db.execute("SELECT job_id,stage,details,created FROM api_diagnostics "
                                  "ORDER BY id DESC LIMIT 20").fetchall()
                print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
            return
        if args.command in {"jobs", "events"}:
            with store.connect() as db:
                query = ("SELECT id,kind,target_type,state,attempts,error,created "
                         "FROM outbox ORDER BY id DESC LIMIT 50"
                         if args.command == "jobs" else
                         "SELECT id,state,error,created FROM inbox ORDER BY id DESC LIMIT 50")
                print(json.dumps([dict(r) for r in db.execute(query)], ensure_ascii=False, indent=2))
            return
        if args.command == "backup":
            if args.destination.exists() or args.destination.resolve() == settings.database:
                raise ValueError("备份目标必须是不存在的新文件")
            with store.connect() as source:
                with sqlite3.connect(args.destination) as destination:
                    source.backup(destination)
            print("一致性备份已完成；备份含私有提交元数据，请妥善保管。")
            return
        if args.command == "bind-token":
            code = store.token(args.target)
            where = "在机器人私聊中发送" if args.target == "private" else "在目标群 @机器人"
            print(f"10 分钟内{where} /绑定 以下口令（仅本次显示）：")
            print(code)
            return
        # Changes to delivery state and destinations require a stopped worker.
        with FileLock(str(settings.database) + ".service.lock", timeout=0):
            with store.connect() as db:
                if args.command == "baseline":
                    if db.execute("SELECT 1 FROM meta WHERE key='baseline_repository'").fetchone():
                        raise ValueError("基线已存在；不可重建以免吞掉未发送的新提交")
                    asyncio.run(baseline(settings, store))
                    return
                if args.command == "resume":
                    db.execute("UPDATE binding SET enabled=1")
                elif args.command == "unbind":
                    if db.execute("SELECT 1 FROM inbox WHERE state!='done'").fetchone():
                        raise ValueError("入站事件尚未处理完成；请先处理 events 中的积压，再更换推送目标")
                    if db.execute("SELECT 1 FROM outbox WHERE state IN ('sending','uncertain')").fetchone():
                        raise ValueError("请先启动服务恢复发送状态，再核查并 resolve 所有 uncertain 任务")
                    if not args.discard_pending:
                        raise ValueError("解绑会丢弃旧目标积压；确认后加 --discard-pending")
                    db.execute("UPDATE outbox SET state='skipped' WHERE state NOT IN ('sent','skipped')")
                    db.execute("DELETE FROM pull_items")
                    db.execute("DELETE FROM binding")
                    db.execute("DELETE FROM bind_tokens")
                elif args.command == "retry-event":
                    if not db.execute("UPDATE inbox SET state='pending',error=NULL "
                                      "WHERE id=? AND state='failed'",
                                      (args.id,)).rowcount:
                        raise ValueError("找不到该失败事件")
                elif args.command == "retry":
                    row = db.execute("SELECT kind FROM outbox WHERE id=?", (args.id,)).fetchone()
                    if row and row["kind"] == "pull":
                        raise ValueError("领取回复不能重试旧 msg_id；请在原会话重新发送 /更新")
                    if not settings.include_url:
                        # A queued message keeps its original text. Honor disabling links on retry.
                        row = db.execute("SELECT content,sha,kind FROM outbox WHERE id=? AND state='failed'",
                                         (args.id,)).fetchone()
                        if row and row["kind"] == "commit":
                            suffix = f"\nhttps://github.com/{settings.repository}/commit/{row['sha']}"
                            db.execute("UPDATE outbox SET content=? WHERE id=?",
                                       (row["content"].removesuffix(suffix), args.id))
                    if not db.execute("UPDATE outbox SET state='pending',due=0,error=NULL WHERE id=? "
                                      "AND state='failed'", (args.id,)).rowcount:
                        raise ValueError("找不到该 failed 任务；uncertain 请用 resolve")
                elif args.command == "resolve":
                    if args.resolution == "retry" and not args.accept_duplicate_risk:
                        raise ValueError("重试可能重复；确认后加 --accept-duplicate-risk")
                    state = "pending" if args.resolution == "retry" else args.resolution
                    resolved = db.execute("SELECT kind FROM outbox WHERE id=?", (args.id,)).fetchone()
                    if resolved and resolved["kind"] == "pull" and args.resolution == "retry":
                        # Release reservations for a new command; never reuse an old reply window.
                        state = "expired"
                    if not db.execute("UPDATE outbox SET state=?,due=0,error='operator_resolved' "
                                      "WHERE id=? AND state='uncertain'", (state, args.id)).rowcount:
                        raise ValueError("找不到该 uncertain 任务")
                    store.settle_pull(db, args.id, state)
            print("操作完成。请启动服务，并运行 status 检查状态。")
    except Timeout:
        print("服务正在运行；此操作请先停止 qbot 服务后再执行。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, sqlite3.Error, httpx.HTTPError) as exc:
        # Configuration errors are controlled; external exception messages may contain secrets.
        msg = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print("操作失败：" + msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
