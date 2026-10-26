import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str = field(repr=False)
    repository: str
    github_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    database: Path
    include_url: bool = False
    send_interval: float = 4.0
    qq_environment: str = "production"
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def qq_base(self):
        return ("https://sandbox.api.bot.qq.com" if self.qq_environment == "sandbox"
                else "https://api.bot.qq.com")

    @classmethod
    def load(cls):
        load_dotenv()
        required = ["QQ_APP_SECRET", "GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET"]
        missing = [k for k in required if not os.getenv(k, "").strip()]
        if missing:
            raise ValueError("请在 .env 填写：" + ", ".join(missing))
        repo = os.environ["GITHUB_REPOSITORY"].strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("GITHUB_REPOSITORY 必须是 owner/repository，不是 URL")
        interval = float(os.getenv("SEND_INTERVAL_SECONDS", "4"))
        if not 4 <= interval <= 3600:
            raise ValueError("SEND_INTERVAL_SECONDS 必须为 4～3600")
        env = os.getenv("QQ_ENVIRONMENT", "production")
        if env not in {"production", "sandbox"}:
            raise ValueError("QQ_ENVIRONMENT 必须为 production 或 sandbox")
        app_id = os.getenv("QQ_APP_ID", "1906010968").strip()
        if not app_id.isdigit():
            raise ValueError("QQ_APP_ID 必须是开发平台数字 AppID")
        return cls(app_id, os.environ["QQ_APP_SECRET"].strip(), repo,
                   os.environ["GITHUB_TOKEN"].strip(), os.environ["GITHUB_WEBHOOK_SECRET"].strip(),
                   Path(os.getenv("DATABASE_PATH", "./data/qbot.sqlite3")).resolve(),
                   os.getenv("INCLUDE_COMMIT_URL", "false").lower() == "true", interval, env,
                   os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000")))
