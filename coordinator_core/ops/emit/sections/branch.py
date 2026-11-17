
"""Emit-section collector: renders the current git branch's own record, including machine/date hints parsed from an `<owner>/<machine>/<date>` (or legacy `work/<machine>/<date>`) branch name."""

from __future__ import annotations

import re

from coordinator_core.daily_branch import is_work_branch
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections._shared import run_git

# ``<owner>/<machine>/<date>`` branch-name parse (owner a username or legacy ``work``);
# the is_work_branch gate keeps ``feature/x/y`` and ``origin/...`` reads out.
_BRANCH_PARTS_RE = re.compile(r"^[^/]+/([^/]+)/(.+)$")


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    # git log -1 for the tip commit; offline/failure → OBSERVED_AT / "" (bash:1517-1518).
    last_commit_at = run_git(ctx.repo_root, "log", "-1", "--format=%cI", "HEAD") or ctx.observed_at
    last_commit_message = run_git(ctx.repo_root, "log", "-1", "--format=%s", "HEAD") or ""

    machine_hint = None
    date_hint = None
    m = None
    if is_work_branch(ctx.git_branch) and not ctx.git_branch.startswith("origin/"):
        m = _BRANCH_PARTS_RE.match(ctx.git_branch)
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
