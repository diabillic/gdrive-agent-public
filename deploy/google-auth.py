#!/usr/bin/env python3
"""One-time Google sign-in for the agent: prints a refresh token, nothing else, on stdout.

    python deploy/google-auth.py client_secret.json | gh secret set GOOGLE_REFRESH_TOKEN

client_secret.json is the "Desktop app" OAuth client you download from Google Cloud Console (see README).
Your browser opens Google's consent page; after you allow it, Google redirects to a one-off listener on
127.0.0.1 and this script trades the code for tokens (PKCE, standard library only). Run it on a machine
with a browser. The token never touches disk.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/drive"  # files.emptyTrash accepts no narrower scope
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as fh:
        raw = json.load(fh)
    client = raw.get("installed")
    if not client:
        sys.exit("That isn't a Desktop app OAuth client (no 'installed' section): create one of type Desktop app.")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            got.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done: you can close this tab and go back to the terminal.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_address[1]}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Opening Google's consent page. If no browser opens, visit:\n{url}\n", file=sys.stderr)
    webbrowser.open(url)
    while not got:
        server.handle_request()
    server.server_close()

    if got.get("state") != state:
        sys.exit("The reply didn't match this sign-in (state mismatch); run it again.")
    if "error" in got:
        sys.exit(f"Google said: {got['error']}")
    body = urllib.parse.urlencode({
        "code": got["code"], "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as resp:
        tokens = json.load(resp)
    if "refresh_token" not in tokens:
        sys.exit("Google sent no refresh token. Remove the app's access at myaccount.google.com/permissions and retry.")
    if SCOPE not in tokens.get("scope", "").split():
        sys.exit("Drive access wasn't granted: tick the Drive box on the consent page.")
    print("Signed in. The refresh token is on stdout.", file=sys.stderr)
    print(tokens["refresh_token"])


if __name__ == "__main__":
    main()
