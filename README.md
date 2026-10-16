# colab-dfs-ext — a zero-browser Drive mount extension for google-colab-cli

> [中文](README.zh-CN.md) | English

Exposes browser-less Google Drive mounting as a `dfs` subcommand group on the `colab` CLI —
**without touching a single file of google-colab-cli**: colab-cli upgrades are unaffected,
no re-integration is needed, and worst case a single command repairs it. This directory is a
self-contained project that can be copied elsewhere and used standalone (pure Python standard
library, no third-party dependencies).

```
colab dfs mount   [-s SESSION] [path]   # Zero-browser mount (DriveFS + in-runtime credentials shim)
colab dfs umount  [-s SESSION] [path]
colab dfs status  [-s SESSION] [path]
colab dfs consent [--flavor dfs] [--minimal] ...   # One-time Google consent -> refresh token
colab dfs show | token
colab dfs doctor                        # Health check: injection / credentials / session, with fix hints
colab dfs install                       # Re-inject this extension (run if `dfs` disappears after an upgrade)
```

Every subcommand accepts `-- -<any args>` to pass extra flags through to the underlying tools —
flags not exposed by this extension remain usable.

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-dfs-ext/main/install.sh | sh
```

(wget works too: `wget -qO- ... | sh`. The script does only three things: download the repo
tarball → extract to a temp dir → run `python3 install.py`, then print the next steps.
Use `COLAB_DFS_REPO` / `COLAB_DFS_REF` to override the source.)

## Requirements

- Python 3.8+ (both `install.py` and the extension are pure standard library)
- [`google-colab-cli`](https://pypi.org/project/google-colab-cli/) installed and working
  (`uv tool install google-colab-cli`), i.e. the `colab` command
- A connected colab session (`colab connect -s <session>`)
- Colab runtimes ship `/opt/google/drive` (DriveFS) already — no upload needed

## Usage

### Step 1 · Install the extension

One-line install (above), or manually:

```bash
git clone https://github.com/deluxebear/colab-dfs-ext
cd colab-dfs-ext            # this directory
python3 install.py          # any python3 works
colab dfs doctor            # verify the injection succeeded
```

`install.py` does two things:

1. Copies the extension source to `~/.colab-dfs-ext/` (**outside** colab-cli's venv — upgrades
   never touch it);
2. Writes **a single file**, `colab_dfs.pth` (~600 bytes), into the colab-cli environment's
   site-packages.

Useful install flags:

```bash
python3 install.py --status        # where the .pth / source / colab_cli live
python3 install.py --link          # dev mode: symlink this repo; code changes take effect immediately
python3 install.py --site-dir DIR  # target a specific site-packages
```

### Step 2 · One-time authorization (only once, ever)

```bash
colab dfs consent            # browser pops the Google consent page -> allow
colab dfs show               # inspect stored credentials (refresh token / GaiaId / email)
```

By default this uses the reverse-engineered DriveFS built-in client (flavor `dfs` — Google's own
"Google Drive for desktop", a verified app); the localhost callback is caught automatically, so
there is no authorization code to copy-paste. Other flavors: `--flavor sdk` (colab-cli's bundled
client, works headless but easily blocked by Google) and `--flavor custom --client-id ...
--client-secret ...` (your own GCP project — most robust long-term). On machines that have
already consented once, `colab dfs consent --import` works with no browser at all.

Credentials live in `~/.config/colab-cli/dfs_credentials.json`; every mount afterwards is
zero-browser.

### Step 3 · Everyday mounting

```bash
colab connect -s test        # connect to the colab session (if not already)
colab dfs mount -s test      # mounts at /content/drive, done in seconds
colab dfs status -s test     # mount status
colab dfs umount -s test     # unmount

colab dfs mount -s test --force --purge-state   # remount: clear stale processes/dirs/local state
colab dfs mount -s test --dry-run               # only print config and the remote payload
colab dfs mount -s test --readonly              # read-only mount
```

### Optional · Make the official `drivemount` zero-browser too

```bash
export COLAB_DFS_DEFAULT=1     # put this in your shell profile
colab drivemount -s test       # routed through the dfs extension; falls back to the official
                               # browser flow automatically if credentials are missing
