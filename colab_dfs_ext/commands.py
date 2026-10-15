"""The `colab dfs ...` command group."""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import typer
from typing_extensions import Annotated

from . import __version__

# Keep every command tolerant of unknown flags: they are forwarded to the underlying tool,
# so the CLI surface can never block access to a flag we did not think to expose.
EXTRA = {"allow_extra_args": True, "ignore_unknown_options": True,
         "help_option_names": ["-h", "--help"]}

dfs = typer.Typer(name="dfs", no_args_is_help=True,
                  context_settings={"help_option_names": ["-h", "--help"]})

Session = Annotated[Optional[str], typer.Option("-s", "--session",
                                                help="colab session name (default: the only running one)")]
Path = Annotated[str, typer.Argument(help="mountpoint inside the runtime (default /content/drive)")]
Timeout = Annotated[int, typer.Option("--timeout", help="seconds to wait for the mount")]
Force = Annotated[bool, typer.Option("--force", help="remount even if already mounted")]


# --- mount --------------------------------------------------------------------------------
def _run_mount(cfg: dict, session: Optional[str], verbose: bool = True) -> int:
    """Execute the DriveFS mount payload in the runtime and report the result."""
    from . import drive_mount, remote

    name = remote.resolve_session(session)
    if verbose:
        print("[dfs] %s -> %s (%s mode), browser-free ..." % (name, cfg["mountpoint"], cfg["mode"]))
    t0 = time.time()
    text = remote.exec_code(name, drive_mount.payload(cfg), int(cfg["timeout"]) + 90)
    res = drive_mount.parse_result(text)
    if res and res.get("ok"):
        if verbose:
            if res.get("note"):
                print("[dfs] OK: %s" % res["note"])
            else:
                print("[dfs] OK in %.1fs: Drive at %s (%s entries top-level, mode=%s)"
                      % (time.time() - t0, res.get("mountpoint") or cfg["mountpoint"],
                         res.get("entries"), res.get("mode") or cfg["mode"]))
            if res.get("shim"):
                print("[dfs] creds shim: %s  (keep the runtime alive; it re-serves tokens)"
                      % res["shim"])
        return 0
    if res:
        print("[dfs] FAILED: %s" % json.dumps(res, indent=2)[:4000])
        for key in ("drive_log", "dfs_log_errors", "log", "error", "hint"):
            if res.get(key):
                print("--- %s ---\n%s" % (key, res[key]))
    else:
        print("[dfs] the payload produced no DFS_RESULT; kernel output:\n%s" % text[-3000:])
    return 1


