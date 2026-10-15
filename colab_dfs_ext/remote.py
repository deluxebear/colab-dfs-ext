"""Run python inside a Colab runtime through colab_cli's own API.

Primary path: `colab_cli.runtime.ColabRuntime.execute_code()` (no subprocess, no temp file).
Fallback path: the `colab exec` CLI, for the case where the extension is used from a plain
script instead of inside the CLI process.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text or "")


def resolve_session(name: str | None = None) -> str:
    try:
        from colab_cli.common import state

        return state.resolve_session(name)
    except Exception:
        return name or "default"


def _outputs_to_text(outputs: list) -> str:
    chunks: list[str] = []
    for o in outputs or []:
        kind = o.get("output_type")
        if kind == "stream":
            chunks.append(o.get("text", ""))
        elif kind in ("execute_result", "display_data"):
            chunks.append(str(o.get("data", {}).get("text/plain", "")))
        elif kind == "error":
            chunks.append("\n".join(o.get("traceback", [])))
    return strip_ansi("\n".join(chunks))


# Render a remote snippet for a given mountpoint. Placeholder-based (not %-formatting)
# because the snippets themselves contain '%' shell/Python formatting.
def render(snippet: str, mountpoint: str) -> str:
    return snippet.replace("__MP__", repr(mountpoint))


def exec_code(name: str | None, code: str, timeout: int = 180) -> str:
    """Execute `code` in the session's kernel and return its stdout/stderr text."""
    try:
        from colab_cli.common import state
        from colab_cli.runtime import ColabRuntime
    except ImportError:
        return _exec_cli(name, code, timeout)
    session = state.resolve_session(name)
    st = state.get_session(session)
    runtime = ColabRuntime(st.url, st.token, session_name=session, history=state.history)
    try:
        return _outputs_to_text(runtime.execute_code(code, timeout=timeout))
    finally:
        # Official commands do this in a finally too; without it the kernel client's
        # (non-daemon) threads keep the colab process alive after the command finished.
        try:
            runtime.stop()
        except Exception:
            pass


def _exec_cli(name: str | None, code: str, timeout: int) -> str:
    """Fallback: `colab exec -s <name> -f <tmpfile>`."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        cmd = [os.environ.get("COLAB_BIN", "colab"), "exec", "--timeout", str(timeout)]
        if name:
            cmd += ["-s", name]
        cmd += ["-f", path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return strip_ansi((proc.stdout or "") + (proc.stderr or ""))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- small remote snippets used by `colab dfs status` / `colab dfs umount` -----------------
STATUS_REMOTE = r'''
import os, subprocess, json
MP = __MP__
def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    return ((r.stdout or "") + (r.stderr or "")).strip()
print("DFS_RESULT " + json.dumps({
  "mountpoint": MP,
  "mounted": os.path.ismount(MP),
  "mount_line": [l for l in open("/proc/mounts") if "fuse" in l],
  "drive_procs": sh("pgrep -c -x drive || echo 0"),
  "shim_procs": sh("pgrep -c -f [d]fs_creds_shim.py || echo 0"),
  "shim_log": sh("tail -3 /tmp/dfs_creds_shim.log 2>/dev/null | cut -c1-90"),
  "entries": (len(os.listdir(os.path.join(MP, "My Drive")))
              if os.path.isdir(os.path.join(MP, "My Drive")) else None),
  "ephem_creds_addr": os.environ.get("TBE_EPHEM_CREDS_ADDR"),
  "official_env": {k: v for k, v in os.environ.items() if k in
                   ("TBE_CREDS_ADDR", "TBE_EPHEM_CREDS_ADDR", "USE_AUTH_EPHEM")},
}))
'''

UMOUNT_REMOTE = r'''
import os, subprocess, time, json
MP = __MP__
def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    return ((r.stdout or "") + (r.stderr or "")).strip()
for cmd in ("fusermount -u", "fusermount3 -u", "umount -i -f"):
    sh("%s %s" % (cmd, MP))
    if not os.path.ismount(MP):
        break
try:
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        cl = " ".join(p.info["cmdline"] or [])
        if p.info["name"] == "drive" or "dfs_creds_shim" in cl:
            try:
                p.kill()
            except Exception:
                pass
except ImportError:
    sh("pkill -9 -x drive; pkill -9 -f '[d]fs_creds_shim.py'")
time.sleep(1)
sh("rm -rf %s" % MP)
print("DFS_RESULT " + json.dumps({"mountpoint": MP, "mounted": os.path.ismount(MP),
                                  "gone": not os.path.exists(MP)}))
'''
