#!/usr/bin/env python3
"""bug-backlog-reap — run every open tf backlog row's member tests and close the green rows.

Usage: `bug-backlog-reap --repo <root>`. Runs no model: batched pytest over the member tests,
closes all-green rows (`closed_by: bug-backlog-reap`), writes the reap record, and prints one line:
`closed N, red M, unrun K, <record path>`. Exit: 0 on a written record, 1 otherwise, 2 usage.
"""
from __future__ import annotations

import sys


def _import_reap():
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

    require_dispatch_engine_on_path()
    from coordinator_core.bug_blitz.reap_green import main

    return main


def _repo_arg(args: "list[str]") -> "str | None":
    if len(args) == 2 and args[0] == "--repo":
        return args[1]
    if len(args) == 1 and args[0].startswith("--repo="):
        return args[0].split("=", 1)[1]
    return None


def main(argv: "list[str] | None" = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a in ("-h", "--help") for a in args):
        print((__doc__ or "").strip())
        return 0
    repo = _repo_arg(args)
    if not repo:
        print("usage: bug-backlog-reap --repo <root>", file=sys.stderr)
        return 2
    try:
        run = _import_reap()
    except (RuntimeError, ImportError) as exc:
        print(f"bug-backlog-reap: engine unavailable: {exc}", file=sys.stderr)
        return 1
    try:
        return run([repo])
    except Exception as exc:  # noqa: BLE001 — any failure is exit 1, no record written
        print(f"bug-backlog-reap: failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