@dfs.command(context_settings=EXTRA)
def mount(
    ctx: typer.Context,
    session: Session = None,
    path: Path = "/content/drive",
    mode: Annotated[str, typer.Option("--mode", help="shim (default, works with any client's token)"
                                           " | rt (pass --refresh_token to DriveFS)")] = "shim",
    force: Force = False,
    readonly: Annotated[bool, typer.Option("--readonly", help="read-only mount")] = False,
    timeout: Timeout = 120,
    port: Annotated[int, typer.Option("--port", help="shim port inside the runtime")] = 8012,
    from_env: Annotated[bool, typer.Option("--from-env",
                                           help="read COLAB_DRIVE_* env vars instead of the store")] = False,
    cli_token: Annotated[bool, typer.Option("--cli-token",
                                            help="use the colab-cli token (needs the drive scope; "
                                                 "default colab-cli tokens only have drive.file)")] = False,
    purge_state: Annotated[bool, typer.Option("--purge-state",
                                              help="delete the runtime's DriveFS local state first")] = False,
    user_id: Annotated[Optional[str], typer.Option("--user-id", help="account GaiaId override")] = None,
    email: Annotated[Optional[str], typer.Option("--email", help="account email override")] = None,
    drive_extra: Annotated[Optional[list[str]], typer.Option("--drive-extra",
                                                            help="extra flag for /opt/google/drive/drive")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print config + remote payload")] = False,
    via_cli: Annotated[bool, typer.Option("--via-cli",
                                          help="shell out to `colab exec` instead of the kernel API")] = False,
):
    """Mount Google Drive in the runtime with no browser and no dfs_ephemeral consent."""
    from . import drive_mount

    if " " in path:
        raise typer.BadParameter("mountpoint must not contain a space")
    argv = ["--path", path, "--mode", mode, "--timeout", str(timeout), "--port", str(port)]
    if session:
        argv += ["-s", session]
    for flag, on in (("--force", force), ("--readonly", readonly), ("--from-env", from_env),
                     ("--cli-token", cli_token), ("--purge-state", purge_state),
                     ("--dry-run", dry_run)):
        if on:
            argv.append(flag)
    for flag, val in (("--user-id", user_id), ("--email", email)):
        if val:
            argv += [flag, val]
    for extra in drive_extra or []:
        argv += ["--drive-extra", extra]
    argv += list(ctx.args)

    if dry_run or via_cli:
        raise typer.Exit(code=drive_mount.main(argv))

    cfg = drive_mount.make_cfg(session=session, path=path, mode=mode, force=force,
                               readonly=readonly, timeout=timeout, port=port,
                               cli_token=cli_token, from_env=from_env,
                               drive_extra=drive_extra or [], purge_state=purge_state,
                               user_id=user_id, email=email)
    raise typer.Exit(code=_run_mount(cfg, session))


@dfs.command(context_settings=EXTRA)
def umount(session: Session = None, path: Path = "/content/drive"):
    """Unmount Drive (and stop the in-runtime creds shim / drive processes)."""
    from . import remote

    name = remote.resolve_session(session)
    out = remote.exec_code(name, remote.render(remote.UMOUNT_REMOTE, path), timeout=120)
    line = [l for l in out.splitlines() if l.startswith("DFS_RESULT ")]
    if line:
        res = json.loads(line[0][len("DFS_RESULT "):])
        print("[dfs] %s %s (%s)" % (name, "unmounted" if not res.get("mounted") else "STILL MOUNTED",
                                    res.get("mountpoint")))
        raise typer.Exit(code=0 if not res.get("mounted") else 1)
    print(out[-2000:])
    raise typer.Exit(code=1)


@dfs.command(context_settings=EXTRA)
def status(session: Session = None, path: Path = "/content/drive"):
    """Is it mounted? Who is serving tokens? What does the runtime see?"""
    from . import remote

    name = remote.resolve_session(session)
    out = remote.exec_code(name, remote.render(remote.STATUS_REMOTE, path), timeout=90)
    line = [l for l in out.splitlines() if l.startswith("DFS_RESULT ")]
    if not line:
        print(out[-2000:])
        raise typer.Exit(code=1)
    res = json.loads(line[0][len("DFS_RESULT "):])
    print("session     : %s" % name)
    print("mountpoint  : %s  %s" % (res.get("mountpoint"),
                                    "MOUNTED" if res.get("mounted") else "not mounted"))
    for l in res.get("mount_line") or []:
        print("fuse        : %s" % l.strip()[:150])
    print("My Drive    : %s entries" % res.get("entries"))
    print("drive procs : %s   creds shim procs: %s" % (res.get("drive_procs"), res.get("shim_procs")))
    if res.get("shim_log"):
        print("shim log    : %s" % str(res["shim_log"]).replace("\n", " | ")[:200])
    print("colab env   : %s" % json.dumps(res.get("official_env") or {}))
    creds = os.path.expanduser("~/.config/colab-cli/dfs_credentials.json")
    print("local creds : %s (%s)" % (creds, "present" if os.path.exists(creds)
                                     else "MISSING -> run `colab dfs consent`"))


# --- credentials --------------------------------------------------------------------------
@dfs.command(context_settings=EXTRA)
def consent(
    ctx: typer.Context,
    flavor: Annotated[str, typer.Option("--flavor",
                                        help="dfs (default: the client baked into DriveFS, loopback "
                                             "callback, nothing to copy-paste) | sdk | custom")] = "dfs",
    minimal: Annotated[bool, typer.Option("--minimal", help="only openid + email + drive scopes")] = True,
    port: Annotated[int, typer.Option("--port", help="loopback port (0 = auto)")] = 0,
    user_id: Annotated[Optional[str], typer.Option("--user-id", help="account GaiaId")] = None,
    email: Annotated[Optional[str], typer.Option("--email", help="account email")] = None,
    code: Annotated[Optional[str], typer.Option("--code", help="finish a --url-only flow later")] = None,
    url_only: Annotated[bool, typer.Option("--url-only", help="print the consent URL and exit")] = False,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="do not open the browser")] = False,
):
    """One-time Google consent -> refresh token in ~/.config/colab-cli/dfs_credentials.json."""
    from . import dfs_creds

    argv = ["consent", "--flavor", flavor]
    if minimal:
        argv.append("--minimal")
    argv += ["--port", str(port)]
    for flag, val in (("--user-id", user_id), ("--email", email), ("--code", code)):
        if val:
            argv += [flag, val]
    for flag, on in (("--url-only", url_only), ("--no-browser", no_browser)):
        if on:
            argv.append(flag)
    argv += list(ctx.args)
    if not minimal:
        argv = [a for a in argv if a != "--minimal"]
    try:
        dfs_creds.main(argv)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


