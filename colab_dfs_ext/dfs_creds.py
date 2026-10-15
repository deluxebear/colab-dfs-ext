#!/usr/bin/env python3
"""One-time Google consent + refresh-token store for browser-free Colab Drive mounts.

Background: `colab drivemount` needs a browser because Colab's dfs_ephemeral credential
propagation is gated on an account-level Google consent (response_type=none+gsession, i.e.
it needs browser cookies).  See docs/REVERSE.md in this directory.

This tool mints its OWN long-lived refresh token that carries the scopes DriveFS requires,
so every later mount is pure HTTP with no browser.

Two flavors:
  sdk  : reuse colab-cli's cloud-SDK installed-app client + Google's copy-paste landing page
         (works on headless/remote boxes, no localhost callback needed).  Recommended.
  dfs  : use the OAuth client that is compiled into /opt/google/drive/drive itself
         (loopback localhost callback).  Enables `drive_mount.py --mode rt`, which needs
         no metadata shim at all.

Run it with any python that has google-auth / google-auth-oauthlib installed; if this
interpreter lacks them it re-execs itself with the colab-cli venv python automatically.

    python3 dfs_creds.py consent --flavor sdk
    python3 dfs_creds.py show
    python3 dfs_creds.py token            # access-token JSON on stdout (refreshes as needed)
    python3 dfs_creds.py clear
"""
from __future__ import annotations  # keeps `str | None` annotations importable on py<3.10


import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------------------
# dependencies: fall back to the colab-cli uv tool venv, which ships google-auth + oauthlib
# --------------------------------------------------------------------------------------
_VENV_PYTHONS = [
    os.path.expanduser("~/.local/share/uv/tools/google-colab-cli/bin/python3"),
    os.path.expanduser("~/.local/share/uv/tools/colab-cli/bin/python3"),
]


def _ensure_deps() -> None:
    try:
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401

        return
    except ImportError:
        pass
    me = os.path.realpath(__file__)
    for py in _VENV_PYTHONS:
        if os.path.realpath(sys.executable) == os.path.realpath(py):
            break
        if not os.path.exists(py):
            continue
        probe = subprocess.run(
            [py, "-c", "import google.oauth2.credentials, google_auth_oauthlib.flow"],
            capture_output=True,
        )
        if probe.returncode == 0:
            os.execv(py, [py, me] + sys.argv[1:])
    sys.exit(
        "need google-auth + google-auth-oauthlib:\n"
        "  pip install google-auth google-auth-oauthlib\n"
        "  (or `uv tool install google-colab-cli`)"
    )


# Standalone guard: re-exec into an interpreter that has google-auth when run as a script.
# When imported from inside the colab CLI we are already in an environment that has it, and
# os.execv() in an imported module would be a very unpleasant surprise.
_DEPS_OK = True
try:
    import google.oauth2.credentials  # noqa: F401
    import google_auth_oauthlib.flow  # noqa: F401
except ImportError:  # pragma: no cover
    _DEPS_OK = False
    if __name__ == "__main__":
        _ensure_deps()

if _DEPS_OK:
    from google.auth.transport.requests import Request  # noqa: E402
    from google.oauth2.credentials import Credentials  # noqa: E402
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
else:  # pragma: no cover
    Request = Credentials = InstalledAppFlow = None

# --------------------------------------------------------------------------------------

STORE = os.path.expanduser("~/.config/colab-cli/dfs_credentials.json")
PENDING = os.path.expanduser("~/.config/colab-cli/dfs_pending.json")
CLI_TOKEN = os.path.expanduser("~/.config/colab-cli/token.json")
CLI_OAUTH_CONFIG = os.path.expanduser("~/.colab-cli-oauth-config.json")

# Colab's own Drive consent URL (captured live, see evidence/colab-drive-auth-url.txt) asks
# for exactly this set; DriveFS 403s on drivefrontend-pa.googleapis.com without `drive`.
DFS_SCOPES = [
    "email",
    "https://www.googleapis.com/auth/client_channel",
    "https://www.googleapis.com/auth/docs.test",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/drive.photos.readonly",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "https://www.googleapis.com/auth/peopleapi.readonly",
]

# https://www.googleapis.com/auth/drive alone is what the FUSE mount actually needs to talk to
# the Drive API; the extras only silence DriveFS' config/phenotype calls.
DFS_SCOPES_MINIMAL = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive",
]

