"""Process-wide Windows default: a child spawned without creationflags gets CREATE_NO_WINDOW.

Install it at the entry of every console-less engine root (the warm server, the headless
workflow runner). Their children (git, python, agy) are console programs; with no console
to inherit, Windows opens a new window for each one, or for Windows Terminal as the default
terminal an error pane (0x800700e8), and either steals the PM's focus. A child spawned under
CREATE_NO_WINDOW owns a hidden console that its own children inherit, so one install at the
root covers the whole tree. An explicit creationflags value (CREATE_NEW_CONSOLE for a spawn
that means to be seen) is kept.
"""

from __future__ import annotations

import subprocess
import sys


def install() -> bool:
    """Wrap ``subprocess.Popen.__init__`` once; return whether this call installed it."""
    if sys.platform != "win32" or getattr(subprocess.Popen.__init__, "_windowless", False):
        return False
    original = subprocess.Popen.__init__

    def _windowless_init(self, *args, **kwargs):
        # creationflags is Popen's 14th positional parameter; a caller passing it positionally keeps it.
        if len(args) < 14 and not kwargs.get("creationflags"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    _windowless_init._windowless = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = _windowless_init  # type: ignore[method-assign]
    return True