@dfs.command(context_settings=EXTRA)
def show():
    """Summarise the stored refresh token (never prints the secret)."""
    from . import dfs_creds

    try:
        dfs_creds.main(["show"])
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


@dfs.command(context_settings=EXTRA)
def token(
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
):
    """Mint (and cache-check) an access token right now -- proves the refresh token works."""
    from . import dfs_creds

    argv = ["token"] + (["--json"] if json_out else [])
    try:
        dfs_creds.main(argv)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code or 0))


# --- plumbing ----------------------------------------------------------------------------─
@dfs.command(context_settings=EXTRA)
def install(
    site_dir: Annotated[Optional[str], typer.Option("--site-dir",
                                                    help="site-packages to write the .pth into")] = None,
    quiet: Annotated[bool, typer.Option("--quiet")] = False,
):
    """(Re-)inject this extension into the colab-cli environment. Run after an upgrade."""
    from . import inject

    rep = inject.ensure(site_dir=site_dir, quiet=quiet)
    if rep.get("error"):
        typer.echo("ERROR: %s" % rep["error"], err=True)
        raise typer.Exit(code=1)
    if not quiet:
        typer.echo("[colab-dfs] .pth : %s (%s)"
                   % (rep.get("pth"), "updated" if rep.get("changed") else "already there"))
        typer.echo("[colab-dfs] source: %s" % rep.get("source"))
        typer.echo("[colab-dfs] `colab dfs` is available in the NEXT colab invocation.")


@dfs.command(context_settings=EXTRA)
def doctor():
    """Diagnose the injection, the credentials and the runtime state."""
    from . import inject

    st = inject.status()
    print("colab dfs extension v%s" % __version__)
    print("  registered here : %s" % ("yes" if inject.reload_ok() else "no (fresh process needed)"))
    print("  colab shim      : %s" % st["colab_shim"])
    print("  interpreter     : %s" % st["python"])
    print("  site-packages   : %s%s" % (st["site_packages"],
                                        "" if st["colab_cli_here"] else "  <- colab_cli NOT here"))
    print("  hook (.pth)     : %s  [%s]" % (st["pth"], "ok" if st["pth_present"] else "MISSING"))
    print("  extension source: %s" % (st["source"] or "MISSING"))
    if not st["pth_present"]:
        print("\n  fix: python3 %s/install.py   (or `colab dfs install`)"
              % (st["source"] or "~/.colab-dfs-ext"))
    creds = os.path.expanduser("~/.config/colab-cli/dfs_credentials.json")
    if os.path.exists(creds):
        try:
            data = json.load(open(creds))
            print("  credentials     : %s" % creds)
            print("  client          : %s" % data.get("client_id"))
            print("  scopes          : %s" % " ".join(data.get("scopes", []))[:150])
            print("  user id / email : %s / %s" % (data.get("user_id"), data.get("email")))
            if not data.get("user_id"):
                print("  WARN: no user_id -> DriveFS will CANNOT_START_CORE; re-run `colab dfs consent`")
            if "drive" not in " ".join(data.get("scopes", [])):
                print("  WARN: no full drive scope -> DriveFS 403 insufficient_scope")
        except Exception as exc:
            print("  credentials     : unreadable: %r" % exc)
    else:
        print("  credentials     : MISSING -> run `colab dfs consent`")
    print("\n  next step       : colab dfs mount -s <session>")


@dfs.command(context_settings=EXTRA)
def version():
    """Extension version + where it came from."""
    from . import inject

    st = inject.status()
    print(json.dumps({"extension": __version__, **st}, indent=2, default=str))