```

## Coexisting with upgrades (verified in practice)

| Scenario | Result |
|---|---|
| `uv tool install google-colab-cli --reinstall` (rebuild the tool env) | The `.pth` **survives** (uv only reinstalls its own packages, doesn't clean foreign files); `colab dfs` works right away |
| `colab update --install` | Internally runs `uv tool install -U`; the extension wraps the `update` command at registration time and re-checks/re-wires itself once the upgrade finishes (`[colab-dfs] extension still wired: ...`) |
| Worst case (Python version change, venv manually wiped) | `colab dfs` disappears, but `~/.colab-dfs-ext` is still there: `python3 ~/.colab-dfs-ext/install.py` restores it in one command |

## Uninstall

```bash
python3 install.py --uninstall          # remove the .pth
rm -rf ~/.colab-dfs-ext                 # (optional) delete the extension source copy
```

## Troubleshooting

```bash
colab dfs doctor                # health check: injection / credentials / session, with fix hints
COLAB_DFS_DEBUG=1 colab dfs …   # verbose diagnostics from the loader/injection
```

- `colab dfs` commands gone → re-wire with `python3 ~/.colab-dfs-ext/install.py`.
- Changed this repo's code but behavior didn't change → `~/.colab-dfs-ext` holds the snapshot
  from install time; re-run `python3 install.py` (or switch to `--link`). The `source_dir()`
  lookup order is: `COLAB_DFS_EXT` env var → current code location → `~/.colab-dfs-ext`.

## How the injection works (why upgrades don't break it)

- colab-cli is a Typer app: `colab_cli.cli` exposes a module-level `app` object, and the official
  command modules themselves follow the `register(app)` convention — the extension simply hangs
  another `dfs` subgroup off that same `app`. **Zero intrusion.**
- A `.pth` file is the only hook site.py executes automatically: one line,
  `import os, sys; exec(compile("try: ... import colab_dfs_loader ..."))`, which adds
  `~/.colab-dfs-ext` to `sys.path` and eagerly imports the loader; the loader calls
  `colab_dfs_ext.register(app)`. Measured startup overhead: **≈0** (323 ms vs 324 ms — colab
  imports these modules anyway).
- Defensive: the whole snippet is wrapped in try/except. If the extension is missing or broken it
  is silently skipped, and **`colab` itself can never be broken by it** (set `COLAB_DFS_DEBUG=1`
  to see why it was skipped).

## Known implementation details / gotchas

- `remote.exec_code` uses colab_cli's own `ColabRuntime.execute_code()` (no subprocess, no temp
  files) and **must call `runtime.stop()` in a finally block** — otherwise the kernel client's
  non-daemon threads hang the `colab` process on exit (symptom seen in practice: all output
  printed but the command never returns).
- Credentials are shared with the standalone tools: `~/.config/colab-cli/dfs_credentials.json`.
- For the underlying technical background (DriveFS's credentials contract, the metadata shim,
  the three de-browsering approaches), see the parent repo's
  `colab-drivemount-re/docs/REVERSE.md`.

## Layout

```
extension/
├── install.sh               # curl | sh one-line bootstrap installer (POSIX sh)
├── install.py               # install / repair / uninstall / status (pure stdlib)
├── colab_dfs_loader.py      # loader imported by the .pth startup hook
└── colab_dfs_ext/           # the extension itself (copied/symlinked to ~/.colab-dfs-ext/)
    ├── __init__.py          # register(app) entry point
    ├── inject.py            # .pth writing and state management
    ├── commands.py          # the `colab dfs` subcommands (typer)
    ├── dfs_creds.py         # OAuth consent / credential storage
    ├── drive_mount.py       # remote mount script (metadata shim + DriveFS)
    └── remote.py            # executes code inside the runtime via the colab_cli API
```
