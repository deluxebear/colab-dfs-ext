#!/usr/bin/env python3
"""Mount Google Drive inside a Colab runtime WITHOUT any browser.

Reverse-engineered replacement for `colab drivemount -s <session>`: instead of asking Colab
to propagate "dfs_ephemeral" credentials (which requires an accounts.google.com consent in a
browser), we feed DriveFS our own credentials through a local GCE-metadata-compatible shim
and launch /opt/google/drive/drive ourselves.  See ../docs/REVERSE.md.

    python3 drive_mount.py -s test                       # shim mode, /content/drive
    python3 drive_mount.py -s test --path /content/drive --force
    python3 drive_mount.py -s test --mode rt             # DriveFS refreshes its own token
    python3 drive_mount.py -s test --cli-token           # experiment with colab-cli's token
    python3 drive_mount.py --dry-run                     # print the remote payload only

Prereq: `dfs_creds.py consent` once, plus a running session (`colab new`).
"""
from __future__ import annotations  # keeps `str | None` annotations importable on py<3.10


import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

STORE = os.path.expanduser("~/.config/colab-cli/dfs_credentials.json")
CLI_TOKEN = os.path.expanduser("~/.config/colab-cli/token.json")

# Copied from google.colab.drive._mount() in the runtime image (128.0.0.0), so the resulting
# mount is behaviourally identical to drive.mount(): same feature flags, same preferences.
DRIVE_FEATURES = (
    "crash_throttle_percentage:100,fuse_max_background:1000,max_read_qps:1000,max_write_qps:1000,"
    "max_operation_batch_size:15,max_parallel_push_task_instances:10,opendir_timeout_ms:{timeout_ms},"
    "virtual_folders_omit_spaces:true,read_only_mode:{readonly}"
)
DRIVE_PREFS = (
    "trusted_root_certs_file_path:{drive_dir}/roots.pem,"
    "feature_flag_restart_seconds:129600,mount_point_path:{mountpoint}"
)

