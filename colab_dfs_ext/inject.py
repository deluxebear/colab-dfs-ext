"""Injection manager for the `colab dfs` extension.

Stdlib only, and importable from *any* interpreter (inside or outside the colab-cli
venv). Nothing here edits a file that belongs to google-colab-cli: we drop exactly one
file -- `colab_dfs.pth` -- into the site-packages of whichever environment provides the
`colab` command. The .pth puts ~/.colab-dfs-ext on sys.path and imports the loader, which
calls `colab_dfs_ext.register(app)` on the already-imported typer app.

Why a .pth: `uv tool install -U google-colab-cli` (== `colab update --install`) rebuilds
the tool environment, so anything *installed into* that venv can disappear. The .pth can
disappear too, but the extension source in ~/.colab-dfs-ext never does, so restoring after
an upgrade is one command:  python3 ~/.colab-dfs-ext/install.py
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys

PACKAGE = "colab_dfs_ext"
LOADER = "colab_dfs_loader.py"
PTH_NAME = "colab_dfs.pth"
HOME_DIR = os.path.expanduser("~/.colab-dfs-ext")
TOOL_NAME = "google-colab-cli"

# site.py executes every .pth line that starts with 'import' -- but strictly as ONE line.
# We keep the hook readable as a normal multi-line snippet and flatten it mechanically
# (escape backslashes, quotes, newlines) at write time.
_SNIPPET = """
try:
    here = os.path.expanduser('~/.colab-dfs-ext')
    if os.path.isdir(here) and here not in sys.path:
        sys.path.insert(0, here)
    import colab_dfs_loader
except Exception as _exc:  # never break the colab CLI over an extension
    if os.environ.get('COLAB_DFS_DEBUG'):
        sys.stderr.write('[colab-dfs] loader skipped: ' + repr(_exc))
