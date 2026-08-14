"""
coordinator_core.ops.plan_tasks_grouping_digest — read-only compute-and-print
surface over `compute_grouping_digest` (plan.tasks.grouping_digest).

Purpose: close the gap the 2026-07-29 grouping-approval contract otherwise
leaves open. `plan_tasks_mutate.py`'s `resolve` verb refuses to close a
task-spine row into a `defer`/`ruled_out` grouping unless that grouping's
`grouping_approvals` block reads `status: approved` AND carries a `digest`
matching a fresh `compute_grouping_digest` recomputation over the membership
the write is about to produce (see that module's docstring, and
`schema_validate.compute_grouping_digest`'s own docstring for the recipe).
Every existing caller of `compute_grouping_digest` is a VERIFIER — it checks
a digest someone already wrote. Nothing computes the value FOR the person who
has to write it, and doctrine forbids hand-authoring it. This module is that
missing compute-and-print step, nothing else.

Homing (per DoE-claude's dispatch brief for this module, mirroring
`plan_tasks_render.py`'s own homing note): a NEW module, not a verb bolted
onto `plan_tasks_mutate.py`. This is a READ concern — it never opens the plan
for write, never takes the file lock, never calls `locked_rmw`. Giving
`plan_tasks_mutate.py` a compute-only reason to change alongside its existing
mutate-verb reasons is exactly what that module's own docstring already
declines to do for `plan_tasks_render.py`'s two read projections; the same
declination applies here.

THE PROSPECTIVE-MEMBERSHIP CONTRACT (read this before calling `main`/the
handler): the guard `plan_tasks_mutate._resolve` checks against does NOT
compare the grouping's CURRENT membership — it compares against the
membership the pending `resolve` write is about to PRODUCE. A PM approving a
cut of rows that are still `open` (or otherwise not yet in the grouping) is
approving a set those rows are not members of yet. So this module takes a
`cut` — the set of task ids being closed, each paired with its TARGET
disposition — and computes the digest over the spine as it would read AFTER
those dispositions are applied, exactly mirroring the `prospective` list
`plan_tasks_mutate._resolve` builds before calling `compute_grouping_digest`
(see that function's own comment: "The digest must cover the membership
AFTER this write, not before it"). Computing over CURRENT membership instead
would emit a digest the guard rejects on the very first close of a freshly
approved cut — the single most likely way to get this module wrong.

FRAMING — mechanical value, not authorization. This module records a digest
that reflects an approval the PM already gave; it does not grant one, and
printing a digest is not evidence a cut was approved. The `resolve` verb
checks `status: approved` and a non-empty `pm_utterance` BEFORE it ever
reaches the digest comparison (`plan_tasks_mutate.py`, the `resolve` verb's
CLOSED-disposition branch) — a governed grouping with no PM approval refuses
on that check regardless of what any digest reads. Nothing in this module
writes `status`, `pm_utterance`, or `digest` into the plan; it never opens
the plan file for write at all. The one sanctioned way to write those fields
into a plan remains hand-authoring the `grouping_approvals` block after the
PM's own words are in hand — this module only supplies the one value in that
block that must never be typed from memory.

Reuse discipline: this module does NOT reimplement or re-derive
`compute_grouping_digest`'s hashing recipe — it calls the vendored function
in `coordinator_core.frontmatter.schema_validate` verbatim, over a rows list
this module builds by applying the caller's `cut` on top of the spine's
current rows. A second implementation of the digest recipe is precisely the
drift risk this whole workstream exists to close.

Fenced-block access reuses `coordinator_core.frontmatter.body_blocks.
locate_fenced_block` exclusively, same as `plan_tasks_render.py` and
`plan_tasks_mutate.py` — no fresh parser (see `plan_tasks_render.py`'s
docstring for the two documented silent-row-drop bugs reuse avoids).

Negative-spec:
  - Does NOT write the plan, does NOT call git, does NOT take the file lock
    (`locked_rmw`) — pure read + compute + print. This is the single most
    important constraint on this module: a version of this tool that wrote
    the approval block would rebuild exactly the self-certification the
    grouping-approval contract exists to end.
  - Does NOT validate `status`/`pm_utterance` or any other approval-block
    field — that is `plan_tasks_mutate._resolve`'s gate, checked at write
    time, not this module's concern at compute time.
  - Does NOT accept a bare grouping name without a `cut` and quietly compute
    over current membership — see the prospective-membership contract above.
    An empty `cut` is legal (a caller may want the digest for the CURRENT
    membership, e.g. to confirm an already-produced write matches an
    already-recorded approval) but is never the SILENT default; the caller
    passes an explicit empty list, not an omitted argument.
  - Does NOT guess at an unrecognized grouping, task id, or disposition —
    fails loud instead (see `_apply_cut` / `_handler` below). A digest
    computed over a set the caller had to guess at is worse than no digest.

Spec backlink: docs/plans/2026-07-29-grouping-approval-contract.md (DoE-claude) [DEAD-CITATION: plan file never committed to this repo]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.schema_validate import (
    _PLAN_TASKS_GROUPING_BY_DISPOSITION,
    compute_grouping_digest,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path

import yaml


class GroupingDigestError(ValueError):
    """Raised for every fail-loud validation failure in this module."""


def _plan_tasks_spine_rows(source: str) -> List[dict]:
    """The plan's current `## Tasks` spine rows, or raise `GroupingDigestError`.

    Deliberately fail-loud here (unlike `schema_validate._plan_tasks_spine_rows`,
    which returns `None` for a LINT to report in its own voice) — this module
    has no downstream lint to hand an absent/malformed spine to; a caller who
    asked for a digest and got one computed over nothing had no signal that
    anything went wrong.
    """
    result = locate_fenced_block(source)
    if result.status is LocateStatus.ABSENT:
        raise GroupingDigestError(
            "no '## Tasks' task-spine fenced block found in this plan"
        )
    if result.status is LocateStatus.MALFORMED:
        raise GroupingDigestError(
            "task spine is malformed (multiple 'yaml plan-tasks' fences, or a "
            "fence not directly under the '## Tasks' heading)"
        )

    try:
        rows = yaml.safe_load(result.body) or []
    except yaml.YAMLError as exc:
        raise GroupingDigestError(f"task spine does not parse as YAML: {exc}") from exc

    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise GroupingDigestError("task spine does not parse to a list of mapping rows")
    return rows


def _apply_cut(rows: List[dict], cut: Sequence[dict]) -> List[dict]:
    """Return `rows` with each `cut` entry's disposition applied, prospectively.

    `cut` is `[{"id": <task id>, "disposition": <target disposition>}, ...]` —
    the set of rows a pending `resolve` write is about to close, paired with
    the disposition each would land at. Mirrors the `prospective` list
    `plan_tasks_mutate._resolve` builds before calling `compute_grouping_digest`,
    verbatim in shape (row dict copied, only `disposition` overridden) so the
    two computations can never diverge on how an override is applied.

    Fails loud (rather than silently no-op'ing) on:
      - a `cut` entry naming a task id not present in `rows`;
      - a `cut` entry naming an unrecognized disposition (not one of the
        `_PLAN_TASKS_GROUPING_BY_DISPOSITION` keys) — a typo'd disposition
        would silently misclassify which grouping the row lands in.
    """
    rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    overrides: dict = {}
    for entry in cut:
        if not isinstance(entry, dict):
            raise GroupingDigestError(f"cut entry must be a dict: {entry!r}")
        task_id = entry.get("id")
        disposition = entry.get("disposition")
        if task_id not in rows_by_id:
            raise GroupingDigestError(f"cut: task id not found in spine: {task_id!r}")
        if disposition not in _PLAN_TASKS_GROUPING_BY_DISPOSITION:
            raise GroupingDigestError(
                f"cut: unrecognized disposition {disposition!r} for task {task_id!r} — "
                f"expected one of {sorted(_PLAN_TASKS_GROUPING_BY_DISPOSITION)}"
            )
        overrides[task_id] = disposition

    return [
        {**row, "disposition": overrides[row["id"]]}
        if isinstance(row, dict) and row.get("id") in overrides
        else row
        for row in rows
    ]


def compute_prospective_grouping_digest(
    source: str, grouping: str, cut: Sequence[dict]
) -> str:
    """The `sha256:<hex>` digest over `grouping`'s membership AFTER `cut` is
    applied to the plan's current spine.

    `source` is the plan file's full text (frontmatter + body). `grouping` is
    one of `do` / `defer` / `ruled_out`. `cut` is the prospective-close set —
    see `_apply_cut`'s docstring for its shape and fail-loud conditions.
    Raises `GroupingDigestError` on an absent/malformed spine or an
    unrecognized `cut` entry, and `ValueError` (from `compute_grouping_digest`
    itself) on an unrecognized `grouping`.
    """
    rows = _plan_tasks_spine_rows(source)
    prospective = _apply_cut(rows, cut)
    return compute_grouping_digest(prospective, grouping)


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


def _resolve_path(plan_path: str, worktree: Path) -> Path:
    """Resolve `plan_path` to an absolute Path, contained under docs/plans/.

    Same shape as `plan_tasks_mutate._resolve_path` (F0 containment), kept as
    its own small copy rather than an import from a MUTATE-verb module — this
    is a READ module and should not carry a dependency on the mutate module's
    lock/verb machinery just to reuse four lines of path math.
    """
    p = Path(plan_path)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "docs" / "plans"]
    resolved = contained_path(p, allowed_roots)
    if resolved is None:
        raise GroupingDigestError(f"plan_path escapes docs/plans/: {plan_path!r}")
    return resolved


def _err(message: str) -> dict:
    return {"exit_code": 1, "error": message}


@register_op("plan.tasks.grouping_digest")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'plan.tasks.grouping_digest' handler.

    READ-ONLY: never writes the plan, never takes the file lock. See the
    module docstring for the prospective-membership contract and the
    framing note (a digest is not an approval).

    Required params:
        plan_path (str)  — absolute or repo-relative path to the plan file
                            (must resolve under <worktree>/docs/plans/).
        grouping  (str)  — one of: do | defer | ruled_out.
        cut       (list) — `[{"id": ..., "disposition": ...}, ...]`, the
                            prospective-close set. Pass `[]` explicitly to
                            compute over the spine's CURRENT membership.

    Returns:
        {"exit_code": 0, "digest": "sha256:<hex>", "grouping": <str>,
         "member_count": <int>} on success.
        {"exit_code": 1, "error": <str>} on any fail-loud condition.
    """
    plan_path = (params.get("plan_path") or "").strip()
    grouping = (params.get("grouping") or "").strip()
    cut = params.get("cut")

    if not plan_path:
        return _err("plan.tasks.grouping_digest: 'plan_path' is required")
    if not grouping:
        return _err("plan.tasks.grouping_digest: 'grouping' is required (do|defer|ruled_out)")
    if cut is None or not isinstance(cut, list):
        return _err(
            "plan.tasks.grouping_digest: 'cut' is required (a list; pass [] to "
            "compute over the spine's current membership)"
        )
    if repo_root is None:
        return _err(
            "plan.tasks.grouping_digest: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    from coordinator_core.ops.fleet._common import main_worktree_root

    worktree = main_worktree_root(repo_root)

    try:
        path = _resolve_path(plan_path, worktree)
    except GroupingDigestError as exc:
        return _err(f"plan.tasks.grouping_digest: {exc}")

    if not path.is_file():
        return _err(f"plan.tasks.grouping_digest: plan not found: {plan_path}")

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(f"plan.tasks.grouping_digest: cannot read plan: {exc}")

    try:
        rows = _plan_tasks_spine_rows(source)
        prospective = _apply_cut(rows, cut)
        digest = compute_grouping_digest(prospective, grouping)
    except (GroupingDigestError, ValueError) as exc:
        return _err(f"plan.tasks.grouping_digest: {exc}")

    member_count = len(
        {
            row.get("id")
            for row in prospective
            if isinstance(row, dict)
            and _PLAN_TASKS_GROUPING_BY_DISPOSITION.get(row.get("disposition"), "do")
            == grouping
        }
    )

    return {
        "exit_code": 0,
        "digest": digest,
        "grouping": grouping,
        "member_count": member_count,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_git_root(cwd: Optional[str] = None) -> str:
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError as exc:
        raise GroupingDigestError(f"cannot resolve git repo root: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise GroupingDigestError(
            f"cannot resolve git repo root from {cwd or '.'}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _parse_cut_arg(raw: Optional[str]) -> List[dict]:
    """Parse a repeatable `--cut <id>:<disposition>` CLI argument into the
    `cut` list shape the handler expects. `None`/empty -> `[]` (current
    membership)."""
    if not raw:
        return []
    entries = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise GroupingDigestError(
                f"--cut entry must be '<id>:<disposition>', got {token!r}"
            )
        task_id, disposition = token.split(":", 1)
        entries.append({"id": task_id.strip(), "disposition": disposition.strip()})
    return entries


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan-tasks-grouping-digest",
        description=(
            "Print the sha256 membership digest for a plan task-spine grouping "
            "(do|defer|ruled_out), computed over the membership a prospective "
            "close (--cut) would produce. Read-only: never writes the plan. "
            "This is the value of a hash over a named set — it is not an "
            "approval, and having it does not mean a cut is approved."
        ),
    )
    parser.add_argument("--plan", required=True, help="path to the plan file")
    parser.add_argument(
        "--grouping", required=True, choices=["do", "defer", "ruled_out"], help="grouping name"
    )
    parser.add_argument(
        "--cut",
        default=None,
        help=(
            "comma-separated '<id>:<disposition>' pairs — the rows being closed and "
            "their target disposition. Omit (or pass an empty string) to compute over "
            "the spine's CURRENT membership instead of a prospective one."
        ),
    )
    parser.add_argument("--root", default=None, help="repo root (default: resolved via git)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        git_root = Path(args.root) if args.root else Path(_resolve_git_root())
    except GroupingDigestError as exc:
        print(f"plan-tasks-grouping-digest: {exc}", file=sys.stderr)
        return 2

    try:
        cut = _parse_cut_arg(args.cut)
    except GroupingDigestError as exc:
        print(f"plan-tasks-grouping-digest: {exc}", file=sys.stderr)
        return 2

    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = git_root / plan_path

    from coordinator_core.ops._path_guard import contained_path as _contained_path

    resolved = _contained_path(plan_path, [git_root / "docs" / "plans"])
    if resolved is None:
        print(
            f"plan-tasks-grouping-digest: plan_path escapes docs/plans/: {args.plan!r}",
            file=sys.stderr,
        )
        return 2
    if not resolved.is_file():
        print(f"plan-tasks-grouping-digest: plan not found: {resolved}", file=sys.stderr)
        return 1

    try:
        source = resolved.read_text(encoding="utf-8")
        digest = compute_prospective_grouping_digest(source, args.grouping, cut)
    except (GroupingDigestError, ValueError, OSError) as exc:
        print(f"plan-tasks-grouping-digest: {exc}", file=sys.stderr)
        return 1

    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