# Google hands this client a slightly larger grant than we ask for (it adds openid +
# userinfo.email). oauthlib treats "scope changed" as an exception, which would throw the
# whole consent away, so relax it. Also see: never pass include_granted_scopes for this
# client -- it re-attaches https://www.googleapis.com/auth/flexible-api, which Google cannot
# render -> 400 invalid_scope.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Registered landing page of Google's cloud-SDK client: displays the code for copy & paste.
# (Same trick colab-cli uses in colab_cli/auth.py:_run_remote_flow.)
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"

# OAuth client compiled into /opt/google/drive/drive (extracted from the binary; the secret
# sits immediately next to the client id in .rodata).
DFS_CLIENT_ID = "947318989803-k88lapdik9bledfml8rr69ic6d3rdv57.apps.googleusercontent.com"
DFS_CLIENT_SECRET = "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"
DFS_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _in_cli() -> bool:
    """True when this module runs inside the colab CLI extension (not as a standalone script)."""
    return bool(__package__) and __name__ != "__main__"


def _mount_hint() -> str:
    return "colab dfs mount -s <session>" if _in_cli() else "python3 drive_mount.py -s <session>"


def load() -> dict:
    if not os.path.exists(STORE):
        return {}
    with open(STORE) as f:
        return json.load(f)


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def _sdk_client_config() -> dict:
    for path in (CLI_OAUTH_CONFIG, os.path.expanduser("~/.config/colab-cli/oauth_config.json")):
        if os.path.exists(path):
            with open(path) as f:
                cfg = json.load(f)
            return cfg.get("installed", cfg)
    # colab-cli bundles the same cloud-SDK client config inside the wheel.
    for py in _VENV_PYTHONS:
        if not os.path.exists(py):
            continue
        code = (
            "import json,os,importlib.util;"
            "s=importlib.util.find_spec('colab_cli');"
            "roots=list(getattr(s,'submodule_search_locations',None) or []);"
            "f=getattr(s,'origin',None);"
            "roots.append(os.path.dirname(f)) if f and f!='none' else None;"
            "p=next((os.path.join(r,'oauth_config.json') for r in roots "
            "if os.path.exists(os.path.join(r,'oauth_config.json'))),None);"
            "print(json.dumps(json.load(open(p))) if p else None)"
        )
        r = subprocess.run([py, "-c", code], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "None":
            cfg = json.loads(r.stdout)
            return cfg.get("installed", cfg)
    raise SystemExit(
        "no client config found: pass --client-oauth-config <json> or install google-colab-cli"
    )


def _userinfo(access_token: str) -> dict:
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": "Bearer " + access_token},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _flow_for(flavor: str, scopes: list[str], port: int, redirect_uri: str | None,
             client_id: str | None = None, client_secret: str | None = None,
             token_uri: str | None = None):
    """Return (flow, client_id, client_secret, token_uri) for a consent flavor."""
    if flavor == "custom":
        if not client_id:
            raise SystemExit("--client-id is required when using your own OAuth client")
        ru = redirect_uri or f"http://localhost:{port}"
        client = {
            "client_id": client_id,
            "client_secret": client_secret or "",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": token_uri or DFS_TOKEN_URI,
            "redirect_uris": [ru],
        }
        flow = InstalledAppFlow.from_client_config({"installed": client}, scopes)
        flow.redirect_uri = ru
        return flow, client_id, client_secret or "", client["token_uri"]
    if flavor == "sdk":
        client = _sdk_client_config()
        client["redirect_uris"] = list(client.get("redirect_uris") or []) + [REMOTE_REDIRECT_URI]
        flow = InstalledAppFlow.from_client_config({"installed": client}, scopes)
        flow.redirect_uri = redirect_uri or REMOTE_REDIRECT_URI
        return (flow, client["client_id"], client.get("client_secret", ""),
                getattr(flow, "token_uri", None) or "https://oauth2.googleapis.com/token")
    client = {
        "client_id": DFS_CLIENT_ID,
        "client_secret": DFS_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": DFS_TOKEN_URI,
        "redirect_uris": [f"http://localhost:{port}"],
    }
    flow = InstalledAppFlow.from_client_config({"installed": client}, scopes)
    flow.redirect_uri = redirect_uri or f"http://localhost:{port}"
    return flow, DFS_CLIENT_ID, DFS_CLIENT_SECRET, DFS_TOKEN_URI


