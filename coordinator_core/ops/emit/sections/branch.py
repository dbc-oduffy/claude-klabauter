"""Section porter — Branch (envelope key: ``branches``).

Emits the single Branch record for the repo being observed: git branch name + tip SHA,
last-commit metadata, and the ``work/<machine>/<date>`` name parse into machine_hint /
date_hint. Compare-derived fields (merge_base_sha / ahead_by / behind_by) are LEFT null —
they are the D9 "compare not run in single-repo emit" nulls, not enrich-derived.

Port of: emit-cockpit-snapshot.sh (coordinator-claude 07eedcfb, 2026-07-19) — § SECTION 8, Branch.
  Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P08
"""

from __future__ import annotations

import re

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections._shared import run_git

# ``work/<machine>/<date>`` branch-name parse (bash:1526 regex, BASH_REMATCH captures).
_WORK_BRANCH_RE = re.compile(r"^work/([^/]+)/(.+)$")


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build the single Branch record (records=[branch], malformed=[])."""
    # git log -1 for the tip commit; offline/failure → OBSERVED_AT / "" (bash:1517-1518).
    last_commit_at = run_git(ctx.repo_root, "log", "-1", "--format=%cI", "HEAD") or ctx.observed_at
    last_commit_message = run_git(ctx.repo_root, "log", "-1", "--format=%s", "HEAD") or ""

    machine_hint = None
    date_hint = None
    m = _WORK_BRANCH_RE.match(ctx.git_branch)
    if m:
        machine_hint = m.group(1)
        date_hint = m.group(2)

    owner = ctx.repo_name.split("/")[0]

    record = {
        "repo": ctx.repo_name,
        "owner": owner,
        "coordinator_root_path": ".",
        "name": ctx.git_branch,
        "tip_sha": ctx.git_sha,
        "merge_base_sha": None,
        "ahead_by": None,
        "behind_by": None,
        "last_commit_at": last_commit_at,
        "last_commit_message": last_commit_message,
        "machine_hint": machine_hint,
        "date_hint": date_hint,
        "provenance": ctx.provenance("local_fs", path="", derivation="parsed"),
    }

    return [record], []
