from datetime import datetime

import pytest

from gdrive_agent import drive as drive_mod
from gdrive_agent.__main__ import Agent, utc_stamp
from gdrive_agent.bus import Approvals
from gdrive_agent.config import Config
from gdrive_agent.drive import FOLDER, AuthRejected, Changes, Drive, DriveError, Quota, count, size
from gdrive_agent.state import State

GB = 1024 ** 3
ASSISTANT = "123456789012345678"


def ts(text):
    return datetime.fromisoformat(text).timestamp()


# --- Fakes ---

class FakeDrive:
    def __init__(self, usage=5 * GB, limit=15 * GB, trash=1 * GB, items=10):
        self.q = Quota(limit, usage, usage - GB // 2, trash)
        self.items, self.changed, self.emptied, self.fail, self.tokens = items, Changes(), 0, None, 0
        self.top, self.top_fail = [{"name": "big_*file*.iso", "size": 40 * GB, "path": "Backups/2026"},
                                   {"name": "loose.bin", "size": GB, "path": ""}], None

    def quota(self):
        if self.fail:
            raise self.fail
        return Quota(self.q.limit, self.q.usage, self.q.drive, self.q.trash)

    def trash_items(self):
        return self.items

    def start_token(self):
        self.tokens += 1
        return f"t{self.tokens}"

    def changes(self, token, since):
        self.asked_with = (token, since)
        out = Changes(**{k: getattr(self.changed, k) for k in ("files", "folders", "new_files", "new_folders",
                                                              "trashed", "removed", "complete")})
        out.token = token + "+"
        return out

    def largest(self, n):
        if self.top_fail:
            raise self.top_fail
        return self.top[:n]

    def empty_trash(self):
        self.emptied += 1
        self.q.usage -= self.q.trash
        self.q.trash = 0


class FakeOutbox:
    def __init__(self):
        self.sent, self.down = [], False

    def __len__(self):
        return 1

    def send(self, msg):
        if self.down:
            return False
        self.sent.append(msg)
        return True


class Reply:
    def __init__(self, status=204):
        self.status_code, self.ok = status, status < 300


class FakePost:
    def __init__(self, status=204):
        self.calls, self.status = [], status

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return Reply(self.status)


def make(tmp_path, assistant=True, **drive_kw):
    cfg = Config("id", "secret", "refresh", ["discord://x"], report_hour=8, usage_percent=75,
                 assistant_url="https://hook.example/assistant" if assistant else "",
                 assistant_user_id=ASSISTANT if assistant else "", state_dir=str(tmp_path))
    post = FakePost()
    agent = Agent(cfg, State(cfg.state_path), FakeOutbox(), FakeDrive(**drive_kw), post=post)
    return agent, post


def text(msg):
    return msg.title + "\n" + msg.markdown()


# --- Counting changes ---

def change(fid, mime="text/plain", created="2026-11-01T00:00:00.000Z", trashed=False, removed=False):
    c = {"changeType": "file", "fileId": fid, "removed": removed}
    if not removed:
        c["file"] = {"mimeType": mime, "createdTime": created, "trashed": trashed}
    return c


def test_count_last_change_per_item_wins():
    since = "2026-11-07T13:00:00.000Z"
    out = count([
        change("a"), change("a"),  # one file, changed twice
        change("b", created="2026-11-07T20:00:00.123Z"),  # new file
        change("f", FOLDER, created="2026-11-08T01:00:00.000Z"),  # new folder
        change("g", FOLDER),
        change("c"), change("c", trashed=True),  # changed, then trashed: counts as trashed only
        change("d", removed=True),
        {"changeType": "drive", "driveId": "x"},  # shared-drive record: ignored
    ], since)
    assert (out.files, out.new_files, out.folders, out.new_folders, out.trashed, out.removed) == (2, 1, 2, 1, 1, 1)
    assert out.total == 6


def test_count_without_a_start_time_calls_nothing_new():
    assert count([change("a", created="2030-01-01T00:00:00.000Z")], "").new_files == 0


def test_size_matches_googles_units():
    assert size(15 * GB) == "15.0 GB"
    assert size(512) == "512 bytes"
    assert size(int(2.5 * 1024 ** 2)) == "2.5 MB"
    assert size(None) == "unlimited"


# --- Drive client ---

class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.ok, self._body = status, status < 300, body
        self.content = b"" if body is None else b"x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, token_reply, replies):
        self.token_reply, self.replies, self.requests = token_reply, list(replies), []

    def post(self, url, data, timeout):
        return self.token_reply

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw.get("params")))
        return self.replies.pop(0)