"""


def comment_line() -> str:
    return "# colab dfs extension (%s): browser-free DriveFS mounting for %s" % (
        _version(), TOOL_NAME)


def _version() -> str:
    try:
        import colab_dfs_ext as _m

        return getattr(_m, "__version__", "?")
    except Exception:
        return "unknown"


def pth_contents() -> str:
    line = (_SNIPPET.strip("\n").replace("\\", "\\\\")
            .replace('"', '\\"').replace("\n", "\\n"))
    return ("# %s\n"
            "# Managed file: regenerate with `colab dfs install` or `python3 %s/install.py`\n"
            "import os, sys; exec(compile(\"%s\", '%s', 'exec'))"
            % (comment_line().lstrip("# "), HOME_DIR, line, PTH_NAME))


# --- where to inject -----------------------------------------------------------------------
def _site_packages_of(interpreter: str) -> str | None:
    """site-packages of a given python interpreter (or None)."""
    try:
        out = subprocess.run(
            [interpreter, "-c", "import site,sys;print(site.getsitepackages()[0])"],
            capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        return out[-1] if out else None
    except Exception:
        return None


def _which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _interpreters_to_try() -> list[str]:
    """Candidate interpreters of the environment that owns the `colab` command."""
    out: list[str] = []
    shim = _which("colab")
    if shim:
        try:
            first = open(shim, "rb").read(512).split(b"\n", 1)[0].decode("utf-8", "replace")
            if first.startswith("#!"):
                shebang = first[2:].strip()
                # `#!/usr/bin/env python3` is useless here; uv writes absolute paths
                if " " not in shebang or shebang.startswith("/"):
                    out.append(shebang.split()[0])
        except Exception:
            pass
    here = os.path.realpath(sys.executable)
    if here not in [os.path.realpath(p) for p in out]:
        out.append(here)
    return out


def candidate_site_dirs() -> list[str]:
    """Every plausible site-packages dir, best first. Handles uv tools, pipx and plain pip."""
    dirs: list[str] = []

    def add(d):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)

    for py in _interpreters_to_try():
        add(_site_packages_of(py))
    home = os.path.expanduser("~")
    for pat in (
        home + "/.local/share/uv/tools/" + TOOL_NAME + "/lib/python*/site-packages",
        home + "/.local/pipx/venvs/" + TOOL_NAME + "/lib/python*/site-packages",
        "/usr/local/lib/python3*/site-packages",
        "/usr/lib/python3*/dist-packages",
    ):
        for d in sorted(glob.glob(pat), reverse=True):
            add(d)
    for d in getattr(sys, "getsitepackages", lambda: [])():
        add(d)
    return dirs


def target_site_dir() -> str | None:
    """The site-packages that actually contains colab_cli -- i.e. the live CLI environment."""
    cands = candidate_site_dirs()
    for d in cands:
        if os.path.isdir(os.path.join(d, "colab_cli")):
            return d
    return cands[0] if cands else None


# --- where the source lives ---------------------------------------------------------------
def _package_dir() -> str | None:
    # Priority matters: when running from a repo checkout we must prefer the repo copy,
    # otherwise the (stale) installed copy in HOME_DIR would shadow every fix and
    # `install.py` would silently stop propagating updates (source == HOME_DIR -> no copy).
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.environ.get("COLAB_DFS_EXT"), here, HOME_DIR):
        if base:
            d = os.path.abspath(os.path.expanduser(base))
            if os.path.isdir(os.path.join(d, PACKAGE)):
                return d
            if os.path.basename(d) == PACKAGE and os.path.isdir(d):
                return os.path.dirname(d)
    return None


def source_dir() -> str | None:
    """A directory that contains colab_dfs_ext/ + colab_dfs_loader.py."""
    pkg = _package_dir()
    if pkg and os.path.isfile(os.path.join(pkg, LOADER)):
        return pkg
    # running from a git checkout: .../extension/colab_dfs_ext/inject.py
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.dirname(here), here):
        if os.path.isdir(os.path.join(base, PACKAGE)) and os.path.isfile(os.path.join(base, LOADER)):
            return base
    return pkg


def install_root() -> str:
    return HOME_DIR


# --- the actual injection -----------------------------------------------------------------
def pth_path(site_dir: str | None = None) -> str | None:
    site_dir = site_dir or target_site_dir()
    return os.path.join(site_dir, PTH_NAME) if site_dir else None


def ensure(site_dir: str | None = None, copy_source: bool = True, quiet: bool = False) -> dict:
    """Make sure the extension is wired into the colab-cli environment. Idempotent."""
    import shutil

    report = {"site_dir": site_dir or target_site_dir(), "source": source_dir(),
              "installed": [], "changed": False}
    if not report["site_dir"]:
        report["error"] = "could not find the site-packages of the colab-cli environment"
        return report
    if (copy_source and report["source"]
            and os.path.realpath(report["source"]) != os.path.realpath(HOME_DIR)):
        dst = os.path.join(HOME_DIR, PACKAGE)
        os.makedirs(HOME_DIR, exist_ok=True)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(os.path.join(report["source"], PACKAGE), dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy(os.path.join(report["source"], LOADER), os.path.join(HOME_DIR, LOADER))
        report["installed"].append(HOME_DIR)
        report["source"] = HOME_DIR
    path = pth_path(report["site_dir"])
    body = pth_contents()
    old = open(path).read() if path and os.path.exists(path) else None
    if path and old != body:
        open(path, "w").write(body)
        report["changed"] = True
        report["installed"].append(path)
    report["pth"] = path
    if not quiet:
        for p in report["installed"]:
            print("[colab-dfs] wrote %s" % p)
    return report


def remove(site_dir: str | None = None) -> dict:
    path = pth_path(site_dir)
    out = {"pth": path, "removed": False}
    if path and os.path.exists(path):
        os.unlink(path)
        out["removed"] = True
    return out


def status(site_dir: str | None = None) -> dict:
    site_dir = site_dir or target_site_dir()
    path = pth_path(site_dir)
    return {
        "extension_version": _version(),
        "source": source_dir(),
        "install_root": HOME_DIR if os.path.isdir(HOME_DIR) else None,
        "site_packages": site_dir,
        "colab_cli_here": bool(site_dir and os.path.isdir(os.path.join(site_dir, "colab_cli"))),
        "pth": path,
        "pth_present": bool(path and os.path.exists(path)),
        "colab_shim": _which("colab"),
        "python": sys.executable,
    }


def reload_ok() -> bool:
    """True when the commands are actually registered in this process."""
    try:
        import colab_cli.cli as cli

        return bool(getattr(cli, "_colab_dfs_registered", False))
    except Exception:
        return False
