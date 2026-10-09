"""
coordinator_core.ops.plan_signoff — JSON-RPC "plan.signoff".

Purpose: the sanctioned writer of who signed off a plan's spine rows (`pm_approved` +
`signoff`) or a `grouping_approvals.<g>` block, PM-verified or delegated to the APM, or (a
grouping only, DR-399) given by a G-EM or Uhura.

Wire params:
    plan_path (str, required)  — plan path, repo-relative or absolute, under docs/plans/.
    target (dict, required)    — {"rows": [ids]} or {"grouping": name}.
    source (str, required)     — "pm" | "apm" | "g-em" | "uhura" (the last two: grouping only).
    pm_quote (str)             — source pm: the PM's verbatim words.
    apm_ruling, ruling_ref     — source apm: the ruling text and where it is recorded.
    approver, ruling, ruling_ref — source g-em or uhura: the session name, the ruling text and
                                 where it is recorded.
    digest (str, optional)     — grouping only: must equal a fresh recompute.
    on (str, optional)         — YYYY-MM-DD, default today (UTC).

Reply: `{"plan", "applied", "message", "targets"}`.

Negative-spec:
  - All-or-nothing across row ids, under locked_rmw; any refusal writes nothing.
  - A pm sign-off over an existing delegated one writes a countersign (the delegated record
    moves into `history`); a delegated sign-off over any prior sign-off is refused.
  - A delegated ruling naming an irreversible or external act is refused (stays the PM's).
  - Never commits; the caller's ceremony does.
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # pm_approved/signoff on spine rows; grouping_approvals.<g>
GENERATES: list = []

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
from coordinator_core.frontmatter.schema_validate import (
    _plan_tasks_row_grouping,
    compute_grouping_digest,
    format_validation_errors,
    is_governed_plan,
    parse_frontmatter,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.plan_tasks_mutate import (
    _parse_rows_or_abort,
    _patch_body,
    _validate_all,
)
from coordinator_core.ops.signoff_provenance import (
    DELEGATED_SOURCES,
    SOURCE_APM,
    SOURCE_GEM,
    SOURCE_PM,
    SOURCE_UHURA,
    apm_admissible,
    build_signoff,
    countersign,
)

#: The grouping block's `approver` per signoff source; schema_validate matches it case-blind.
_BLOCK_APPROVER = {SOURCE_PM: "PM", SOURCE_APM: "APM", SOURCE_GEM: "G-EM", SOURCE_UHURA: "Uhura"}
_DELEGATE_WORDS = {SOURCE_APM: "apm_ruling", SOURCE_GEM: "ruling", SOURCE_UHURA: "ruling"}
_NAMES = {SOURCE_PM: "the PM", SOURCE_APM: "the APM", SOURCE_GEM: "a G-EM", SOURCE_UHURA: "Uhura"}

_PLAN_SCHEMA = Path(__file__).resolve().parent.parent / "frontmatter" / "schemas" / "plan.schema.json"


def _fail(message: str) -> MutateAbort:
    return MutateAbort(f"plan.signoff: {message}")


def _new_record(prior: Any, source: str, words: str, ruling_ref: Optional[str], on: str,
                approver: Optional[str] = None) -> dict:
    """The record to write over `prior` (the target's current `signoff` dict or None)."""
    prior_source = prior.get("source") if isinstance(prior, dict) else None
    if prior_source == SOURCE_PM:
        raise _fail("already signed off by the PM; nothing to upgrade or overwrite")
    if prior_source in DELEGATED_SOURCES:
        if source != SOURCE_PM:
            raise _fail(f"already signed off by {_NAMES[prior_source]}")
        return countersign(prior, words, on)
    return build_signoff(source, words, ruling_ref, on, approver)


def _rewrite_grouping(fm_text: str, grouping: str, block: dict) -> str:
    """Replace the 2-space `grouping:` entry under `grouping_approvals:` with `block`."""
    lines = fm_text.split("\n")
    top = next((i for i, ln in enumerate(lines) if ln.startswith("grouping_approvals:")), None)
    if top is None:
        raise _fail("plan has no grouping_approvals")
    end = top + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#"))):
        end += 1
    start = next((i for i in range(top + 1, end) if lines[i].startswith(f"  {grouping}:")), None)
    if start is None:
        raise _fail(f"grouping_approvals has no {grouping!r} entry")
    stop = start + 1
    while stop < end and (not lines[stop].strip() or len(lines[stop]) - len(lines[stop].lstrip(" ")) > 2):
        stop += 1
    while stop > start + 1 and not lines[stop - 1].strip():
        stop -= 1
    dumped = yaml.safe_dump({grouping: block}, sort_keys=False, allow_unicode=True, width=10**6)
    new = ["  " + ln for ln in dumped.rstrip("\n").split("\n")]
    return "\n".join(lines[:start] + new + lines[stop:])


def _sign_rows(old_text: str, ids: list, source: str, words: str, ref: Optional[str], on: str) -> str:
    located = locate_fenced_block(old_text)
    if located.status is not LocateStatus.LOCATED:
        raise _fail("task spine is absent or malformed")
    plan_fm = parse_frontmatter(old_text).get("frontmatter")
    plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None
    governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False
    rows = _parse_rows_or_abort(located.body, "plan.signoff")
    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    original = [dict(r) if isinstance(r, dict) else r for r in rows]
    for rid in ids:
        row = by_id.get(rid)
        if row is None:
            raise _fail(f"task id not found: {rid!r}")
        try:
            record = _new_record(row.get("signoff"), source, words, ref, on)
        except ValueError as exc:
            raise _fail(f"row {rid!r}: {exc}") from exc
        except MutateAbort as exc:
            raise _fail(f"row {rid!r}: {str(exc).removeprefix('plan.signoff: ')}") from exc
        row["pm_approved"] = True
        row["signoff"] = record
    _validate_all(rows, governed=governed, touched_ids=set(ids), plan_created=plan_created)
    start, end = located.span
    body = _patch_body(located.body, original, rows)
    return old_text[:start] + body + old_text[end:]


def _sign_grouping(old_text: str, grouping: str, digest: Optional[str], source: str, words: str,
                   ref: Optional[str], on: str, approver: Optional[str]) -> str:
    split = split_frontmatter(old_text)
    fm = parse_frontmatter(old_text).get("frontmatter")
    if split is None or not isinstance(fm, dict):
        raise _fail("no parseable YAML frontmatter")
    blocks = fm.get("grouping_approvals")
    block = blocks.get(grouping) if isinstance(blocks, dict) else None
    if not isinstance(block, dict):
        raise _fail(f"grouping_approvals has no {grouping!r} entry")
    located = locate_fenced_block(old_text)
    if located.status is not LocateStatus.LOCATED:
        raise _fail("task spine is absent or malformed")
    rows = [r for r in _parse_rows_or_abort(located.body, "plan.signoff") if isinstance(r, dict)]
    if not any(_plan_tasks_row_grouping(r) == grouping for r in rows):
        raise _fail(f"grouping {grouping!r} covers no rows")
    fresh = compute_grouping_digest(rows, grouping)
    if digest is not None and digest != fresh:
        raise _fail(f"digest does not match the current membership of {grouping!r}")
    prior = block.get("signoff") if block.get("status") == "approved" else None
    try:
        record = _new_record(prior, source, words, ref, on, approver)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    block = dict(block)
    block.update(
        status="approved",
        approver=_BLOCK_APPROVER[source],
        approved_at=on,
        pm_utterance=words,
        digest=fresh,
        signoff=record,
    )
    new_fm = dict(fm)
    new_fm["grouping_approvals"] = {**blocks, grouping: block}
    errors = [
        e for e in validate_frontmatter(new_fm, _PLAN_SCHEMA)
        if str(e.get("field", "")).startswith(f"grouping_approvals.{grouping}")
    ]
    if errors:
        raise _fail(f"schema-invalid grouping {grouping!r}: {format_validation_errors(errors)}")
    return rebuild(split, _rewrite_grouping(split.fm_text, grouping, block))


@register_op("plan.signoff")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.signoff" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.signoff requires a resolved repo_root")
    raw_plan, target, source = (params.get(k) for k in ("plan_path", "target", "source"))
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError("plan_path must be a non-empty string")
    if source not in _BLOCK_APPROVER:
        raise ValueError("source must be 'pm', 'apm', 'g-em' or 'uhura'")
    if not isinstance(target, dict) or sum(k in target for k in ("rows", "grouping")) != 1:
        raise ValueError("target must be exactly one of {rows: [ids]} or {grouping: name}")
    ids = target.get("rows")
    grouping = target.get("grouping")
    if "rows" in target and not (isinstance(ids, list) and ids and all(isinstance(i, str) and i for i in ids)):
        raise ValueError("target.rows must be a non-empty list of row ids")
    if len(set(ids or [])) != len(ids or []):
        raise ValueError("target.rows has a duplicate id")
    if "grouping" in target and not (isinstance(grouping, str) and grouping.strip()):
        raise ValueError("target.grouping must be a non-empty string")
    digest = params.get("digest")
    if digest is not None and grouping is None:
        raise ValueError("digest applies to a grouping target only")
    on = params.get("on") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if source in (SOURCE_GEM, SOURCE_UHURA) and grouping is None:
        raise ValueError(f"a {source} sign-off applies to a grouping only (DR-399)")
    approver = params.get("approver") if source in (SOURCE_GEM, SOURCE_UHURA) else None
    if source in DELEGATED_SOURCES:
        key = _DELEGATE_WORDS[source]
        words, ref = params.get(key), params.get("ruling_ref")
        if not apm_admissible(words):
            raise ValueError(f"{key} is empty or names an irreversible or external act; that stays the PM's")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"a {source} sign-off requires ruling_ref")
    else:
        words, ref = params.get("pm_quote"), None
        if not isinstance(words, str) or not words.strip():
            raise ValueError("a pm sign-off requires pm_quote")
    build_signoff(source, words, ref, on, approver)

    worktree_root = Path(main_worktree_root(repo_root))
    plan_path = Path(raw_plan.strip())
    if not plan_path.is_absolute():
        plan_path = worktree_root / plan_path
    if contained_path(plan_path, [worktree_root / "docs" / "plans"]) is None:
        raise ValueError(f"plan escapes docs/plans/: {raw_plan!r}")
    rel = plan_path.resolve().relative_to(worktree_root.resolve()).as_posix()

    def mutate(old_text: str) -> str:
        if grouping is not None:
            return _sign_grouping(old_text, grouping.strip(), digest, source, words, ref, on, approver)
        return _sign_rows(old_text, ids, source, words, ref, on)

    try:
        before = plan_path.read_text(encoding="utf-8")
        after = locked_rmw(plan_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        raise ValueError(f"no such plan: {rel}")
    except LockTimeout as exc:
        raise ValueError(f"timed out waiting for the file lock on {rel}: {exc}")
    except MutateAbort as exc:
        raise ValueError(exc.args[0] if exc.args else "plan.signoff: mutation aborted")
    names = list(ids) if ids else [grouping.strip()]
    return {
        "plan": rel,
        "applied": after != before,
        "message": f"{source} sign-off recorded on {', '.join(names)}",
        "targets": names,
    }
