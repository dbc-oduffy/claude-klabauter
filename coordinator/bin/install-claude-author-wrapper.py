#!/usr/bin/env python3
"""
install-claude-author-wrapper.py — CLI trampoline over claude-klabauter
coordinator_core.ops.install_claude_author_wrapper.

Installs the co-located `coordinator/bin/claude-author.py` wrapper onto
`${CLAUDE_HOME:-$HOME}/.local/bin/claude-author`. Collapses the bash fence at
coordinator/commands/install.md line 892 (coordinator-content-repo repo) into one call —
see coordinator_core.ops.install_claude_author_wrapper's own docstring for the
full design rationale and negative-spec.

`--wrapper-src` defaults to this trampoline's own sibling `claude-author.py` file
(this script and the wrapper it installs both live under
`<engine_root>/coordinator/bin/`) — no engine-root resolution is needed for
that default, since it is always co-located.

Spec backlink: docs/plans/2026-07-23-skills-carry-no-code-extirpation.md § M3/D9
"""

from __future__ import annotations

INSTALL_CLASS = True

import os
import sys

def _default_wrapper_src() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude-author.py")


def _resolve_run_op_main():
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

    claude_klabauter_root = require_dispatch_engine_on_path()
    from coordinator_core.cli_entry import run_op_main
    return run_op_main


_USAGE = """\
Usage: install-claude-author-wrapper.py [OPTIONS]

Installs the claude-author wrapper onto ${CLAUDE_HOME:-$HOME}/.local/bin/claude-author.
On Windows it also renders the claude-author.cmd and claude-author.ps1 launchers
(the .ps1 with a UTF-8 BOM) beside it.

Options:
  --wrapper-src <path>  Wrapper source (default: the sibling claude-author.py)
  --check-only          Validate without writing any file
  --no-smoke            Windows: skip the post-render launcher smoke check
  -h, --help            Show this help and exit
"""


def _render_windows_launchers(argv: "list[str]") -> int:
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen-claude-author-launcher.py")
    spec = importlib.util.spec_from_file_location("_gen_claude_author_launcher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    forwarded = [a for a in argv if a in ("--check-only", "--no-smoke")]
    return mod.main(forwarded)


def main(argv: "list[str] | None" = None) -> int:
    argv = (sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print(_USAGE, end="")
        return 0

    try:
        run_op_main = _resolve_run_op_main()
    except RuntimeError as exc:
        print(f"install-claude-author-wrapper.py: CLAUDE_KLABAUTER_ROOT resolution failed: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(
            f"install-claude-author-wrapper.py: coordinator_core.cli_entry not importable: {exc}",
            file=sys.stderr,
        )
        return 1

    if not any(a == "--wrapper-src" or a.startswith("--wrapper-src=") for a in argv):
        argv = argv + ["--wrapper-src", _default_wrapper_src()]

    try:
        code = run_op_main("coordinator_core.ops.install_claude_author_wrapper", argv)
    except ImportError as exc:
        print(
            f"install-claude-author-wrapper.py: coordinator_core.ops.install_claude_author_wrapper "
            f"not importable: {exc}",
            file=sys.stderr,
        )
        return 1

    if code == 0 and os.name == "nt":
        return _render_windows_launchers(argv)

    return code


if __name__ == "__main__":
    sys.exit(main())