def test_drive_pages_through_changes_and_keeps_the_new_start_token():
    s = FakeSession(Resp(200, {"access_token": "a", "expires_in": 3600}), [
        Resp(200, {"changes": [change("a")], "nextPageToken": "p2"}),
        Resp(200, {"changes": [change("b", FOLDER)], "newStartPageToken": "next"}),
    ])
    out = Drive("id", "secret", "refresh", s).changes("start", "")
    assert (out.files, out.folders, out.token, out.complete) == (1, 1, "next", True)
    assert [r[2]["pageToken"] for r in s.requests] == ["start", "p2"]


def test_drive_reports_a_revoked_token_as_auth_rejected():
    s = FakeSession(Resp(400, {"error": "invalid_grant"}), [])
    with pytest.raises(AuthRejected):
        Drive("id", "secret", "refresh", s).quota()


def test_drive_quota_and_empty_trash(monkeypatch):
    s = FakeSession(Resp(200, {"access_token": "a"}), [
        Resp(200, {"storageQuota": {"limit": str(15 * GB), "usage": str(12 * GB), "usageInDrive": str(10 * GB),
                                    "usageInDriveTrash": str(2 * GB)}}),
        Resp(204),
    ])
    d = Drive("id", "secret", "refresh", s)
    q = d.quota()
    assert (round(q.percent), q.trash) == (80, 2 * GB)
    d.empty_trash()
    assert s.requests[-1][:2] == ("DELETE", drive_mod.API + "/files/trash")


def test_drive_error_names_the_http_status(monkeypatch):
    monkeypatch.setattr(drive_mod.time, "sleep", lambda s: None)
    s = FakeSession(Resp(200, {"access_token": "a"}), [Resp(403, {"error": {"message": "insufficient scope"}})])
    with pytest.raises(DriveError, match="403 insufficient scope"):
        Drive("id", "secret", "refresh", s).quota()


def test_unlimited_quota_has_no_percent():
    assert Quota.parse({"usage": "5"}).percent is None


# --- The daily report ---

def test_first_start_counts_from_now_and_waits_for_report_hour(tmp_path):
    agent, _ = make(tmp_path)
    agent.start(ts("2026-11-08T10:00"))
    assert agent.run_state["token"] == "t1" and agent.run_state["ran"] == "2026-11-08"
    agent.tick(ts("2026-11-08T11:00"))
    assert agent.outbox.sent == []
    agent.tick(ts("2026-11-09T08:00"))
    assert len(agent.outbox.sent) == 1


def test_report_shows_storage_trash_and_changes(tmp_path):
    agent, post = make(tmp_path)
    agent.start(ts("2026-11-07T07:00"))
    agent.drive.changed = Changes(files=12, new_files=3, folders=2, new_folders=1, trashed=4, removed=1)
    agent.run_state["usage"] = 4 * GB
    agent.tick(ts("2026-11-07T08:00"))
    body = text(agent.outbox.sent[0])
    assert "33% full" in body and "5.0 GB of 15.0 GB used" in body
    assert "+1.0 GB since the last report" in body
    assert "1.0 GB in 10 items" in body
    assert "12 files added or changed (3 new)" in body and "2 folders added or changed (1 new)" in body
    assert "4 items moved to the trash" in body and "1 item deleted for good" in body
    assert agent.drive.asked_with[0] == "t1"
    assert agent.run_state["token"] == "t1+"
    assert post.calls == []  # under the limit: nothing to ask
    assert agent.outbox.sent[0].level == "info"


def test_over_the_limit_asks_the_assistant_once_a_day(tmp_path):
    agent, post = make(tmp_path, usage=12 * GB, trash=2 * GB)
    agent.start(ts("2026-11-07T07:00"))
    agent.tick(ts("2026-11-07T08:00"))
    report = agent.outbox.sent[0]
    assert report.level == "warning" and "free about 2.0 GB" in text(report) and "Allow or Deny" in text(report)
    assert len(post.calls) == 1
    url, kw = post.calls[0]
    assert url == "https://hook.example/assistant"
    assert kw["json"]["content"].startswith(f"<@{ASSISTANT}> ")
    assert "drive_empty_trash" in kw["json"]["content"]
    assert kw["json"]["allowed_mentions"] == {"users": [ASSISTANT]}
    agent.runtime.request_check()  # a check from the bus the same day doesn't prompt again
    agent.tick(ts("2026-11-07T09:00"))
    assert len(agent.outbox.sent) == 2 and len(post.calls) == 1
    agent.tick(ts("2026-11-08T08:00"))  # still over the next day: asks again
    assert len(post.calls) == 2


