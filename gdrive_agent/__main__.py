"""Entry point: python -m gdrive_agent [check]

Once a day: reports Google Drive's storage, its trash, and how many files and folders changed since the
last report. Over USAGE_PERCENT it asks the assistant to empty the trash, which the operator approves or
denies with the assistant's buttons. `check` prints the numbers without saving or sending anything.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests

from . import health
from .bus import Approvals, BusTool, Runtime, schema, serve_mcp
from .config import Config
from .drive import AuthRejected, Changes, Drive, DriveError, Quota, size
from .findings import Finding
from .notice import ICONS, Message, Outbox, Section
from .state import State

log = logging.getLogger("gdrive_agent")

TICK = 60
LOOP_WITHIN = 1800  # a report pages through the day's changes and the whole trash
AUTHOR = "Google Drive agent"
AVATAR = ICONS.format("google-drive")
SIGN_IN_ADVICE = "Run deploy/google-auth.py again and set the new GOOGLE_REFRESH_TOKEN secret (see README)."


def utc_stamp(now: float) -> str:
    """RFC 3339 the way Drive writes createdTime, so the two compare as strings."""
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local_time(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%a %d %b %H:%M")
    except ValueError:
        return stamp


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def usage_line(q: Quota) -> str:
    if q.limit is None:
        return f"{size(q.usage)} used (no storage limit)"
    return f"{size(q.usage)} of {size(q.limit)} used (**{q.percent:.0f}%**)"


def code(text: str) -> str:
    """Inline code, so names with * or _ show as they are."""
    return "`" + text.replace("`", "'") + "`"


def largest_lines(files: list[dict]) -> list[str]:
    return [f"**{size(f['size'])}** · {code(f['name'])}" + (f" · in {code(f['path'])}" if f["path"] else " · in My Drive")
            for f in files]


def change_lines(c: Changes, since: str) -> list[str]:
    lines = [f"Since {local_time(since)}:"] if since else []
    if not c.total:
        return lines + ["Nothing changed."]
    lines += [f"{plural(c.files, 'file')} added or changed ({c.new_files:,} new)",
              f"{plural(c.folders, 'folder')} added or changed ({c.new_folders:,} new)"]
    if c.trashed:
        lines.append(f"{plural(c.trashed, 'item')} moved to the trash")
    if c.removed:
        lines.append(f"{plural(c.removed, 'item')} deleted for good or no longer shared with you")
    if not c.complete:
        lines.append("*More changes than one report pages through: the rest count towards the next one.*")
    return lines


class Agent:
    def __init__(self, cfg: Config, state: State, outbox: Outbox, drive: Drive, runtime: Runtime | None = None,
                 post=requests.post):
        self.cfg, self.state, self.outbox, self.drive = cfg, state, outbox, drive
        self.runtime = runtime or Runtime("gdrive")
        self.post = post
        self.lock = threading.RLock()  # the daily run and the bus share the state

    @property
    def run_state(self) -> dict:
        return self.state["run"]

    # --- Telling people. Messages are saved first, so a Discord outage can't lose one. ---

    def say(self, title: str, level: str, sections: list[Section]) -> None:
        self.state["pending"].setdefault("messages", []).append(
            {"title": title, "level": level,
             "sections": [{"heading": s.heading, "lines": s.lines, "note": s.note, "bullets": s.bullets} for s in sections]})
        self.state.save()

    def flush(self) -> None:
        queue = self.state["pending"].get("messages", [])
        while queue:
            m = queue[0]
            if not self.outbox.send(Message(m["title"], m["level"], [Section(**s) for s in m["sections"]], AUTHOR)):
                return
            queue.pop(0)
            self.state.save()

    def problem(self, key: str, title: str, lines: list[str]) -> None:
        """Said once, while the problem stays the same, however many days it lasts."""
        known = self.state["problems"]
        if known.get(key) != title:
            known[key] = title
            self.say(f"❌ {title}", "error", [Section("", lines, bullets=False)])
        self.runtime.record([Finding(k, v, "error") for k, v in known.items()], None)

    def clear(self, key: str) -> None:
        title = self.state["problems"].pop(key, None)
        if title:
            self.say(f"✅ Resolved: {title}", "ok", [])
        self.runtime.record([Finding(k, v, "error") for k, v in self.state["problems"].items()], None)

    # --- The daily report ---

    def over(self, q: Quota) -> bool:
        return q.percent is not None and q.percent > self.cfg.usage_percent

    def report(self, now: float) -> None:
        with self.lock:
            self._report(now)
            self.flush()

    def _report(self, now: float) -> None:
        run = self.run_state
        try:
            q = self.drive.quota()
            items = self.drive.trash_items()
            since, token = run.get("token_at", ""), run.get("token")
            changes = self.drive.changes(token, since) if token else None
            if changes is None:
                run["token"] = self.drive.start_token()
            else:
                run["token"] = changes.token
        except AuthRejected as exc:
            self.problem("google", "Google rejected the agent's sign-in, so there's no Drive report",
                         [str(exc), SIGN_IN_ADVICE])
            return
        except DriveError as exc:
            self.problem("google", "Can't read Google Drive, so there's no report today", [str(exc)])
            return
        self.clear("google")
        run["token_at"] = utc_stamp(now)

        storage = [usage_line(q), f"Drive {size(q.drive)} · Gmail and Photos {size(max(0, q.usage - q.drive))}"]
        if "usage" in run:
            delta = q.usage - run["usage"]
            storage.append(f"{'+' if delta >= 0 else '−'}{size(abs(delta))} since the last report")
        trash = [f"{size(q.trash)}" + ("" if items is None else f" in {plural(items, 'item')}")]
        changed = change_lines(changes, since) if changes else \
            ["Counting starts now: the next report has the first numbers."]
        sections = [Section("💾 Storage", storage, bullets=False), Section("🗑️ Trash", trash, bullets=False),
                    Section("📝 Changes", changed, bullets=False)]
        top = self.largest()
        if top is not None:
            sections.append(Section(f"📦 Largest {len(top)} files", largest_lines(top) or ["No files."], bullets=False))
        elif self.cfg.top_files:
            sections.append(Section("📦 Largest files", ["Couldn't list them today."], bullets=False))
        over = self.over(q)
        ask = over and q.trash > 0
        if over:
            pct = f"over the {self.cfg.usage_percent}% limit"
            if not q.trash:
                note = f"Storage is {pct}, but the trash is empty: there's nothing for the agent to reclaim."
            elif self.cfg.assistant_url:
                note = (f"Storage is {pct}. Emptying the trash would free about {size(q.trash)}; the assistant "
                        "will ask you to Allow or Deny it.")
            else:
                note = (f"Storage is {pct}. Emptying the trash would free about {size(q.trash)}: ask your "
                        "assistant to run the gdrive agent's drive_empty_trash.")
            sections.append(Section("⚠️ Over the limit", [note], bullets=False))
        title = f"☁️ Google Drive: {usage_line(q).split(' (')[0]}" if q.limit is None \
            else f"☁️ Google Drive {q.percent:.0f}% full"
        self.say(title, "warning" if over else "info", sections)

        run.update(usage=q.usage, reported=utc_stamp(now))
        self.state["report"].update(
            at=utc_stamp(now), limit=q.limit, usage=q.usage, drive=q.drive, trash=q.trash, trash_items=items,
            percent=None if q.percent is None else round(q.percent, 1), over_limit=over,
            changes=None if changes is None else {k: getattr(changes, k) for k in
                                                  ("files", "new_files", "folders", "new_folders", "trashed",
                                                   "removed", "complete")},
            changes_since=since or None,
            largest=None if top is None else [{**f, "size": size(f["size"])} for f in top])
        self.state.save()
        self.flush()
        today = datetime.fromtimestamp(now).date().isoformat()
        # At most once a day, so asking for a check can't keep prompting.
        if ask and self.cfg.assistant_url and run.get("asked") != today:
            if self.ask_assistant(q, items):
                run["asked"] = today
            else:
                self.say("⚠️ Couldn't ask the assistant to empty the trash", "warning", [Section(
                    "", ["Ask it yourself to run the gdrive agent's drive_empty_trash."], bullets=False)])
            self.state.save()

    def ask_assistant(self, q: Quota, items: int | None) -> bool:
        """A plain message that mentions the assistant, in a channel it reads: it calls drive_empty_trash, and
        its approval buttons put the decision with the operator. The agent never empties the trash itself."""
        count = "" if items is None else f", {plural(items, 'item')}"
        text = (f"<@{self.cfg.assistant_user_id}> Google Drive is {q.percent:.0f}% full ({size(q.usage)} of "
                f"{size(q.limit)}), over the {self.cfg.usage_percent}% limit. Emptying the trash would free about "
                f"{size(q.trash)}{count}, deleted for good. Please call the gdrive agent's drive_empty_trash tool "
                "(it takes no arguments) so the operator can Allow or Deny it, then post its result. Nothing else "
                "is needed.")
        payload = {"content": text, "username": "gdrive-agent", "avatar_url": AVATAR,
                   "allowed_mentions": {"users": [self.cfg.assistant_user_id]}}
        try:
            r = self.post(self.cfg.assistant_url, json=payload, params={"wait": "true"}, timeout=15)
        except requests.RequestException as exc:
            log.warning("Can't reach the assistant's webhook: %s", type(exc).__name__)
            return False
        if not r.ok:
            log.warning("The assistant's webhook refused the message: HTTP %s", r.status_code)
        return r.ok

    def largest(self) -> list[dict] | None:
        """The report goes out without the list rather than not at all if this fails."""
        if not self.cfg.top_files:
            return None
        try:
            return self.drive.largest(self.cfg.top_files)
        except DriveError as exc:
            log.warning("Can't list the largest files: %s", exc)
            return None

    # --- Bus tools ---

    def status(self) -> dict:
        q = self.drive.quota()
        return {"limit": size(q.limit), "used": size(q.usage), "percent": None if q.percent is None else round(q.percent, 1),
                "drive": size(q.drive), "trash": size(q.trash), "trash_bytes": q.trash,
                "over_limit": self.over(q), "limit_percent": self.cfg.usage_percent,
                "last_report": dict(self.state["report"]) or None}

    def empty_trash(self) -> dict:
        """Run only through the approval policy (destructive tool)."""
        with self.lock:
            before = self.drive.quota()
            self.drive.empty_trash()
            after = self.drive.quota()
            freed = max(0, before.trash - after.trash)
            self.run_state["emptied"] = utc_stamp(time.time())
            lines = [f"Trash had {size(before.trash)}; it now shows {size(after.trash)}.",
                     f"Storage: {usage_line(after)}."]
            if after.trash:
                lines.append("*Google can take a while to reclaim the space: the next report shows where it settled.*")
            self.say("🗑️ Google Drive trash emptied", "ok", [Section("", lines, bullets=False)])
            self.flush()
            return {"trash_before": size(before.trash), "trash_now": size(after.trash), "freed_so_far": size(freed),
                    "used_now": size(after.usage),
                    "percent_now": None if after.percent is None else round(after.percent, 1),
                    "note": "Google can take a while to reclaim space after the trash is emptied."}

    def tools(self) -> list[BusTool]:
        return self.runtime.common_tools(check_is_destructive=False) + [
            BusTool("drive_status", "Google Drive: storage used and its limit, the trash size, and the last daily "
                    "report (with how many files and folders changed, and the largest files).", schema(), self.status),
            BusTool("drive_empty_trash", "Google Drive: permanently delete everything in the trash to free space. "
                    "Can't be undone. Takes no arguments.", schema(), self.empty_trash, read_only=False),
        ]

    # --- Scheduling ---

    def asked_to_run(self) -> bool:
        """`touch run-now` in the state folder, or the bus's agent_run_check, starts a report now."""
        if self.runtime.wake.is_set():
            self.runtime.wake.clear()
            return True
        try:
            os.unlink(self.cfg.run_now_path)
            return True
        except OSError:
            return False

    def start(self, now: float) -> None:
        """A new deployment starts counting changes now and sends its first report at the next REPORT_HOUR."""
        local = datetime.fromtimestamp(now)
        if "ran" not in self.run_state and local.hour >= self.cfg.report_hour:
            self.run_state["ran"] = local.date().isoformat()
        if "token" not in self.run_state:
            try:
                self.run_state["token"], self.run_state["token_at"] = self.drive.start_token(), utc_stamp(now)
            except DriveError as exc:
                log.warning("Can't start counting changes yet: %s", exc)
        self.state.save()

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.flush()
        local = datetime.fromtimestamp(now)
        today = local.date().isoformat()
        due = local.hour >= self.cfg.report_hour and self.run_state.get("ran") != today
        if self.asked_to_run() or due:
            if due:
                self.run_state["ran"] = today
                self.state.save()
            self.report(now)
        self.flush()

    def run(self) -> None:
        self.start(time.time())
        while True:
            health.beat("daily", LOOP_WITHIN)
            try:
                self.tick()
            except Exception:  # keep the daemon alive; next tick may succeed
                log.exception("Tick failed")
            self.runtime.wake.wait(TICK)


