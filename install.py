#!/usr/bin/env python3
"""Install / repair / inspect the `colab dfs` extension for google-colab-cli.

Runs with ANY python3 (stdlib only) -- that is the point: after `colab update --install`
rebuilds the tool environment, the extension source in ~/.colab-dfs-ext is still there and
this script re-wires it in one command.

    python3 install.py                  # install (default: copy source + drop the .pth)
    python3 install.py --status         # what is wired where
    python3 install.py --uninstall      # remove the .pth (the copy in ~/.colab-dfs-ext stays)
    python3 install.py --link           # dev mode: symlink the repo instead of copying
    python3 install.py --site-dir DIR   # target a specific site-packages
    COLAB_DFS_DEBUG=1 ...               # verbose loader diagnostics
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from colab_dfs_ext import inject  # noqa: E402  (stdlib-only module)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="show the wiring and exit")
    ap.add_argument("--uninstall", action="store_true", help="remove the .pth hook")
    ap.add_argument("--link", action="store_true",
                    help="dev: symlink the extension source instead of copying it")
    ap.add_argument("--site-dir", help="site-packages of the colab-cli environment")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.status:
        for key, val in inject.status().items():
            print("%-16s: %s" % (key, val))
        return 0

    if args.uninstall:
        out = inject.remove(args.site_dir)
        print("[colab-dfs] removed %s" % out["pth"] if out["removed"]
              else "[colab-dfs] nothing to remove (%s)" % out["pth"])
        return 0

    if args.link:
        src = inject.source_dir()
        if not src:
            sys.exit("cannot locate the extension source (run from the repo's extension/ dir)")
        os.makedirs(inject.install_root(), exist_ok=True)
        dst = os.path.join(inject.install_root(), "colab_dfs_ext")
        if os.path.islink(dst) or os.path.exists(dst):
            os.remove(dst) if os.path.islink(dst) else None
            if os.path.isdir(dst) and not os.path.islink(dst):
                import shutil
                shutil.rmtree(dst)
        os.symlink(os.path.join(src, "colab_dfs_ext"), dst)
        loader_dst = os.path.join(inject.install_root(), "colab_dfs_loader.py")
        loader_src = os.path.join(src, "colab_dfs_loader.py")
        if os.path.exists(loader_dst):
            os.remove(loader_dst)
        os.symlink(loader_src, loader_dst)
        print("[colab-dfs] linked %s -> %s" % (inject.install_root(), src))

    rep = inject.ensure(site_dir=args.site_dir, copy_source=not args.link, quiet=args.quiet)
    if rep.get("error"):
        print("ERROR: %s" % rep["error"], file=sys.stderr)
        return 1
    if not args.quiet:
        st = inject.status()
        print("[colab-dfs] .pth : %s (%s)"
              % (rep.get("pth"), "written" if rep.get("changed") else "already present"))
        print("[colab-dfs] source: %s" % rep.get("source"))
        if not st.get("colab_cli_here"):
            print("[colab-dfs] WARN: %s does not contain colab_cli" % st.get("site_packages"))
        print("[colab-dfs] done. `colab dfs --help` works in the NEXT colab invocation.")
        print("[colab-dfs] after a colab-cli upgrade, re-run this script if `colab dfs` disappears.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
