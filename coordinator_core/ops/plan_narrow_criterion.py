"""
coordinator_core.ops.plan_narrow_criterion — JSON-RPC "plan.narrow_criterion".

Purpose: the sanctioned writer of a plan's `prime_exit_criterion.statement` narrowing
backed by a PM-approved `spun_off` grouping, plus the pure predicates the mint and the
approval prompt share.

Wire params:
    plan (str, required)       — plan path, repo-relative or absolute, under docs/plans/.
    grouping (str, required)   — the grouping id; only "spun_off" narrows a criterion.
    statement (str, required)  — the new prime-criterion statement.

Writes `statement` and `narrowed_by: {grouping, digest, prior_statement, narrowed_at}`, then
commits the plan through `commit_authored_content`. Reply:
`{"plan", "changed", "committed", "message"}`.

Negative-spec:
  - Refuses (ValueError, nothing written) unless `grouping_approvals.<grouping>` is approved
    with a non-empty `pm_utterance` and a `digest` equal to a fresh recompute over the
    spine's current membership, that membership is non-empty, and every member row reads
    disposition `spun_off`.
  - Never grants approval and never edits `grouping_approvals`.
  - Frontmatter is outside `approval_body_sha`, so a narrowing never moves that hash.
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # prime_exit_criterion.statement / narrowed_by of the named plan
GENERATES: list = []

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony import git_native

NARROWING_GROUPING = "spun_off"
_MIN_MATCH = 12


def _plan_fm_and_rows(plan_text: str):
    import yaml

    from coordinator_core.frontmatter.schema_validate import parse_frontmatter
    from coordinator_core.ops.plan_tasks_grouping_digest import GroupingDigestError, _plan_tasks_spine_rows

    fm = parse_frontmatter(plan_text).get("frontmatter")
    if not isinstance(fm, dict):
        return None, None, "plan has no parseable frontmatter"
    try:
        rows = _plan_tasks_spine_rows(plan_text)
    except (GroupingDigestError, yaml.YAMLError) as exc:
        return fm, None, f"task spine unreadable: {exc}"
    return fm, rows, None


def grouping_approval_problem(fm: Dict[str, Any], rows: List[dict], grouping: str) -> Optional[str]:
    """Why `grouping` is not a valid PM approval over the spine's current membership, else None."""
    from coordinator_core.frontmatter.schema_validate import (
        _plan_tasks_row_disposition,
        _plan_tasks_row_grouping,
        compute_grouping_digest,
    )

    if grouping != NARROWING_GROUPING:
        return f"grouping {grouping!r} cannot narrow a criterion; only {NARROWING_GROUPING!r}"
    blocks = fm.get("grouping_approvals")
    block = blocks.get(grouping) if isinstance(blocks, dict) else None
    if not isinstance(block, dict) or block.get("status") != "approved":
        return f"grouping {grouping!r} is not approved on this plan"
    utterance = block.get("pm_utterance")
    if not isinstance(utterance, str) or not utterance.strip():
        return f"grouping {grouping!r} carries no pm_utterance"
    members = [r for r in rows if isinstance(r, dict) and _plan_tasks_row_grouping(r) == grouping]
    if not members:
        return f"grouping {grouping!r} covers no rows"
    if any(_plan_tasks_row_disposition(r) != "spun_off" for r in members):
        return f"a row in grouping {grouping!r} is not disposition spun_off"
    if block.get("digest") != compute_grouping_digest(rows, grouping):
        return f"grouping {grouping!r} digest does not match its current membership"
    return None


def narrowing_problem(plan_text: str) -> Optional[str]:
    """None when the plan carries a `narrowed_by` that validates against its approved
    grouping; otherwise the reason it does not (including "no narrowing recorded")."""
    fm, rows, err = _plan_fm_and_rows(plan_text)
    if err:
        return err
    if fm is None or rows is None:
        return "plan has no parseable frontmatter"
    prime = fm.get("prime_exit_criterion")
    nb = prime.get("narrowed_by") if isinstance(prime, dict) else None
    if not isinstance(nb, dict):
        return "plan carries no structured narrowed_by"
    for key in ("grouping", "digest", "prior_statement"):
        if not isinstance(nb.get(key), str) or not nb[key].strip():
            return f"narrowed_by.{key} is missing"
    problem = grouping_approval_problem(fm, rows, nb["grouping"])
    if problem:
        return problem
    if fm["grouping_approvals"][nb["grouping"]].get("digest") != nb["digest"]:
        return "narrowed_by.digest differs from the approved grouping digest"
    return None


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def criterion_waiver(plan_text: str, criterion: Dict[str, Any], judged_at: Optional[str]) -> Optional[str]:
    """The receipt line when a recorded `not_met` is the pre-narrowing verdict of a validly
    narrowed plan, else None. The verdict counts as pre-narrowing when it names the
    narrowed-away statement, or (carrying no statement) was recorded before `narrowed_at`."""
    if criterion.get("status") != "not_met" or narrowing_problem(plan_text) is not None:
        return None
    fm, _rows, _err = _plan_fm_and_rows(plan_text)
    if fm is None:
        return None
    nb = fm["prime_exit_criterion"]["narrowed_by"]
    current = fm["prime_exit_criterion"].get("statement") or ""
    judged = criterion.get("statement")
    if isinstance(judged, str) and judged.strip():
        if _norm(judged) != _norm(nb["prior_statement"]) or _norm(judged) == _norm(current):
            return None
    else:
        narrowed_at = nb.get("narrowed_at")
        if not (isinstance(judged_at, str) and isinstance(narrowed_at, str) and judged_at < narrowed_at):
            return None
    return f"criterion narrowed by {nb['grouping']}; judge verdict pre-dates the narrowing"


