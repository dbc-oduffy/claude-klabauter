"""workday-start-run: one deterministic launch for the whole workday-start ceremony.

Parses `launch` and `finish` and hands `run_spec.SPEC` to the shared driver. Nothing a person
supplies continues a run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from coordinator_core.ceremony_common.run import driver
from coordinator_core.ceremony_common.run.contract import EXIT_OK, EXIT_USAGE, RUN_ID_RE
from coordinator_core.ceremony_common.run.driver import (  # noqa: F401
    AGENT_RESULTS_FILE,
    BACKLOG_PREFIX,
    DIGEST_FILE,
    FAILURE_ROWS_FILED_MAX,
    Dispatch,
)
from coordinator_core.workday_start.run_spec import (  # noqa: F401
    CEREMONY,
    FRESHNESS_STEP,
    INBOX_STEP,
    ORIENT_STEP,
    SPEC,
    TEMPLATE_RELPATH,
)

PROG = "workday-start-run"

USAGE = (
    f"usage: {PROG} launch\n"
    f"       {PROG} finish --run-id <id> [--agent-results <path>]"
)


def _parse(argv: Sequence[str]) -> "tuple[str, dict[str, str]] | None":
    if not argv or argv[0] not in ("launch", "finish"):
        return None
    verb, opts, rest = argv[0], {}, list(argv[1:])
    allowed = {"--run-id", "--agent-results"} if verb == "finish" else set()
    while rest:
        flag = rest.pop(0)
        if flag not in allowed or not rest or flag in opts:
            return None
        opts[flag] = rest.pop(0)
    if verb == "finish" and not RUN_ID_RE.fullmatch(opts.get("--run-id", "")):
        return None
    return verb, opts


def main(
    argv: "Sequence[str] | None" = None,
    *,
    dispatch: "Dispatch | None" = None,
    plugin_root: "Path | None" = None,
    repo_root: "Path | None" = None,
    today: "str | None" = None,
) -> int:
    """Run one verb. `dispatch`, `plugin_root`, `repo_root` and `today` are test seams, not flags."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        return EXIT_OK
    parsed = _parse(args)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    verb, opts = parsed
    if repo_root is None:
        from coordinator_core.git.repo_root import show_toplevel

        top = show_toplevel(os.getcwd())
        if not top:
            print(f"{PROG}: not inside a git repository", file=sys.stderr)
            return EXIT_USAGE
        repo_root = Path(top)
    try:
        if verb == "launch":
            from coordinator_core.session.core import resolve_session_id

            return driver.launch(
                SPEC, repo_root=Path(repo_root), flags={},
                session_id=resolve_session_id(str(repo_root)) or "",
                prog_argv=(PROG,), dispatch=dispatch, plugin_root=plugin_root, today=today,
            )
        return driver.finish(
            SPEC, repo_root=Path(repo_root), run_id=opts["--run-id"],
            agent_results_path=opts.get("--agent-results"), prog_argv=(PROG,), dispatch=dispatch,
        )
    except Exception as exc:  # noqa: BLE001 -- the one-line stderr report is the contract
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