def show(drive: Drive, state: State, top: int) -> None:
    """Changes nothing: prints today's numbers, and the changes since the last report."""
    q = drive.quota()
    print(usage_line(q).replace("**", ""))
    print(f"Drive {size(q.drive)}, trash {size(q.trash)} in {drive.trash_items()} items")
    for f in drive.largest(top) if top else []:
        print(f"{size(f['size']):>10}  {f['name']}  (in {f['path'] or 'My Drive'})")
    run = state["run"]
    if run.get("token"):
        print("\n".join(change_lines(drive.changes(run["token"], run.get("token_at", "")), run.get("token_at", ""))))
    else:
        print("No change count yet: it starts when the agent first runs.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = Config.from_env()
    drive = Drive(cfg.client_id, cfg.client_secret, cfg.refresh_token)
    state = State(cfg.state_path)
    if sys.argv[1:] == ["check"]:
        show(drive, state, cfg.top_files)
        return
    health.reset()
    runtime = Runtime("gdrive", revision=_revision())
    agent = Agent(cfg, state, Outbox(cfg.notify_urls, avatar=AVATAR), drive, runtime)
    if cfg.bus_token and cfg.bus_port:
        approvals = Approvals("gdrive", cfg.approval_notify_urls, mode=cfg.approval_mode)
        serve_mcp("0.0.0.0", cfg.bus_port, cfg.bus_token, agent.tools(), "gdrive-agent", approvals)
        health.endpoint("agent bus", f"http://127.0.0.1:{cfg.bus_port}/health")
    log.info("Starting: daily report at %02d:00, trash prompt over %d%%, assistant=%s, bus=%s", cfg.report_hour,
             cfg.usage_percent, bool(cfg.assistant_url), bool(cfg.bus_token))
    agent.run()


def _revision() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "REVISION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "dev"


if __name__ == "__main__":
    main()