def narrow_call_hint(plan_text: str, resolved_ids: List[str]) -> Optional[str]:
    """The `plan.narrow_criterion` call to run when a just-approved spun_off row's
    `criterion`/`traces_to_brief` names the prime-criterion statement, else None."""
    fm, rows, err = _plan_fm_and_rows(plan_text)
    if err or fm is None or rows is None:
        return None
    prime = fm.get("prime_exit_criterion")
    statement = _norm(prime.get("statement") or "") if isinstance(prime, dict) else ""
    if len(statement) < _MIN_MATCH:
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in resolved_ids:
            continue
        for key in ("criterion", "traces_to_brief"):
            leg = _norm(row.get(key) or "")
            if len(leg) >= _MIN_MATCH and (leg in statement or statement in leg):
                return (
                    f"row {row['id']} names the prime criterion; narrow it: "
                    f"plan.narrow_criterion {{plan, grouping: {NARROWING_GROUPING}, statement: <narrowed text>}}"
                )
    return None


def _entry_span(lines: List[str], start: int) -> int:
    """Index one past the 2-space-indented `key:` entry starting at `start`."""
    j = start + 1
    while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip(" ")) > 2):
        j += 1
    return j


def _rewrite_prime(fm_text: str, statement: str, narrowed_by: Dict[str, str]) -> str:
    lines = fm_text.split("\n")
    try:
        top = next(i for i, ln in enumerate(lines) if ln.startswith("prime_exit_criterion:"))
    except StopIteration:
        raise MutateAbort("plan has no prime_exit_criterion")
    end = top + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(" ")):
        end += 1
    block = lines[top + 1:end]
    out: List[str] = []
    i = 0
    wrote = False
    while i < len(block):
        key = re.match(r"^  (statement|narrowed_by):", block[i])
        if not key:
            out.append(block[i])
            i += 1
            continue
        i = _entry_span(block, i)
        if key.group(1) == "statement":
            out.append(f"  statement: {json.dumps(statement, ensure_ascii=False)}")
            out.append("  narrowed_by:")
            out.extend(f"    {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in narrowed_by.items())
            wrote = True
    if not wrote:
        raise MutateAbort("prime_exit_criterion has no statement")
    return "\n".join(lines[:top + 1] + out + lines[end:])


@register_op("plan.narrow_criterion")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.narrow_criterion" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.narrow_criterion requires a resolved repo_root")
    raw_plan, grouping, statement = (params.get(k) for k in ("plan", "grouping", "statement"))
    for name, val in (("plan", raw_plan), ("grouping", grouping), ("statement", statement)):
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{name} must be a non-empty string")
    assert isinstance(raw_plan, str) and isinstance(grouping, str) and isinstance(statement, str)
    grouping, statement = grouping.strip(), " ".join(statement.split())

    worktree_root = Path(main_worktree_root(repo_root))
    plan_path = Path(raw_plan.strip())
    if not plan_path.is_absolute():
        plan_path = worktree_root / plan_path
    if contained_path(plan_path, [worktree_root / "docs" / "plans"]) is None:
        raise ValueError(f"plan escapes docs/plans/: {raw_plan!r}")
    rel = plan_path.resolve().relative_to(worktree_root.resolve()).as_posix()

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {rel}")
        fm, rows, err = _plan_fm_and_rows(old_text)
        if err or fm is None or rows is None:
            raise MutateAbort(err or "plan has no parseable frontmatter")
        problem = grouping_approval_problem(fm, rows, grouping)
        if problem:
            raise MutateAbort(f"refusing to narrow: {problem}")
        prime = fm.get("prime_exit_criterion")
        prior = prime.get("statement") if isinstance(prime, dict) else None
        if not isinstance(prior, str) or not prior.strip():
            raise MutateAbort("plan has no prime_exit_criterion.statement")
        if _norm(prior) == _norm(statement):
            return old_text
        narrowed_by = {
            "grouping": grouping,
            "digest": fm["grouping_approvals"][grouping]["digest"],
            "prior_statement": " ".join(prior.split()),
            "narrowed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        new_text = rebuild(split, _rewrite_prime(split.fm_text, statement, narrowed_by))
        if narrowing_problem(new_text) is not None:
            raise MutateAbort(f"narrowing did not round-trip: {narrowing_problem(new_text)}")
        return new_text

    try:
        old_on_disk = plan_path.read_text(encoding="utf-8")
        new_text = locked_rmw(plan_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        raise ValueError(f"no such plan: {rel}")
    except LockTimeout as exc:
        raise ValueError(f"timed out waiting for the file lock on {rel}: {exc}")
    except MutateAbort as exc:
        raise ValueError(exc.args[0] if exc.args else "mutation aborted")

    if new_text == old_on_disk:
        return {"plan": rel, "changed": [], "committed": False, "message": "statement already narrowed"}

    msg = f"plan: prime criterion narrowed by {grouping} grouping — {rel}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(msg)
        msg_path = fh.name
    try:
        res = git_native.commit_authored_content(rel, new_text, msg_path, worktree_root)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    if not res.ok:
        raise ValueError(f"narrowing written to {rel} but commit failed: {(res.stderr or '').strip()[:200]}")
    return {
        "plan": rel,
        "changed": ["prime_exit_criterion.statement", "prime_exit_criterion.narrowed_by"],
        "committed": True,
        "message": "criterion narrowed and committed",
    }
