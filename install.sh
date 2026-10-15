#!/bin/sh
# Bootstrap installer for colab-dfs-ext (https://github.com/deluxebear/colab-dfs-ext).
#
# One-line install (any POSIX shell, no git required):
#
#     curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
#
# or with wget:
#
#     wget -qO- https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
#
# Env overrides:
#     COLAB_DFS_REPO=owner/repo     source repository     (default: deluxebear/colab-dfs-ext)
#     COLAB_DFS_REF=main            branch or tag         (default: main)
#
# What it does: fetch the repo tarball, run `python3 install.py` from it. That copies the
# extension to ~/.colab-dfs-ext and drops a single .pth file into the colab-cli
# environment's site-packages. Nothing else on your machine is touched.
set -eu

REPO="${COLAB_DFS_REPO:-deluxebear/colab-dfs-ext}"
REF="${COLAB_DFS_REF:-main}"
TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/${REF}.tar.gz"

say() { printf '[colab-dfs] %s\n' "$*"; }
die() { printf '[colab-dfs] ERROR: %s\n' "$*" >&2; exit 1; }

# --- pick a downloader ----------------------------------------------------------------------
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        die "need curl or wget to download the source"
    fi
}

# --- pick a python --------------------------------------------------------------------------
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
[ -n "$PY" ] || die "python3 not found on PATH (https://www.python.org/)"

# --- sanity: colab should exist --------------------------------------------------------------
if ! command -v colab >/dev/null 2>&1; then
    say "WARN: 'colab' (google-colab-cli) not found on PATH."
    say "      install it first:  uv tool install google-colab-cli"
    say "      continuing anyway -- the .pth is written, it activates once colab exists."
fi

# --- download + extract ----------------------------------------------------------------------
command -v tar >/dev/null 2>&1 || die "tar not found on PATH"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/colab-dfs-ext.XXXXXX")" || die "cannot create temp dir"
trap 'rm -rf "$TMP"' EXIT INT TERM

say "downloading ${REPO}@${REF} ..."
fetch "$TARBALL_URL" | tar -xz -C "$TMP" || die "download/extract failed ($TARBALL_URL)"

SRC_DIR=""
for d in "$TMP"/*/; do
    [ -f "${d}install.py" ] && SRC_DIR="$d" && break
done
[ -n "$SRC_DIR" ] || die "install.py not found in the tarball"

# --- install ----------------------------------------------------------------------------------
cd "$SRC_DIR"
"$PY" install.py || die "install.py failed (run it again with COLAB_DFS_DEBUG=1 for details)"

# --- next steps --------------------------------------------------------------------------------
cat <<'EOF'

[colab-dfs] installed. next steps:
  1. colab dfs doctor     # verify the wiring
  2. colab dfs consent    # one-time Google consent (browser opens once)
  3. colab dfs mount      # browser-free mount, e.g.:  colab dfs mount -s <session>
[colab-dfs] docs: https://github.com/deluxebear/colab-dfs-ext#readme
EOF
