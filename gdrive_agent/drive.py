"""Google Drive API v3 over plain HTTPS: the storage quota, the change feed, the trash.

These are the only calls the agent makes. Emptying the trash is the only one that changes anything,
and it only runs from the bus tool drive_empty_trash, after the operator approves it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
TIMEOUT = 30
MAX_PAGES = 200  # 1000 items a page: enough for any day's changes or any trash
MAX_DEPTH = 30  # folder levels shown in a path
# Google sends the change feed ~50 records a page whatever pageSize asks for: this covers 250,000 changes,
# e.g. the deletions from emptying a big trash, in one report.
MAX_CHANGE_PAGES = 5000


class DriveError(Exception):
    """Google couldn't be asked, or answered with an error."""


class AuthRejected(DriveError):
    """The refresh token was revoked or expired: a person has to sign in again."""


@dataclass
class Quota:
    limit: int | None  # bytes; None for an account with no limit
    usage: int  # everything that counts: Drive, Gmail, Photos
    drive: int  # Drive alone, trash included
    trash: int

    @property
    def percent(self) -> float | None:
        return None if not self.limit else 100 * self.usage / self.limit

    @classmethod
    def parse(cls, raw: dict) -> "Quota":
        limit = raw.get("limit")
        return cls(int(limit) if limit else None, int(raw.get("usage", 0)), int(raw.get("usageInDrive", 0)),
                   int(raw.get("usageInDriveTrash", 0)))


@dataclass
class Changes:
    """What changed between two page tokens, one verdict per file: its last change wins."""

    files: int = 0
    folders: int = 0
    new_files: int = 0
    new_folders: int = 0
    trashed: int = 0
    removed: int = 0  # deleted for good, or no longer shared with this account
    token: str = ""  # where the next count starts
    complete: bool = True

    @property
    def total(self) -> int:
        return self.files + self.folders + self.trashed + self.removed


def count(changes: list[dict], since: str) -> Changes:
    """Tally Drive change records. `since` is an RFC 3339 time: items created after it count as new."""
    last: dict[str, dict] = {}
    for c in changes:
        if c.get("changeType", "file") == "file" and c.get("fileId"):
            last[c["fileId"]] = c
    out = Changes()
    for c in last.values():
        f = c.get("file")
        if c.get("removed") or not f:
            out.removed += 1
        elif f.get("trashed"):
            out.trashed += 1
        else:
            folder = f.get("mimeType") == FOLDER
            new = bool(since) and (f.get("createdTime") or "") >= since
            if folder:
                out.folders += 1
                out.new_folders += new
            else:
                out.files += 1
                out.new_files += new
    return out


class Drive:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, session: requests.Session | None = None):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.http = session or requests.Session()
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()  # the bus and the daily run share the access token

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            try:
                r = self.http.post(TOKEN_URL, data={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token, "grant_type": "refresh_token"}, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise DriveError(f"can't reach Google sign-in: {type(exc).__name__}") from None
            body = _json(r)
            if r.status_code in (400, 401) and body.get("error") in ("invalid_grant", "unauthorized_client",
                                                                     "invalid_client"):
                raise AuthRejected(f"Google rejected the refresh token ({body.get('error')})")
            if not r.ok or "access_token" not in body:
                raise DriveError(f"Google sign-in failed: HTTP {r.status_code}")
            self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
            return self._token

    def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            try:
                r = self.http.request(method, f"{API}{path}", params=params, timeout=TIMEOUT,
                                      headers={"Authorization": f"Bearer {self._access_token()}"})
            except requests.RequestException as exc:
                raise DriveError(f"can't reach Google Drive: {type(exc).__name__}") from None
            if r.status_code == 401 and attempt == 0:
                self._token = ""  # expired early: sign in again once
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt * 5)
                continue
            if not r.ok:
                reason = (_json(r).get("error") or {}).get("message", "") if r.content else ""
                raise DriveError(f"Drive API {method} {path}: HTTP {r.status_code} {reason}".strip())
            return _json(r) if r.content else {}
        raise DriveError(f"Drive API {method} {path}: gave up after retries")

    # --- Reads ---

    def quota(self) -> Quota:
        return Quota.parse(self._call("GET", "/about", {"fields": "storageQuota"}).get("storageQuota", {}))

    def start_token(self) -> str:
        return self._call("GET", "/changes/startPageToken")["startPageToken"]

    def changes(self, token: str, since: str) -> Changes:
        """Every change after `token`, counted; the result's token is where tomorrow starts."""
        records, page = [], token
        for _ in range(MAX_CHANGE_PAGES):
            body = self._call("GET", "/changes", {
                "pageToken": page, "pageSize": 1000, "includeRemoved": "true", "spaces": "drive",
                "fields": "nextPageToken,newStartPageToken,"
                          "changes(changeType,fileId,removed,file(mimeType,trashed,createdTime))"})
            records += body.get("changes", [])
            if body.get("newStartPageToken"):
                out = count(records, since)
                out.token = body["newStartPageToken"]
                return out
            page = body.get("nextPageToken")
            if not page:
                break
        out = count(records, since)  # too many to finish today: carry on from here tomorrow
        out.token, out.complete = page or token, False
        return out

    def trash_items(self) -> int | None:
        """Items in the trash (files and folders, including the contents of trashed folders); None if
        there are more than this agent will page through."""
        total, page = 0, None
        for _ in range(MAX_PAGES):
            params = {"q": "trashed = true and 'me' in owners", "pageSize": 1000, "spaces": "drive",
                      "fields": "nextPageToken,files(id)"}
            if page:
                params["pageToken"] = page
            body = self._call("GET", "/files", params)
            total += len(body.get("files", []))
            page = body.get("nextPageToken")
            if not page:
                return total
        return None

    def largest(self, n: int) -> list[dict]:
        """The n files you own that take the most space, outside the trash, each with its folder path."""
        body = self._call("GET", "/files", {
            "q": "trashed = false and 'me' in owners", "orderBy": "quotaBytesUsed desc", "pageSize": n,
            "spaces": "drive", "fields": "files(id,name,quotaBytesUsed,parents)"})
        folders: dict[str, tuple[str, list[str]]] = {}  # id -> (name, parents), shared by every path
        return [{"name": f.get("name", ""), "size": int(f.get("quotaBytesUsed") or 0),
                 "path": self._path(f.get("parents") or [], folders)} for f in body.get("files", [])]

    def _path(self, parents: list[str], folders: dict) -> str:
        """Folder names from the top down, without My Drive itself."""
        names, seen = [], set()
        while parents and parents[0] not in seen and len(names) < MAX_DEPTH:
            fid = parents[0]
            seen.add(fid)
            if fid not in folders:
                try:
                    f = self._call("GET", f"/files/{fid}", {"fields": "name,parents"})
                except DriveError:  # a folder we can't see, e.g. someone else's
                    break
                folders[fid] = (f.get("name", ""), f.get("parents") or [])
            name, parents = folders[fid]
            if parents:  # the root (My Drive) is the only folder without a parent
                names.append(name)
        return "/".join(reversed(names))

    # --- The one change ---

    def empty_trash(self) -> None:
        """Permanently deletes everything this account owns in its trash."""
        self._call("DELETE", "/files/trash")


def _json(r: requests.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def size(n: int | None) -> str:
    """Bytes the way Google's storage page shows them (1 GB = 1024³ bytes)."""
    if n is None:
        return "unlimited"
    value, unit = float(n), "bytes"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            break
        value /= 1024
    return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}" if abs(value) < 100 else f"{value:.0f} {unit}"