def do_consent(args) -> None:
    scopes = DFS_SCOPES_MINIMAL if args.minimal else DFS_SCOPES
    if args.scopes:
        scopes = args.scopes.split()
    flavor = "custom" if (args.client_id and args.flavor == "sdk") else args.flavor
    cid, csec, turi = (args.client_id or os.environ.get("COLAB_DRIVE_CLIENT_ID"),
                       args.client_secret or os.environ.get("COLAB_DRIVE_CLIENT_SECRET"),
                       args.token_uri or os.environ.get("COLAB_DRIVE_TOKEN_URI"))

    # ---- step 2 of the two-step (agent friendly) flow ---------------------------------
    if args.code:
        if not os.path.exists(PENDING):
            raise SystemExit(f"nothing to finish: {PENDING} missing; run --url-only first")
        pend = json.load(open(PENDING))
        flow, client_id, client_secret, token_uri = _flow_for(
            pend["flavor"], pend["scopes"], pend.get("port", 8091), pend["redirect_uri"],
            pend.get("client_id"), pend.get("client_secret"), pend.get("token_uri"),
        )
        flow.oauth2state = pend.get("state")
        flow.fetch_token(code=args.code)
        creds = flow.credentials
        flavor = pend["flavor"]
    else:
        # ---- step 1: just hand out the URL (no stdin needed) --------------------------
        if args.url_only:
            flow, client_id, client_secret, token_uri = _flow_for(
                flavor, scopes, args.port, args.redirect_uri, cid, csec, turi
            )
            kwargs = {"prompt": "consent", "access_type": "offline"}
            if getattr(args, "include_granted_scopes", False):
                kwargs["include_granted_scopes"] = "true"
            if flavor == "sdk":
                kwargs["token_usage"] = "remote"
            auth_url, state = flow.authorization_url(**kwargs)
            save_pending({"flavor": flavor, "scopes": scopes, "state": state,
                          "redirect_uri": flow.redirect_uri, "port": args.port,
                          "client_id": client_id, "client_secret": client_secret,
                          "token_uri": token_uri,
                          "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            print(auth_url)
            print(f"\n# open the URL, approve, then finish with:\n"
                  f"#   python3 {os.path.basename(__file__)} consent --code <PASTED_CODE>",
                  file=sys.stderr)
            return

        flow, client_id, client_secret, token_uri = _flow_for(
            flavor, scopes, args.port, args.redirect_uri, cid, csec, turi
        )
        if flavor in ("sdk", "custom") and (args.redirect_uri or flavor == "sdk"
                                            or flow.redirect_uri == REMOTE_REDIRECT_URI):
            extra = {"token_usage": "remote"} if flavor == "sdk" else {}
            if args.include_granted_scopes:
                extra["include_granted_scopes"] = "true"
            auth_url, state = flow.authorization_url(prompt="consent", access_type="offline",
                                                     **extra)
            print("\n1) Open this URL in any browser (it only needs to hand back a code):\n")
            print("  " + auth_url + "\n")
            print("2) Approve, then copy the authorization code Google displays.\n")
            code = input("Paste the code (empty = open the browser for you): ").strip()
            if not code:
                webbrowser_open(auth_url)
                code = input("Paste the code: ").strip()
            flow.fetch_token(code=code)
        else:
            # loopback redirect: the code comes straight back, no copy & paste needed
            args.port = _pick_port(args.port)
            flow, client_id, client_secret, token_uri = _flow_for(
                flavor, scopes, args.port, args.redirect_uri, cid, csec, turi
            )
            auth_url, state = flow.authorization_url(
                prompt="consent", access_type="offline",
                **({"include_granted_scopes": "true"} if args.include_granted_scopes else {}))
            if args.url_only:
                print(auth_url)
                save_pending({"flavor": flavor, "scopes": scopes, "state": state,
                              "redirect_uri": flow.redirect_uri, "port": args.port,
                              "client_id": client_id, "client_secret": client_secret,
                              "token_uri": token_uri,
                              "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                return
            code = _wait_for_code_on_loopback(
                flow, args.port, auth_url,
                timeout=args.consent_timeout, open_browser=not args.no_browser)
            flow.fetch_token(code=code)
        creds = flow.credentials

    data = {
        "flavor": flavor,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "scopes": list(creds.scopes or scopes),
        "token_uri": creds.token_uri or token_uri or DFS_TOKEN_URI,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        info = _userinfo(creds.token)
        data["user_id"] = info.get("sub")          # == the GaiaId DriveFS wants as auth/user-id
        data["email"] = info.get("email")
        print(f"\nuser id : {data['user_id']}\nemail   : {data['email']}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nwarning: could not read userinfo ({exc}); "
              "need the account's GaiaId for DriveFS")
    # explicit overrides / fallbacks (the GaiaId is what DriveFS calls auth/user-id)
    if args.user_id or os.environ.get("COLAB_DRIVE_USER_ID"):
        data["user_id"] = args.user_id or os.environ.get("COLAB_DRIVE_USER_ID")
    if args.email or os.environ.get("COLAB_DRIVE_EMAIL"):
        data["email"] = args.email or os.environ.get("COLAB_DRIVE_EMAIL")
    if not data.get("user_id"):
        print("  set it with --user-id <GaiaId> (or env COLAB_DRIVE_USER_ID)")
    save(data)
    print(f"saved -> {STORE}\nNow: {_mount_hint()}")


def save_pending(data: dict) -> None:
    os.makedirs(os.path.dirname(PENDING), exist_ok=True)
    tmp = PENDING + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PENDING)


def do_import(args) -> None:
    """Non-interactive install of an already-minted refresh token (CI friendly).

    Needs the refresh token + the client it was minted for; optionally validates it by
    refreshing once and recording user_id/email.
    """
    rt = args.refresh_token or os.environ.get("COLAB_DRIVE_REFRESH_TOKEN")
    cid = args.client_id or os.environ.get("COLAB_DRIVE_CLIENT_ID")
    csec = args.client_secret or os.environ.get("COLAB_DRIVE_CLIENT_SECRET") or ""
    turi = args.token_uri or os.environ.get("COLAB_DRIVE_TOKEN_URI") or DFS_TOKEN_URI
    scopes = (args.scopes or os.environ.get("COLAB_DRIVE_SCOPES")
              or " ".join(DFS_SCOPES)).split()
    if not rt or not cid:
        raise SystemExit(
            "need COLAB_DRIVE_REFRESH_TOKEN + COLAB_DRIVE_CLIENT_ID "
            "(or --refresh-token/--client-id)"
        )
    creds = Credentials(token=None, refresh_token=rt, client_id=cid, client_secret=csec or None,
                       token_uri=turi, scopes=scopes)
    creds.refresh(Request())  # fail fast on a bad/expired grant
    data = {"flavor": args.flavor, "client_id": cid, "client_secret": csec,
            "refresh_token": rt, "scopes": list(creds.scopes or scopes), "token_uri": turi,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        info = _userinfo(creds.token)
        data["user_id"], data["email"] = info.get("sub"), info.get("email")
        print(f"refresh token OK\nuser id: {data['user_id']}\nemail  : {data['email']}")
    except Exception as exc:  # noqa: BLE001
        print(f"refresh token OK but userinfo failed ({exc}); "
              "set COLAB_DRIVE_USER_ID or pass --user-id")
    save(data)
    print(f"saved -> {STORE}")


def _pick_port(pref: int) -> int:
    """Return a usable localhost port (Google loopback matching ignores the port number)."""
    import socket

    for p in ([pref] if pref else []) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("localhost", p))
                return s.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("no free localhost port for the loopback redirect")


def _wait_for_code_on_loopback(flow, port: int, url: str, timeout: int = 300, open_browser: bool = True) -> str:
    """Print/launch the consent URL, then catch ?code= on http://localhost:<port>."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    box: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if "code" in q:
                box["code"] = q["code"][0]
                body = b"<h3>Authorized.</h3>You can close this tab and return to the terminal."
            else:
                box["error"] = q.get("error", ["unknown"])[0]
                self.send_response(400); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("localhost", port), H)
    srv.timeout = 2
    deadline = time.time() + timeout
    print(f"\nConsent URL (already opened in your browser; loopback http://localhost:{port}):\n\n"
          f"  {url}\n", flush=True)
    if open_browser:
        webbrowser_open(url)
    while time.time() < deadline and not (box.get("code") or box.get("error")):
        srv.handle_request()
    srv.server_close()
    if box.get("error"):
        raise SystemExit(f"consent failed: {box['error']}")
    if not box.get("code"):
        raise SystemExit("consent timed out: no code received on the loopback port")
    return box["code"]


def webbrowser_open(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def current_access_token(data: dict) -> tuple[str, int]:
    creds = Credentials(
        token=None,
        refresh_token=data["refresh_token"],
        client_id=data["client_id"],
        client_secret=data.get("client_secret") or None,
        token_uri=data.get("token_uri", DFS_TOKEN_URI),
        scopes=data.get("scopes"),
    )
    creds.refresh(Request())
    return creds.token, int(getattr(creds, "expiry", 0) and
                            (creds.expiry.timestamp() - time.time()) or 3599)


def do_token(args) -> None:
    data = load()
    if not data.get("refresh_token"):
        raise SystemExit(f"no refresh token in {STORE}; run `consent` first")
    tok, exp = current_access_token(data)
    if args.whoami:
        info = _userinfo(tok)
        data["user_id"], data["email"] = info.get("sub"), info.get("email")
        save(data)
    out = {
        "access_token": tok,
        "expires_in": max(60, exp),
        "token_type": "Bearer",
        "scope": " ".join(data.get("scopes", [])),
        "user_id": data.get("user_id"),
        "email": data.get("email"),
    }
    print(json.dumps(out, indent=2) if args.json is None else json.dumps(out))


def do_show(_args) -> None:
    data = load()
    if not data:
        print(f"{STORE}: empty (run `consent`)")
        return
    red = {k: ("<set>" if k in ("refresh_token", "client_secret") and v else v)
           for k, v in data.items()}
    print(json.dumps(red, indent=2))


def do_clear(_args) -> None:
    if os.path.exists(STORE):
        os.remove(STORE)
        print(f"removed {STORE} (remember to revoke the grant on "
              "https://myaccount.google.com/permissions)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("consent", help="one-time browser consent for the DriveFS scopes")
    c.add_argument("--flavor", choices=["sdk", "dfs", "custom"], default="sdk",
                   help="sdk = colab-cli cloud client + copy-paste code (default, works headless);"
                        " dfs = client baked into /opt/google/drive/drive (needs localhost);"
                        " custom = your own OAuth client (see --client-id)")
    c.add_argument("--client-id", help="your own OAuth client id (implies --flavor custom)")
    c.add_argument("--client-secret", help="your own OAuth client secret")
    c.add_argument("--token-uri", help="token endpoint (default https://oauth2.googleapis.com/token)")
    c.add_argument("--scopes", help="override the scope list (space separated)")
    c.add_argument("--minimal", action="store_true",
                   help="only email + drive (smaller consent dialog; DriveFS may complain about "
                        "experimentsandconfigs/client_channel)")
    c.add_argument("--redirect-uri", help="override redirect (sdk flavor)")
    c.add_argument("--port", type=int, default=0,
                   help="localhost port for the loopback redirect (0 = auto-pick a free one)")
    c.add_argument("--no-browser", action="store_true", help="do not auto-open the browser")
    c.add_argument("--include-granted-scopes", action="store_true",
                   help="ask Google to re-attach previously granted scopes. Off by default: "
                        "for the DriveFS client it drags in https://www.googleapis.com/auth/"
                        "flexible-api, which Google cannot render -> 400 invalid_scope")
    c.add_argument("--user-id", help="account GaiaId, if userinfo cannot supply it")
    c.add_argument("--email", help="account email, if userinfo cannot supply it")
    c.add_argument("--consent-timeout", type=int, default=300,
                   help="seconds to wait for the loopback redirect")
    c.add_argument("--url-only", action="store_true",
                   help="print the consent URL and remember the state; finish later with --code")
    c.add_argument("--code", help="finish a --url-only consent with this pasted authorization code")
    c.set_defaults(func=do_consent)

    t = sub.add_parser("token", help="print a fresh access token as JSON")
    t.add_argument("--whoami", action="store_true", help="also refresh/record user_id + email")
    t.add_argument("--json", nargs="?", const="", default=None, help=argparse.SUPPRESS)
    t.set_defaults(func=do_token)

    i = sub.add_parser("import", help="store an existing refresh token (no browser at all)")
    i.add_argument("--refresh-token", help="or env COLAB_DRIVE_REFRESH_TOKEN")
    i.add_argument("--client-id", help="or env COLAB_DRIVE_CLIENT_ID")
    i.add_argument("--client-secret", help="or env COLAB_DRIVE_CLIENT_SECRET")
    i.add_argument("--token-uri", help="or env COLAB_DRIVE_TOKEN_URI")
    i.add_argument("--scopes", help="or env COLAB_DRIVE_SCOPES")
    i.add_argument("--flavor", default="imported", help="free-form label for `show`")
    i.set_defaults(func=do_import)

    sub.add_parser("show", help="show the stored credential (secrets redacted)").set_defaults(
        func=do_show)
    sub.add_parser("clear", help="delete the stored credential").set_defaults(func=do_clear)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
