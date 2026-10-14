"""coordinator_core.ops.dispatch_emit.delivery_credit -- spine rows already delivered before a
run's base.

A run's delivery diff starts at `run_base_sha`, so a row coded before a resume (on another
machine, in an earlier run) has no hunk in it and reads as unbacked. `rows_backed_before_base`
names those rows: `disposition: coded` whose `disposition_ref` commit exists and is an ancestor
of `run_base_sha` (a `repo_key:` prefix on the ref is stripped). Two git spawns for the whole spine, never one per row.

Failure contract: `ancestor_refs` returns `(ancestors, unknown)`. A failed git spawn or a
line-count mismatch puts every given ref in `unknown`, never in a silent "no ancestors" set;
a ref git answered as missing or non-commit is in neither set. Membership in `ancestors` is
parity-tested against `git merge-base --is-ancestor`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.frontmatter.body_blocks import LocateStatus

_SHA = re.compile(r"[0-9a-fA-F]{7,40}")


def ancestor_refs(repo_root: Path, refs: List[str], base: str) -> Tuple[set, set]:
    """`(ancestors, unknown)` over `refs` (as given). `ancestors` names a commit reachable from
    `base`; `unknown` is every ref when git could not answer (failed spawn or line mismatch).
    A ref git answered as missing or non-commit is in neither set."""
    if not refs or not base:
        return set(), set()
    cwd = str(repo_root)
    everything = set(refs)
    checked = run_git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=cwd,
        input=(chr(10).join(refs) + chr(10)).encode("utf-8"),
    )
    lines = checked.stdout.splitlines()
    if checked.returncode != 0 or len(lines) != len(refs):
        return set(), everything
    oid_of = {}
    for ref, line in zip(refs, lines):
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit":
            oid_of[ref] = parts[0].lower()
    if not oid_of:
        return set(), set()
    ahead = run_git(["rev-list", *sorted(set(oid_of.values())), "--not", base], cwd=cwd)
    if ahead.returncode != 0:
        return set(), everything
    not_ancestors = {ln.strip().lower() for ln in ahead.stdout.splitlines()}
    return {ref for ref, oid in oid_of.items() if oid not in not_ancestors}, set()


def coded_row_refs(plan_path: str) -> dict:
    """`{sha: [row ids]}` for the plan's `coded` rows, a `repo_key:` prefix stripped from each
    `disposition_ref`; `{}` when the spine is unreadable."""
    try:
        result = load_rows(Path(plan_path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    if result.status is not LocateStatus.LOCATED:
        return {}
    by_ref: dict = {}
    for row in result.rows:
        if not isinstance(row, dict) or row.get("disposition") != "coded":
            continue
        ref = str(row.get("disposition_ref") or "").strip().rpartition(":")[2].strip()
        match = _SHA.fullmatch(ref)
        if match and row.get("id"):
            by_ref.setdefault(ref, []).append(str(row["id"]))
    return by_ref


def rows_backed_before_base(repo_root: Path, plan_path: str, run_base_sha: Optional[str]) -> List[str]:
    """Ids of the plan's `coded` rows whose `disposition_ref` commit is an ancestor of
    `run_base_sha`; `[]` when the spine is unreadable or `run_base_sha` is empty. Refs whose
    ancestry git could not read are omitted (read as unbacked here); only the reverify credit
    note reports them as unknown."""
    if not run_base_sha:
        return []
    by_ref = coded_row_refs(plan_path)
    if not by_ref:
        return []
    ok, _unknown = ancestor_refs(repo_root, list(by_ref), run_base_sha)
    return [i for ref, ids in by_ref.items() if ref in ok for i in ids]
