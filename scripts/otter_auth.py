"""One-time Otter MCP OAuth helper. Run on your laptop, not the server.

    python scripts/otter_auth.py

Opens a browser for the Otter login, captures the OAuth code on a local
callback server, exchanges it for tokens, and prints the env var values to
paste into Render. Re-run this if the token ever expires and refresh fails.

Uses OAuth 2.0 discovery + dynamic client registration + PKCE, per the MCP
authorization spec.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

MCP_URL = os.environ.get("OTTER_MCP_URL", "https://mcp.otter.ai/mcp")
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

_auth_code: dict = {}
_received = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        if parsed.path != "/callback" or not code:
            # Ignore favicon requests etc.; keep waiting for the real callback.
            self.send_response(404)
            self.end_headers()
            return
        _auth_code["code"] = code
        _auth_code["state"] = query.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Done. You can close this tab and return to the terminal.</h2>")
        _received.set()

    def log_message(self, *args):
        pass


def discover_metadata() -> dict:
    origin = urllib.parse.urlparse(MCP_URL)
    candidates = [f"{origin.scheme}://{origin.netloc}"]
    # Per the MCP auth spec, the resource server points at its authorization
    # server(s) via protected-resource metadata (Otter: mcp.otter.ai -> otter.ai).
    resp = httpx.get(candidates[0] + "/.well-known/oauth-protected-resource", follow_redirects=True)
    if resp.status_code == 200:
        candidates = [u.rstrip("/") for u in resp.json().get("authorization_servers", [])] + candidates
    for base in candidates:
        for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            resp = httpx.get(base + path, follow_redirects=True)
            if resp.status_code == 200:
                return resp.json()
    raise SystemExit(f"Could not discover OAuth metadata (tried {candidates}). "
                     "Check Otter's help center for the current MCP URL.")


def main() -> None:
    meta = discover_metadata()
    register_url = meta.get("registration_endpoint")
    if not register_url:
        raise SystemExit("Otter's OAuth server does not advertise dynamic client "
                         "registration. Check Otter's docs for a manual client id.")
    reg = httpx.post(register_url, json={
        "client_name": "Basanite Intern",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }).json()
    client_id = reg["client_id"]

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if meta.get("scopes_supported"):
        params["scope"] = " ".join(meta["scopes_supported"])
    auth_url = f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    server.timeout = 5

    def _serve():
        while not _received.is_set():
            server.handle_request()

    threading.Thread(target=_serve, daemon=True).start()
    if "--no-browser" in sys.argv:
        print(f"Open this URL in the browser where Otter is logged in:\n\n{auth_url}\n", flush=True)
    else:
        print(f"Opening browser for Otter login...\nIf it doesn't open, visit:\n{auth_url}\n", flush=True)
        webbrowser.open(auth_url)
    if not _received.wait(timeout=300):
        raise SystemExit("Timed out waiting for the OAuth callback — try again.")
    server.server_close()

    if _auth_code.get("state") != state:
        raise SystemExit("OAuth state mismatch — try again.")
    tokens = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": _auth_code["code"],
        "redirect_uri": REDIRECT_URI, "client_id": client_id, "code_verifier": verifier,
    }).json()
    if "access_token" not in tokens:
        raise SystemExit(f"Token exchange failed: {json.dumps(tokens)}")

    values = {
        "OTTER_ACCESS_TOKEN": tokens["access_token"],
        "OTTER_REFRESH_TOKEN": tokens.get("refresh_token", ""),
        "OTTER_TOKEN_URL": meta["token_endpoint"],
        "OTTER_CLIENT_ID": client_id,
    }
    if "--write-env" in sys.argv and os.path.exists(".env"):
        lines = open(".env", encoding="utf-8").read().splitlines()
        keys_written = set()
        for i, line in enumerate(lines):
            key = line.split("=", 1)[0]
            if key in values:
                lines[i] = f"{key}={values[key]}"
                keys_written.add(key)
        lines += [f"{k}={v}" for k, v in values.items() if k not in keys_written]
        open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
        print("Updated .env with fresh Otter credentials (values not printed).")
        print("Remember to copy them into the Render env vars before deploying.")
    else:
        print("\nPaste these into the Render env vars (both services):\n")
        for k, v in values.items():
            print(f"{k}={v}")


if __name__ == "__main__":
    main()