def test_exactly_at_the_limit_is_not_over(tmp_path):
    agent, post = make(tmp_path, usage=int(11.25 * GB), trash=GB)  # 75.0%
    agent.report(ts("2026-11-07T08:00"))
    assert post.calls == []


def test_over_the_limit_with_an_empty_trash_just_says_so(tmp_path):
    agent, post = make(tmp_path, usage=12 * GB, trash=0)
    agent.report(ts("2026-11-07T08:00"))
    assert "the trash is empty" in text(agent.outbox.sent[0])
    assert post.calls == []


def test_without_an_assistant_the_report_says_what_to_ask(tmp_path):
    agent, post = make(tmp_path, assistant=False, usage=12 * GB)
    agent.report(ts("2026-11-07T08:00"))
    assert "ask your assistant to run the gdrive agent's drive_empty_trash" in text(agent.outbox.sent[0])
    assert post.calls == []


def test_a_failed_prompt_is_reported_and_retried_next_check(tmp_path):
    agent, post = make(tmp_path, usage=12 * GB)
    post.status = 404
    agent.report(ts("2026-11-07T08:00"))
    assert "Couldn't ask the assistant" in agent.outbox.sent[-1].title
    post.status = 204
    agent.report(ts("2026-11-07T09:00"))
    assert len(post.calls) == 2 and agent.run_state["asked"] == "2026-11-07"


def test_sign_in_rejected_is_said_once_then_resolved(tmp_path):
    agent, _ = make(tmp_path)
    agent.drive.fail = AuthRejected("Google rejected the refresh token (invalid_grant)")
    agent.report(ts("2026-11-07T08:00"))
    agent.report(ts("2026-11-08T08:00"))
    errors = [m for m in agent.outbox.sent if m.level == "error"]
    assert len(errors) == 1 and "google-auth.py" in text(errors[0])
    assert agent.runtime.snapshot()["openFindings"][0]["key"] == "google"
    agent.drive.fail = None
    agent.report(ts("2026-11-09T08:00"))
    assert any(m.title.startswith("✅ Resolved") for m in agent.outbox.sent)
    assert agent.runtime.snapshot()["openFindings"] == []


def test_messages_wait_for_discord(tmp_path):
    agent, _ = make(tmp_path)
    agent.outbox.down = True
    agent.report(ts("2026-11-07T08:00"))
    assert agent.outbox.sent == [] and len(agent.state["pending"]["messages"]) == 1
    agent.outbox.down = False
    agent.flush()
    assert len(agent.outbox.sent) == 1 and agent.state["pending"]["messages"] == []


def test_run_now_file(tmp_path):
    agent, _ = make(tmp_path)
    agent.start(ts("2026-11-07T09:00"))
    open(agent.cfg.run_now_path, "w").close()
    agent.tick(ts("2026-11-07T10:00"))
    assert len(agent.outbox.sent) == 1


def test_report_lists_the_largest_files_with_their_folders(tmp_path):
    agent, _ = make(tmp_path)
    agent.report(ts("2026-11-07T08:00"))
    body = text(agent.outbox.sent[0])
    assert "📦 Largest 2 files" in body
    assert "**40.0 GB** · `big_*file*.iso` · in `Backups/2026`" in body
    assert "**1.0 GB** · `loose.bin` · in My Drive" in body
    assert agent.state["report"]["largest"][0] == {"name": "big_*file*.iso", "size": "40.0 GB", "path": "Backups/2026"}


def test_report_still_goes_out_when_the_largest_files_cant_be_listed(tmp_path):
    agent, _ = make(tmp_path)
    agent.drive.top_fail = DriveError("HTTP 500")
    agent.report(ts("2026-11-07T08:00"))
    assert "Couldn't list them today." in text(agent.outbox.sent[0])


def test_top_files_zero_leaves_the_list_out(tmp_path):
    agent, _ = make(tmp_path)
    agent.cfg.top_files = 0
    agent.report(ts("2026-11-07T08:00"))
    assert "Largest" not in text(agent.outbox.sent[0])