REMOTE = r'''
"""Payload executed inside the Colab kernel by drive_mount.py (stdlib only)."""
import base64, http.server, json, os, re, subprocess, sys, threading, time
import urllib.parse, urllib.request

CFG = json.loads(base64.b64decode(os.environ.get("DFS_CFG") or "<<DFS_CFG_B64>>").decode())
MP = CFG["mountpoint"]
PORT = int(CFG["port"])
TIMEOUT = int(CFG["timeout"])
MODE = CFG["mode"]
DRIVE_DIR = "/opt/google/drive"
DRIVE = os.path.join(DRIVE_DIR, "drive")
SHIM_CFG = "/tmp/dfs_creds.json"
SHIM_SRC = "/tmp/dfs_creds_shim.py"
SHIM_LOG = "/tmp/dfs_creds_shim.log"
DFS_LOG = "/root/.config/Google/DriveFS/Logs/drive_fs.txt"


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def result(ok, **extra):
    print("DFS_RESULT " + json.dumps(dict(ok=ok, **extra)))
    sys.stdout.flush()
    raise SystemExit(0 if ok else 1)


def mounted():
    return os.path.isdir(os.path.join(MP, "My Drive"))


def in_mounts():
    try:
        with open("/proc/mounts") as f:
            return any(parts[1] == MP for parts in (line.split() for line in f) if len(parts) > 1)
    except OSError:
        return False


if mounted() and not CFG["force"]:
    result(True, note="already mounted at %s (use --force to remount)" % MP)

# --- mimic _mount()'s teardown so a stale DriveFS does not poison the new attempt ---------
# --- kill leftovers. NOTE: `pkill -f <pattern>` self-matches the `sh -c` command line, which
# --- used to kill our own shell and leave the previous shim holding the port (stale token!).
try:
    import psutil
    for _p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if _p.info["name"] == "drive" or "dfs_creds_shim" in " ".join(_p.info["cmdline"] or []):
                _p.kill()
        except Exception:
            pass
except ImportError:
    sh('pkill -9 -x drive; pkill -9 -f "[d]fs_creds_shim.py"')
sh('sleep 1')
sh('umount -f "%s" 2>/dev/null; umount "%s" 2>/dev/null' % (MP, MP))
if in_mounts():
    # never rm -rf through a live FUSE mount: that would delete real Drive files
    result(False, stage="teardown", error="mountpoint still mounted: " + MP,
           hint="unmount: colab dfs umount -s <session>  (or: colab exec -s <session> -c 'umount -f %s')" % MP)
if os.path.isdir(MP):
    sh('rm -rf "%s"' % MP)
os.makedirs(MP, exist_ok=True)

if CFG.get("purge_state"):
    # a sqlite/metadata store left behind by a failed run can masquerade as AUTH_ERROR
    for d in ("/root/.config/Google/DriveFS",
              os.path.join(os.environ.get("HOME", "/root"), ".config/Google/DriveFS")):
        sh('rm -rf "%s"' % d)
    print('purged DriveFS local state')

# --- credentials ------------------------------------------------------------------------
SHIM = r"""
import base64, http.server, json, os, threading, time, urllib.parse, urllib.request
CFG = json.load(open("/tmp/dfs_creds.json"))
LOCK = threading.Lock()
CACHE = {"tok": CFG.get("access_token"), "until": CFG.get("expires_at", 0)}

def mint():
    now = time.time()
    with LOCK:
        if CACHE["tok"] and CACHE["until"] - 120 > now:
            return CACHE["tok"]
        payload = {"grant_type": "refresh_token", "refresh_token": CFG["refresh_token"],
                   "client_id": CFG["client_id"]}
        if CFG.get("client_secret"):
            payload["client_secret"] = CFG["client_secret"]
        req = urllib.request.Request(
            CFG.get("token_uri", "https://oauth2.googleapis.com/token"),
            urllib.parse.urlencode(payload).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
        CACHE["tok"] = data["access_token"]
        CACHE["until"] = now + int(data.get("expires_in", 3599))
        return CACHE["tok"]

class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/plain"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Metadata-Flavor", "Google")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        try:
            if p.endswith("/instance/service-accounts/default/token"):
                self._send(json.dumps({
                    "access_token": mint(),
                    "expires_in": max(60, int(CACHE["until"] - time.time())),
                    "token_type": "Bearer",
                    "scope": " ".join(CFG.get("scopes", [])),
                }), "application/json")
            elif p.endswith("/guest-attributes/auth/user-id"):
                self._send(CFG["user_id"])
            elif p.endswith("/email"):
                self._send(CFG.get("email", ""))
            elif p.endswith("/guest-attributes/auth/scopes"):
                self._send(" ".join(CFG.get("scopes", [])))
            else:
                self.send_response(404); self.send_header("Content-Length", "0")
                self.end_headers()
        except Exception as exc:
            self.send_response(500); self.end_headers()
            self.wfile.write(str(exc).encode())
        with open("/tmp/dfs_creds_shim.log", "a") as f:
            f.write(time.strftime("%H:%M:%S ") + p + "\n")

    def log_message(self, *a):
        pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("SHIM_PORT", "8012"))), H)
srv.serve_forever()
"""

def port_free(p):
    import socket
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


if MODE == "shim":
    base = PORT
    PORT = next((p for p in range(base, base + 30) if port_free(p)), None)
    if PORT is None:
        result(False, stage="port",
               error="no free port in %d..%d" % (base, base + 29))
    json.dump(CFG, open(SHIM_CFG, "w"))
    os.chmod(SHIM_CFG, 0o600)
    open(SHIM_SRC, "w").write(SHIM)
    env = dict(os.environ, SHIM_PORT=str(PORT))
    subprocess.Popen([sys.executable, SHIM_SRC], stdout=open(SHIM_LOG, "w"),
                     stderr=subprocess.STDOUT, start_new_session=True, env=env)
    probe = "curl -sS -m 3 -H 'Metadata-Flavor: Google' http://127.0.0.1:%d/computeMetadata/v1/instance/service-accounts/default/token" % PORT
    for _ in range(20):
        out = sh(probe)
        if '"access_token"' in out:
            break
        time.sleep(0.5)
    else:
        result(False, stage="shim", error=out[:400], log=sh("tail -c 800 " + SHIM_LOG)[:400])
    # Fail fast if the shim is serving someone else's token (e.g. a stale process on the port).
    try:
        served = json.loads(out)["access_token"]
    except Exception as exc:
        result(False, stage="shim", error="unparsable token json: %s" % exc, log=out[:300])
    ti = sh('curl -sS -m 15 "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=%s"' % served)
    try:
        info = json.loads(ti)
    except Exception:
        info = {}
    if not info.get("azp") and not info.get("aud"):
        result(False, stage="token", error="served token is not valid: %s" % (ti[:300] or "<empty>"),
               token_len=len(served))
    granted = info.get("scope", "")
    print("TOKEN OK azp=%s sub=%s scope=%s" % (info.get("azp") or info.get("aud"),
                                               info.get("sub"), granted[:120]))
    if "drive" not in granted:
        result(False, stage="token",
               error="served token lacks the drive scope; granted=%s" % granted[:300])
    auth_flag = "--metadata_server_auth_uri=127.0.0.1:%d/computeMetadata/v1" % PORT
    extra_flags = []
else:  # "rt": let DriveFS refresh our refresh_token with its baked-in client credentials
    auth_flag = "--refresh_token=%s" % CFG["refresh_token"]
    extra_flags = ["--token_uri=" + CFG.get("token_uri", "https://oauth2.googleapis.com/token"),
                   "--user_id=" + str(CFG["user_id"]),
                   "--user_email=" + str(CFG.get("email", ""))]

features = ("crash_throttle_percentage:100,fuse_max_background:1000,max_read_qps:1000,"
            "max_write_qps:1000,max_operation_batch_size:15,max_parallel_push_task_instances:10,"
            "opendir_timeout_ms:%d,virtual_folders_omit_spaces:true,read_only_mode:%s"
            % (TIMEOUT * 1000, "true" if CFG["readonly"] else "false"))
prefs = ("trusted_root_certs_file_path:%s/roots.pem,feature_flag_restart_seconds:129600,"
         "mount_point_path:%s" % (DRIVE_DIR, MP))

cmd = [DRIVE, "--features=" + features, auth_flag, "--preferences=" + prefs] + extra_flags + CFG.get("drive_extra", []) + [MP]
print("LAUNCH:", " ".join(c if not c.startswith("--refresh_token=") else "--refresh_token=<redacted>"
                          for c in cmd))
sys.stdout.flush()
logf = open("/tmp/dfs_drive.log", "w")
proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
                        env={"HOME": os.environ.get("HOME", "/root"), "FUSE_DEV_NAME": "/dev/fuse",
                             "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:."})

deadline = time.time() + TIMEOUT
err = ""
while time.time() < deadline:
    if mounted():
        break
    if proc.poll() is not None:
        err = sh('grep -hE "PERMISSION_DENIED|insufficient_scope|Scope|scope|CANNOT_|not allowed|'
                 'empty|Failed to" "%s" 2>/dev/null | tail -8' % DFS_LOG)
        break
    time.sleep(1)

ok = mounted()
print("MOUNT TABLE:", sh("grep fuse /proc/mounts | head -3").strip())
print("LISTING:", sh('ls "%s" 2>&1 | head -5' % MP).strip().replace("\n", " | "))
if ok:
    n = sh('ls "%s/My Drive" 2>/dev/null | wc -l' % MP).strip()
    result(True, mountpoint=MP, entries=n, mode=MODE,
           shim=("http://127.0.0.1:%d" % PORT if MODE == "shim" else None))
result(False, stage="drive", rc=proc.poll(), mode=MODE, drive_log=sh("tail -c 1200 /tmp/dfs_drive.log")[:1200],
       dfs_log_errors=err[:1500])
'''


