"""Environment-driven configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {raw!r}") from None


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


@dataclass
class Config:
    # Your own OAuth client and the refresh token from deploy/google-auth.py
    client_id: str
    client_secret: str
    refresh_token: str
    notify_urls: list[str]

    report_hour: int = 8  # the daily report, in the container's time zone
    usage_percent: int = 75  # over this share of the quota, ask to empty the trash
    top_files: int = 10  # the largest files listed in the report; 0 leaves the list out

    # Where to ask the assistant to empty the trash: a Discord webhook in a channel it reads, and
    # its Discord user ID so the message mentions it. Without both, the report just says to ask.
    assistant_url: str = ""
    assistant_user_id: str = ""

    # Agent bus: MCP endpoint for the assistant. drive_empty_trash needs an approval channel.
    bus_token: str = ""
    bus_port: int = 8765
    approval_mode: str = "code"
    approval_notify_urls: list[str] = field(default_factory=list)

    state_dir: str = "/var/lib/gdrive-agent"

    @property
    def state_path(self) -> str:
        return os.path.join(self.state_dir, "state.json")

    @property
    def run_now_path(self) -> str:
        return os.path.join(self.state_dir, "run-now")

    @classmethod
    def from_env(cls) -> "Config":
        need = {n: os.environ.get(n, "").strip() for n in
                ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")}
        missing = [n for n, v in need.items() if not v]
        if missing:
            raise SystemExit(f"{', '.join(missing)} required (see README: Google sign-in)")
        notify = _list("NOTIFY_URLS")
        if not notify:
            raise SystemExit("NOTIFY_URLS is required")
        hour = _int("REPORT_HOUR", 8)
        if not 0 <= hour <= 23:
            raise SystemExit(f"REPORT_HOUR must be from 0 to 23, got {hour}")
        percent = _int("USAGE_PERCENT", 75)
        if not 1 <= percent <= 100:
            raise SystemExit(f"USAGE_PERCENT must be from 1 to 100, got {percent}")
        top = _int("TOP_FILES", 10)
        if not 0 <= top <= 50:
            raise SystemExit(f"TOP_FILES must be from 0 to 50, got {top}")
        assistant_url = os.environ.get("ASSISTANT_URL", "").strip()
        assistant_id = os.environ.get("ASSISTANT_USER_ID", "").strip()
        if bool(assistant_url) != bool(assistant_id):
            raise SystemExit("ASSISTANT_URL and ASSISTANT_USER_ID go together: set both or neither")
        if assistant_id and not re.fullmatch(r"\d{15,21}", assistant_id):
            raise SystemExit("ASSISTANT_USER_ID must be a Discord user ID (digits only)")
        return cls(
            client_id=need["GOOGLE_CLIENT_ID"],
            client_secret=need["GOOGLE_CLIENT_SECRET"],
            refresh_token=need["GOOGLE_REFRESH_TOKEN"],
            notify_urls=notify,
            report_hour=hour,
            usage_percent=percent,
            top_files=top,
            assistant_url=assistant_url,
            assistant_user_id=assistant_id,
            bus_token=os.environ.get("AGENT_BUS_TOKEN", "").strip(),
            bus_port=_int("AGENT_BUS_PORT", 8765),
            approval_mode=os.environ.get("APPROVAL_MODE", "").strip().lower() or "code",
            approval_notify_urls=_list("APPROVAL_NOTIFY_URLS"),
            state_dir=os.environ.get("STATE_DIR", "").strip() or "/var/lib/gdrive-agent",
        )