def test_drive_largest_builds_paths_once_per_folder():
    s = FakeSession(Resp(200, {"access_token": "a"}), [
        Resp(200, {"files": [{"id": "1", "name": "a.tar", "quotaBytesUsed": str(3 * GB), "parents": ["d2"]},
                             {"id": "2", "name": "b.tar", "quotaBytesUsed": str(2 * GB), "parents": ["d2"]},
                             {"id": "3", "name": "c.txt", "quotaBytesUsed": "5", "parents": ["root"]}]}),
        Resp(200, {"name": "2026", "parents": ["d1"]}),
        Resp(200, {"name": "Backups", "parents": ["root"]}),
        Resp(200, {"name": "My Drive"}),
    ])
    out = Drive("id", "secret", "refresh", s).largest(10)
    assert [(f["name"], f["size"], f["path"]) for f in out] == [
        ("a.tar", 3 * GB, "Backups/2026"), ("b.tar", 2 * GB, "Backups/2026"), ("c.txt", 5, "")]
    assert s.requests[0][2]["orderBy"] == "quotaBytesUsed desc"
    assert len(s.requests) == 4  # one list, then each folder once


# --- Bus tools ---

def test_empty_trash_is_the_only_destructive_tool(tmp_path):
    agent, _ = make(tmp_path)
    tools = {t.name: t for t in agent.tools()}
    assert [n for n, t in tools.items() if not t.read_only] == ["drive_empty_trash"]
    assert tools["drive_empty_trash"].input_schema["properties"] == {}


def test_empty_trash_through_client_approval_announces_and_reports(tmp_path, monkeypatch):
    agent, _ = make(tmp_path, usage=12 * GB, trash=2 * GB)
    audit = []
    approvals = Approvals("gdrive", ["json://localhost/audit"], mode="client")
    monkeypatch.setattr(approvals.outbox, "send", lambda msg: audit.append(msg.title) or True)
    tool = {t.name: t for t in agent.tools()}["drive_empty_trash"]
    result = approvals.handle(tool, {})
    assert result["status"] == "done" and result["result"]["trash_before"] == "2.0 GB"
    assert result["result"]["percent_now"] == pytest.approx(66.7, abs=0.1)
    assert agent.drive.emptied == 1
    assert audit == ["▶️ Running an approved action", "✅ Action done"]
    assert agent.outbox.sent[-1].title == "🗑️ Google Drive trash emptied"


def test_empty_trash_never_runs_without_an_audit_channel(tmp_path):
    agent, _ = make(tmp_path)
    tool = {t.name: t for t in agent.tools()}["drive_empty_trash"]
    assert Approvals("gdrive", [], mode="client").handle(tool, {})["status"] == "refused"
    assert agent.drive.emptied == 0


def test_status_tool(tmp_path):
    agent, _ = make(tmp_path, usage=12 * GB)
    agent.report(ts("2026-11-07T08:00"))
    s = agent.status()
    assert s["over_limit"] and s["percent"] == 80.0 and s["last_report"]["trash_items"] == 10


# --- Config ---

ENV = {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "s", "GOOGLE_REFRESH_TOKEN": "r", "NOTIFY_URLS": "json://x"}


def test_config_defaults(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    cfg = Config.from_env()
    assert (cfg.report_hour, cfg.usage_percent, cfg.approval_mode, cfg.assistant_url) == (8, 75, "code", "")


@pytest.mark.parametrize("extra, message", [
    ({"GOOGLE_REFRESH_TOKEN": ""}, "GOOGLE_REFRESH_TOKEN"),
    ({"USAGE_PERCENT": "0"}, "USAGE_PERCENT"),
    ({"ASSISTANT_URL": "https://hook.example"}, "go together"),
    ({"ASSISTANT_URL": "https://hook.example", "ASSISTANT_USER_ID": "@bot"}, "digits"),
])
def test_config_rejects(monkeypatch, extra, message):
    for k, v in {**ENV, **extra}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match=message):
        Config.from_env()


def test_utc_stamp_compares_with_drive_times():
    assert utc_stamp(0) == "1970-01-01T00:00:00.000Z"
    assert "2026-11-07T20:00:00.123Z" > utc_stamp(ts("2026-11-07T12:00") - 0)  # format check only
