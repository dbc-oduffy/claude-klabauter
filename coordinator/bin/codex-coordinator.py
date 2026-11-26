#!/usr/bin/env python3
"""Launch codex as a coordinator EM, leaving bare `codex` unchanged.

`coordinator-codex` and `codex-coordinator` drop `--coordinator`, put
`--dangerously-bypass-approvals-and-sandbox` first (a caller `--yolo` folds into it, never
stacks), and hand the rest to `unimatrix-codex-launch.main`. That launcher owns the EM model
(`select_model` also sets COORDINATOR_CODEX_EXPECTED_MODEL), the daemon, and the exec: on
Windows a console-inheriting `subprocess.call` with no timeout, on POSIX the daemon then
`execv`. This file adds no `--model`, holds no inbox, and maps no session id: mail arrives
through the SessionStart peers.json binding and the footer id through the Stop hook.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BYPASS = "--dangerously-bypass-approvals-and-sandbox"
_BYPASS_SPELLINGS = frozenset({BYPASS, "--yolo"})


def strip_coordinator(argv: list[str]) -> list[str]:
    """Drop `--coordinator`. The real codex binary does not know that flag."""
    return [arg for arg in argv if arg != "--coordinator"]


def interactive_argv(binary: str, extra: list[str]) -> list[str]:
    """TUI launch: one bypass flag first, then the caller's args. Never a --model."""
    option_end = extra.index("--") if "--" in extra else len(extra)
    kept = [a for i, a in enumerate(extra) if i >= option_end or a not in _BYPASS_SPELLINGS]
    return [binary, BYPASS, *kept]


def load_launcher() -> ModuleType:
    """`unimatrix-codex-launch` from beside this script."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    spec = importlib.util.spec_from_file_location("unimatrix_codex_launch_coordinator",
                                                  here / "unimatrix-codex-launch.py")
    if spec is None or spec.loader is None:
        raise SystemExit("codex-coordinator: cannot load unimatrix-codex-launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    argv = strip_coordinator(list(sys.argv[1:] if argv is None else argv))
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    args = interactive_argv("codex", argv)[1:]
    return load_launcher().main(args)


if __name__ == "__main__":
    raise SystemExit(main())
