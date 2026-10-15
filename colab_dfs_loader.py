"""Loaded by colab_dfs.pth at interpreter startup inside the colab-cli environment.

Imports colab_cli.cli eagerly (in this venv that is nearly free: the venv exists to run
colab, so the module would be imported moments later anyway), then registers the
`colab dfs` command group on the typer app that colab_cli.cli built.

Best-effort by design: any failure is silent unless COLAB_DFS_DEBUG=1, so a missing or
half-updated extension can never stop `colab` itself from working.
"""

import os
import sys

DEBUG = os.environ.get("COLAB_DFS_DEBUG")


def _register():
    import colab_cli.cli as cli

    if getattr(cli, "_colab_dfs_registered", False):
        return
    from colab_dfs_ext import register

    if register(cli.app) and DEBUG:
        import colab_dfs_ext

        sys.stderr.write("[colab-dfs] registered `colab dfs` v%s\n" % colab_dfs_ext.__version__)


try:
    _register()
except ImportError as exc:  # colab_cli not installed here / extension not fully installed
    if DEBUG:
        sys.stderr.write("[colab-dfs] not loaded: %r\n" % (exc,))
except Exception as exc:
    sys.stderr.write("[colab-dfs] extension failed to load: %r\n"
                     "  fix: python3 ~/.colab-dfs-ext/install.py\n" % (exc,))
