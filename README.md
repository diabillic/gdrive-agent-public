# gdrive-agent

Watches a Google account's storage. Once a day it posts a report to Discord with:

- **Space used:** how much of the storage limit is used, split into Drive and the rest (Gmail and Photos share
  the same limit), and the change since the last report.
- **The trash:** its size and how many items are in it.
- **Changes:** how many files and folders were added or changed since the last report, how many of them are
  new, how many went to the trash, and how many were deleted for good.
- **The largest files:** the `TOP_FILES` (default 10) files you own that take the most space, outside the
  trash, each with its folder.

When usage goes over `USAGE_PERCENT` (default 75%) and the trash isn't empty, the agent asks your assistant to
empty the trash. You decide with the assistant's **Allow / Deny** buttons on Discord. The agent never empties
the trash on its own.

It runs as a Docker container and talks to the Drive API v3 directly.

## The daily report

```
☁️ Google Drive 81% full

💾 Storage
12.2 GB of 15.0 GB used (81%)
Drive 10.9 GB · Gmail and Photos 1.3 GB
+240.5 MB since the last report

🗑️ Trash
2.3 GB in 1,204 items

📝 Changes
Since Sat 07 Nov 08:00:
1,312 files added or changed (57 new)
14 folders added or changed (3 new)
40 items moved to the trash

📦 Largest 10 files
75.2 GB · `appdata.tar.zst` · in `Backups/appdata/2026-11-01`
64.1 GB · `appdata.tar.zst` · in `Backups/appdata/2026-10-25`
…

⚠️ Over the limit
Storage is over the 75% limit. Emptying the trash would free about 2.3 GB; the assistant will ask you to Allow or Deny it.
```

Changes come from Drive's change feed. Each item counts once, however often it changed, and the last change
decides how it counts. Something changed and then trashed counts as trashed. Changes to files that others
share with you count too. "Deleted for good" also covers items that are no longer shared with you, because the
feed can't tell those apart.

The first report after a fresh start counts changes from when the container started. A brand new deployment
sends its first report at the next `REPORT_HOUR`.

## Emptying the trash, with your approval

Discord webhooks can't take button clicks, so the agent goes through an assistant that already has Allow / Deny
buttons. It was built for [Hermes Agent](https://github.com/NousResearch/hermes-agent) with an approval plugin
that asks before any MCP tool marked destructive:

1. Over the limit, the daily report says so, and the agent posts a message to `ASSISTANT_URL` (a webhook in a
   channel the assistant reads) that @mentions the assistant (`ASSISTANT_USER_ID`). The message asks it to
   call `drive_empty_trash`.
2. The assistant calls the tool on the agent bus. Its approval plugin sees the tool is destructive and shows
   **Allow / Deny**.
3. On Allow, the agent announces the action to `APPROVAL_NOTIFY_URLS`, empties the trash
   (`DELETE /drive/v3/files/trash`), and posts the result there and in the report channel. On Deny, nothing
   happens.

It asks at most once a day, and only from the daily report (or a report you ask for), so a denied request comes
back with the next day's report while usage stays high. Asking the assistant yourself ("empty the Google Drive
trash") goes through the same buttons at any usage.

**Hermes settings this needs:**

- `DISCORD_ALLOW_BOTS=mentions` in Hermes's `.env`. Hermes ignores messages from bots and webhooks by default.
  With `mentions` it reads them only when they @mention it, which skips its user allowlist for those messages.
  The buttons still only work for allowed users.
- The agent in `mcp_servers` (for example `gdrive` → `http://<host>:<port>/mcp` with
  `Authorization: Bearer ${AGENT_BUS_TOKEN}`), and its name in the approval plugin's `AGENT_BUS_SERVERS`.
- `APPROVAL_MODE=client` on this agent, so it trusts the plugin's buttons. With `code` (the default) the agent
  sends a one-time code to `APPROVAL_NOTIFY_URLS` instead, and you give the code to the assistant.

Emptying the trash deletes only items you own, and it can't be undone. Google can take a while to give the
space back. The result shows what the trash held before and after, and the next report shows where usage ends
up.

## Bus tools

| Tool | What it does |
|---|---|
| `drive_status` | Live usage, limit and trash size, plus the last report (read-only) |
| `drive_empty_trash` | Permanently empties the trash (destructive: goes through approval) |
| `agent_status`, `agent_last_diagnosis` | The agent's revision, uptime and open problems (read-only) |
| `agent_run_check` | Runs a report now. It won't ask about the trash again on a day it already asked |

## Google sign-in

The agent uses your own OAuth client, signed in once as you. The trash call needs the full Drive scope
(`https://www.googleapis.com/auth/drive`): Google has no narrower scope that allows it. The agent makes only
the calls in `gdrive_agent/drive.py`: the storage quota, the change feed, a count of trashed items, the
largest files and their folders' names, and emptying the trash.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Google Drive
   API**.
2. **Google Auth Platform → Branding/Audience**: user type External, add yourself as a test user, then **Publish
   app** (status *In production*). While an app is in *Testing*, Google expires its refresh tokens after 7 days.
   You don't need verification for your own account: the consent screen warns that the app is unverified, and
   you continue past it.
3. **Clients → Create client → Desktop app**, and download its JSON.
4. On a machine with a browser:

   ```bash
   python deploy/google-auth.py client_secret.json | gh secret set GOOGLE_REFRESH_TOKEN -R <you>/gdrive-agent
   gh secret set GOOGLE_CLIENT_ID -R <you>/gdrive-agent        # the client's id
   gh secret set GOOGLE_CLIENT_SECRET -R <you>/gdrive-agent    # the client's secret
   ```

   The script prints only the refresh token on stdout, so it goes straight into the secret. Delete the JSON
   afterwards, or keep it somewhere safe.

If Google rejects the token later (you revoked access or changed your password), the agent posts once with
this fix, and posts again when the sign-in works.

## Doing a run by hand

```bash
docker exec gdrive-agent python -m gdrive_agent check              # changes nothing, prints today's numbers
docker exec gdrive-agent touch /var/lib/gdrive-agent/run-now       # a real report, within a minute
```

## Deployment

Every push to `main` runs the tests, then deploys to the Docker host with `agent-deploy`, a small root-owned
script that builds the image and replaces the container. To run it elsewhere, build the `Dockerfile` and pass the
settings in `.env.example`. State (the change-feed position, the last report, unsent messages) is in the
`gdrive-agent-data` volume.

### Secrets and variables

| Kind | Name | |
|---|---|---|
| secret | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Required. See Google sign-in |
| secret | `NOTIFY_URLS` | Required. Discord webhook or Apprise URLs for the report |
| secret | `ASSISTANT_URL` | Discord webhook in the assistant's channel |
| secret | `AGENT_BUS_TOKEN` | Bearer token for the agent bus; empty turns it off |
| secret | `APPROVAL_NOTIFY_URLS` | Audit channel for actions; without it `drive_empty_trash` always refuses |
| variable | `AGENT_HOST` | Docker host to deploy to; empty means no deploy |
| variable | `PRIORITY` | `high` or `low`: which runner deploys |
| variable | `BUS_HOST_PORT` | Host port for the bus |
| variable | `ASSISTANT_USER_ID` | The assistant's Discord user ID |
| variable | `APPROVAL_MODE` | `client` (the assistant's buttons) or `code` |
| variable | `REPORT_HOUR`, `USAGE_PERCENT`, `TOP_FILES`, `AGENT_BUS_PORT` | Optional; see `.env.example` |