def build_cfg(args) -> dict:
    env = os.environ.get
    if args.cli_token:
        if not os.path.exists(CLI_TOKEN):
            sys.exit(f"--cli-token needs {CLI_TOKEN}")
        tok = json.load(open(CLI_TOKEN))
        return {
            "mode": "shim",
            "mountpoint": args.path,
            "port": args.port,
            "force": args.force,
            "timeout": args.timeout,
            "readonly": args.readonly,
            "access_token": None,
            "expires_at": 0,
            "refresh_token": tok.get("refresh_token"),
            "client_id": tok["client_id"],
            "client_secret": tok.get("client_secret"),
            "token_uri": tok.get("token_uri", "https://oauth2.googleapis.com/token"),
            "scopes": tok.get("scopes", []),
            "user_id": args.user_id or os.environ.get("DFS_UID", ""),
            "email": args.email or os.environ.get("DFS_EMAIL", ""),
            "drive_extra": args.drive_extra,
            "purge_state": args.purge_state,
        }
    # --- CI / env-var mode: COLAB_DRIVE_REFRESH_TOKEN (+ client) beats the store file ---
    if env("COLAB_DRIVE_REFRESH_TOKEN") or args.from_env:
        rt = env("COLAB_DRIVE_REFRESH_TOKEN", "")
        cid = env("COLAB_DRIVE_CLIENT_ID", "")
        if not (rt and cid):
            sys.exit("COLAB_DRIVE_REFRESH_TOKEN and COLAB_DRIVE_CLIENT_ID must both be set "
                     "(COLAB_DRIVE_CLIENT_SECRET is required for confidential clients)")
        uid = args.user_id or env("COLAB_DRIVE_USER_ID", "")
        if not uid:
            sys.exit("DriveFS needs the account user id: set COLAB_DRIVE_USER_ID or --user-id "
                     "(it is userinfo 'sub'; consent/import records it for you)")
        return {
            "mode": args.mode,
            "mountpoint": args.path,
            "port": args.port,
            "force": args.force,
            "timeout": args.timeout,
            "readonly": args.readonly,
            "refresh_token": rt,
            "client_id": cid,
            "client_secret": env("COLAB_DRIVE_CLIENT_SECRET", ""),
            "token_uri": env("COLAB_DRIVE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
            "scopes": env("COLAB_DRIVE_SCOPES", "").split(),
            "user_id": uid,
            "email": args.email or env("COLAB_DRIVE_EMAIL", ""),
            "drive_extra": args.drive_extra,
            "purge_state": args.purge_state,
        }
    if not os.path.exists(STORE):
        sys.exit(f"no credentials in {STORE}\nrun:  {_consent_hint()}")
    data = json.load(open(STORE))
    if not data.get("refresh_token"):
        sys.exit(f"incomplete credentials in {STORE}; run `{_consent_hint()}`")
    if not data.get("user_id") and not (args.user_id or env("COLAB_DRIVE_USER_ID")):
        sys.exit("user_id missing (needed by DriveFS): pass --user-id <GaiaId>, set "
                 "COLAB_DRIVE_USER_ID, or run `dfs_creds.py consent` with the openid scope")
    return {
        "mode": args.mode,
        "mountpoint": args.path,
        "port": args.port,
        "force": args.force,
        "timeout": args.timeout,
        "readonly": args.readonly,
        "refresh_token": data["refresh_token"],
        "client_id": data["client_id"],
        "client_secret": data.get("client_secret", ""),
        "token_uri": data.get("token_uri", "https://oauth2.googleapis.com/token"),
        "scopes": data.get("scopes", []),
        "user_id": args.user_id or env("COLAB_DRIVE_USER_ID", "") or data["user_id"],
        "email": args.email or env("COLAB_DRIVE_EMAIL", "") or data.get("email", ""),
        "drive_extra": args.drive_extra,
        "purge_state": args.purge_state,
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="browser-free google.colab.drive.mount")
    ap.add_argument("-s", "--session", help="colab session name (default: the only running one)")
    ap.add_argument("--path", default="/content/drive", help="mountpoint (default /content/drive)")
    ap.add_argument("--mode", choices=["shim", "rt"], default="shim",
                    help="shim = we serve a GCE-metadata shim (works with any client's token);"
                         " rt = pass --refresh_token to DriveFS (needs --flavor dfs creds)")
    ap.add_argument("--force", action="store_true", help="remount even if already mounted")
    ap.add_argument("--readonly", action="store_true", help="read-only mount")
    ap.add_argument("--timeout", type=int, default=120, help="seconds to wait for the mount")
    ap.add_argument("--port", type=int, default=8012, help="metadata shim port inside the VM")
    ap.add_argument("--cli-token", action="store_true",
                    help="experimental: use colab-cli's own token.json (drive.file scope -> 403)")
    ap.add_argument("--from-env", action="store_true",
                    help="force the COLAB_DRIVE_* env-var credentials even if a store file exists")
    ap.add_argument("--drive-extra", action="append", default=[],
                    help="extra flags for /opt/google/drive/drive, e.g. --drive-extra --curl_logging")
    ap.add_argument("--purge-state", action="store_true",
                    help="delete /root/.config/Google/DriveFS (stale sqlite/metadata from failed runs)")
    ap.add_argument("--user-id", help="override DriveFS user id (the account's GaiaId)")
    ap.add_argument("--email", help="override account email")
    ap.add_argument("--colab", default="colab", help="colab executable (default: colab)")
    ap.add_argument("--dry-run", action="store_true", help="print the remote payload + config")
    args = ap.parse_args(argv)

    if " " in args.path:
        sys.exit("mountpoint must not contain a space")
    cfg = build_cfg(args)
    encoded = base64.b64encode(json.dumps(cfg).encode()).decode()

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(REMOTE)
        payload = f.name
    if args.dry_run:
        red = {k: ("<redacted>" if k in ("refresh_token", "access_token", "client_secret") else v)
               for k, v in cfg.items()}
        print(json.dumps(red, indent=2))
        print("\n# remote payload:\n" + REMOTE)
        os.unlink(payload)
        return

    cmd = [args.colab, "exec", "--timeout", str(args.timeout + 60), "--env", f"DFS_CFG={encoded}",
           "-f", payload]
    if args.session:
        cmd += ["-s", args.session]
    print(f"[drive_mount] {args.session or 'default session'} -> {args.path} ({args.mode} mode) ...")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    clean = re_ansi(out)
    for line in clean.splitlines():
        if line.startswith("DFS_RESULT "):
            res = json.loads(line[len("DFS_RESULT "):])
            if res.get("ok"):
                print(f"[drive_mount] OK in {time.time()-t0:.1f}s: Drive at {res.get('mountpoint')}"
                      f" ({res.get('entries')} entries top-level, mode={res.get('mode')})")
                if res.get("shim"):
                    print(f"[drive_mount] creds shim: {res['shim']}"
                          "  (keep the VM alive; it re-serves tokens as they expire)")
                os.unlink(payload)
                return
            print(f"[drive_mount] FAILED: {json.dumps(res, indent=2)}")
            for k in ("drive_log", "dfs_log_errors", "log", "error"):
                if res.get(k):
                    print(f"--- {k} ---\n{res[k]}\n")
            os.unlink(payload)
            sys.exit(1)
    print(clean)
    print("[drive_mount] no DFS_RESULT marker in output; raw exit=%d" % proc.returncode)
    os.unlink(payload)
    sys.exit(1)


def re_ansi(text: str) -> str:
    import re as _re

    return _re.sub(r"\x1b\[[0-9;]*m", "", text)


def _in_cli() -> bool:
    """True when this module runs inside the colab CLI extension (not as a standalone script)."""
    return bool(__package__) and __name__ != "__main__"


def _consent_hint() -> str:
    return "colab dfs consent" if _in_cli() else "python3 dfs_creds.py consent --flavor sdk"


def make_cfg(session=None, path="/content/drive", mode="shim", force=False, readonly=False,
             timeout=120, port=8012, cli_token=False, from_env=False, drive_extra=(),
             purge_state=False, user_id=None, email=None) -> dict:
    """Build the remote config without going through argparse (used by `colab dfs mount`)."""
    ns = argparse.Namespace(session=session, path=path, mode=mode, force=force,
                            readonly=readonly, timeout=timeout, port=port, cli_token=cli_token,
                            from_env=from_env, drive_extra=list(drive_extra),
                            purge_state=purge_state, user_id=user_id, email=email,
                            colab="colab", dry_run=True)
    return build_cfg(ns)


def payload(cfg: dict, source: str | None = None) -> str:
    """The remote python source with `cfg` inlined (so no --env / no temp file is needed)."""
    src = source or REMOTE
    b64 = base64.b64encode(json.dumps(cfg).encode()).decode()
    if "<<DFS_CFG_B64>>" in src:
        return src.replace("<<DFS_CFG_B64>>", b64)
    return "import os\nos.environ['DFS_CFG'] = %r\n" % b64 + src


def parse_result(text: str):
    """Pull the DFS_RESULT json out of kernel output; None when it never showed up."""
    for line in re_ansi(text).splitlines():
        if line.startswith("DFS_RESULT "):
            return json.loads(line[len("DFS_RESULT "):])
    return None


if __name__ == "__main__":
    main()
