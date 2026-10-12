"""ceremony-run: launch or finish one registered ceremony on the shared driver.

Parses `<ceremony> launch [flags]` and `<ceremony> finish --run-id <id> [--agent-results <path>]`
and hands the registered spec to the driver. The driver alone refuses a flag outside the spec.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Sequence

from coordinator_core.ceremony_common.run import driver, spec as spec_mod
from coordinator_core.ceremony_common.run.contract import EXIT_OK, EXIT_USAGE, RUN_ID_RE
from coordinator_core.ceremony_common.run.driver import Dispatch

PROG = "ceremony-run"

USAGE = (
    f"usage: {PROG} <ceremony> launch [flags]\n"
    f"       {PROG} <ceremony> finish --run-id <id> [--agent-results <path>]\n"
    "ceremonies: %s"
)


def _names() -> dict[str, str]:
    return {key.replace("_", "-"): key for key in spec_mod.CEREMONY_SPECS}


def _usage() -> str:
    return USAGE % ", ".join(sorted(_names()))


def _launch_flags(rest: Sequence[str]) -> "dict[str, Any] | None":
    flags: dict[str, Any] = {}
    items = list(rest)
    while items:
        flag = items.pop(0)
        if not flag.startswith("--") or flag in flags:
            return None
        if items and not items[0].startswith("--"):
            flags[flag] = items.pop(0)
        else:
            flags[flag] = True
    return flags


def _finish_opts(rest: Sequence[str]) -> "dict[str, str] | None":
    opts: dict[str, str] = {}
    items = list(rest)
    while items:
        flag = items.pop(0)
        if flag not in ("--run-id", "--agent-results") or not items or flag in opts:
            return None
        opts[flag] = items.pop(0)
    if not RUN_ID_RE.fullmatch(opts.get("--run-id", "")):
        return None
    return opts


def main(
    argv: "Sequence[str] | None" = None,
    *,
    dispatch: "Dispatch | None" = None,
    plugin_root: "Path | None" = None,
    repo_root: "Path | None" = None,
    today: "str | None" = None,
) -> int:
    """Run one verb of one ceremony. `dispatch`, `plugin_root`, `repo_root` and `today` are test seams."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print(_usage())
        return EXIT_OK
    if len(args) < 2:
        print(_usage(), file=sys.stderr)
        return EXIT_USAGE
    ceremony, verb, rest = args[0], args[1], args[2:]
    key = _names().get(ceremony)
    if key is None:
        print(f"{PROG}: unknown ceremony {ceremony!r}; known: {', '.join(sorted(_names()))}", file=sys.stderr)
        return EXIT_USAGE
    if verb == "launch":
        flags, opts = _launch_flags(rest), {}
        bad = flags is None
    elif verb == "finish":
        flags, opts = None, _finish_opts(rest)
        bad = opts is None
    else:
        bad = True
    if bad:
        print(_usage(), file=sys.stderr)
        return EXIT_USAGE
    if repo_root is None:
        from coordinator_core.git.repo_root import show_toplevel

        top = show_toplevel(os.getcwd())
        if not top:
            print(f"{PROG}: not inside a git repository", file=sys.stderr)
            return EXIT_USAGE
        repo_root = Path(top)
    prog_argv = (PROG, ceremony)
    try:
        spec = spec_mod.load_spec(key)
        if verb == "launch":
            from coordinator_core.session.core import resolve_session_id

            return driver.launch(
                spec, repo_root=Path(repo_root), flags=flags,
                session_id=resolve_session_id(str(repo_root)) or "",
                prog_argv=prog_argv, dispatch=dispatch, plugin_root=plugin_root, today=today,
            )
        return driver.finish(
            spec, repo_root=Path(repo_root), run_id=opts["--run-id"],
            agent_results_path=opts.get("--agent-results"), prog_argv=prog_argv, dispatch=dispatch,
        )
    except spec_mod.SpecUnavailable as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 -- the one-line stderr report is the contract
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
