"""
coordinator_core.ops.blocked

Port of: blocked.sh (DoE b5a4192c, 2026-07-20).

Purpose: Two-part blocked-work detector, unchanged from the bash oracle:
  1. Handoffs with `status: blocked` or `status: paused` — via
     `coordinator_core.ops.ceremony.records_query.query_records`, makima's native records
     seam (no more `bin/query-records.js` node subprocess — see § Negative-spec).
  2. `tasks/*/todo.md` files with `status: blocked`, `status: paused`, `blocked:`,
     or `waiting on:` lines — via direct line-scan (query-records has no todo type;
     direct scan is the approved approach here, per the bash oracle's own comment).

Spec backlink: archive/specs/2026-05-05-script-first-deterministic-ops.md §T2

Exit codes (parity-critical, unchanged from the bash oracle):
  0 — always, EXCEPT when this process is not run inside a git repository, in
      which case it prints an ERROR line to stderr and returns 1. Callers decide
      what to do with the found/not-found output; this is not a gate.

Negative-spec:
    - Does NOT parse frontmatter directly for handoffs — delegates to
      `coordinator_core.ops.ceremony.records_query.query_records(record_type="handoff",
      where="status in (blocked,paused)")`, makima's own native records seam (repointed
      2026-07-22, retiring the `bin/query-records.js` node subprocess this site used to
      shell out to). The one-line-per-record markdown-list rendering for `--format
      markdown-list --type handoff` is replicated in-process, byte-for-byte, from
      `query-records.js`'s `TYPE_DISPLAY.handoff` (query-records.js:308):
      ``- [{title|basename}]({path}) — {deployment_state|status|'unknown'}``.
    - The todo scan is a deliberate scoped exception (direct line matching, not a
      frontmatter parse), NOT a precedent for reimplementing frontmatter elsewhere.
    - Does NOT recurse below `tasks/<name>/todo.md` (mindepth 2, maxdepth 2 in the
      bash oracle) — mirrors that exact depth, not a general todo-file finder.
    - No more node/subprocess dependency for the handoff leg — `BLOCKED_QUERY_RECORDS_DIR`
      and the DoE `coordinator/bin/query-records.js` co-location are retired for this site;
      `query_records()` walks makima's own worktree directly, so `repo_root` is the only
      root this leg now depends on (same root the removed spawn's `cwd`/`--root` resolved
      to). Any failure in the seam (unreadable dir, bad glob, parse error) is swallowed —
      mirrors the bash oracle's `|| true` fail-open exactly as before.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.wire_paths import rel_id

_PROG = "blocked.sh"  # literal program-name prefix — matches the DoE filename


def _git_repo_root(cwd: Optional[str] = None) -> Optional[str]:
    """Resolve the git repo root for `cwd` (defaults to process cwd). None if not a repo."""
    return show_toplevel(cwd)


def _render_handoff_line(path: str, frontmatter: dict) -> str:
    """Render one handoff record as a `markdown-list` line.

    Byte-for-byte port of query-records.js's `TYPE_DISPLAY.handoff`
    (query-records.js:308): ``- [{title|basename}]({path}) — {deployment_state|status|'unknown'}``.
    """
    title = frontmatter.get("title") or os.path.basename(path)
    status = frontmatter.get("deployment_state") or frontmatter.get("status") or "unknown"
    return f"- [{title}]({path}) — {status}"


def _run_query_records(repo_root: str) -> str:
    """Native-seam handoff query for blocked/paused; empty string on any failure.

    Mirrors the retired bash/node oracle's `|| true` — a query-records failure is
    swallowed, never propagated as an error for this section (fail-open, unchanged).
    """
    try:
        records = query_records(
            "handoff",
            Path(repo_root),
            where="status in (blocked,paused)",
        )
        lines = [
            _render_handoff_line(rec["path"], rec["frontmatter"])
            for rec in records
        ]
    except (Exception, SystemExit):
        print(f"skip: _run_query_records: query_records( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    return "\n".join(lines).strip()


_TODO_MARKERS = ("status: blocked", "status: paused", "blocked:", "waiting on:")


def _grep_todo_matches(path: str) -> List[str]:
    """Return `lineno:line` strings for every line in `path` matching a blocked marker."""
    matches: List[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.rstrip("\n")
                if any(marker in stripped for marker in _TODO_MARKERS):
                    matches.append(f"{lineno}:{stripped}")
    except OSError:
        print(f"skip: _grep_todo_matches: with open(path, encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    return matches


def _find_todo_files(tasks_dir: str) -> List[str]:
    """Return sorted `tasks/<name>/todo.md` paths — exactly mindepth 2, maxdepth 2."""
    found: List[str] = []
    try:
        entries = sorted(os.listdir(tasks_dir))
    except OSError:
        print(f"skip: _find_todo_files: entries = sorted(os.listdir(tasks_dir)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    for entry in entries:
        candidate = os.path.join(tasks_dir, entry, "todo.md")
        if os.path.isfile(candidate):
            found.append(candidate)
    return found


def main(argv: List[str]) -> int:
    repo_root = _git_repo_root()
    if repo_root is None:
        print("ERROR: not inside a git repository", file=sys.stderr)
        return 1

    found_any = False

    print("== Blocked handoffs ==")
    handoff_output = _run_query_records(repo_root)
    if handoff_output:
        print(handoff_output)
        found_any = True
    else:
        print("  (none)")
    print("")

    print("== Blocked todo items ==")
    tasks_dir = os.path.join(repo_root, "tasks")
    if os.path.isdir(tasks_dir):
        todo_hit = False
        for todo_path in _find_todo_files(tasks_dir):
            matches = _grep_todo_matches(todo_path)
            if matches:
                rel = rel_id(Path(todo_path), Path(repo_root))
                print(f"  {rel}:")
                for match in matches:
                    print(f"    {match}")
                todo_hit = True
                found_any = True
        if not todo_hit:
            print("  (none)")
    else:
        print("  (tasks/ not found)")
    print("")

    if not found_any:
        print("No blocked items found.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
