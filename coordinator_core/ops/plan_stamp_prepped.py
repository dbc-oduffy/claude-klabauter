"""
coordinator_core.ops.plan_stamp_prepped — JSON-RPC "plan.stamp_prepped" operation.

Purpose: the ONLY writer of the four-field mise-prep attest
(``mise_prepped_by``/``_at``/``_sha``/``_findings``). It refuses unless
``coordinator_core.roadmap.prep_gate`` returns PREPPED for the exact bytes it is
about to stamp, then writes all four fields in ONE ``locked_rmw`` mutate closure.

WHY AN ATTEST AND NOT A STATUS RUNG. Status is a position; a certification has to
survive the position moving. A certified plan then executes — the run advances it
to ``executing``, and ``blitz_land`` advanced it to ``approved`` before that — so
a ``mise-prepped`` status value would be erased by the first legitimate advance,
gone precisely during the run it authorised. This repo has ruled the same way
three times (``execution_authorized_*``, ``review_verified_*``,
``prime_exit_criterion``/``exit_criterion_met``): a certification that must
outlive a status change is a field beside ``status``, never a value in it.

WHY THE GATE RUNS INSIDE THE LOCK. The verdict and the recorded sha have to
describe the same document. Gating before the lock and stamping after it leaves a
window in which the body changes, and what lands is a certification of bytes the
bar never saw — the exact failure the sha exists to make detectable. The gate is
pure and spawn-free, so running it under the lock costs the lock nothing it would
not have paid anyway.

Wire params:
    plan (str, required) — the plan to certify, repo-relative or absolute. Must
                           resolve inside the main worktree.
    by   (str, optional) — the certifying session. Defaults to
                           ``attributable_session_id()``; refuses when neither
                           resolves, because an attest whose author is unknown
                           names nobody to ask.

Reply fields:
    {"plan": "docs/plans/....md", "stamped": bool, "outcome": str,
     "verdict": "PREPPED"|"NOT-PREPPED"|"REFUSED", "mise_prepped_sha": str|None,
     "mise_prepped_findings": [row_id, ...], "message": str,
     "classes": {...}, "stamp": {...}}

    `outcome` is one of "stamped", "already-certified", "refused". A refusal is a
    REPLY, not an exception: the per-class breakdown is what tells an author
    where the fix lands, and raising would collapse four routable findings into
    one string.

`mise_prepped_findings` is the spine row ids WITHHELD at stamp time — rows held
back by an ``external_gate`` carrying ``requires: landed-work``, which blocks its
own row while the plan still certifies. ``[]`` is a declared-empty and is the
point: it is the same distinction ``writes: []`` draws against an absent
``writes:``. It never carries a defect — a NOT-PREPPED plan is not stamped, and
``requires: commit-in-owner-repo`` refuses the whole plan — so a non-empty value
is a schedule fact, never a failure.

Negative-spec:
  - Does NOT stamp a plan the gate did not pass. There is no override parameter
    and no force flag; the only way past this op is to declare what the bar
    names.
  - Does NOT commit. The write lands in the worktree so the diff can be read
    before it becomes history, mirroring ``roadmap.blitz_land``.
  - Does NOT flip ``status`` or touch any other field. The attest accompanies no
    transition — that is why it is not in ``plan_status_transition``, whose every
    verb mutates ``status:`` and whose ``review_verified_*`` fields ride there
    only because they share a ``locked_rmw`` closure with the flip they
    accompany.
  - Does NOT spawn, and does NOT shell out for the sha.
    ``primitives.canonical_body_sha`` is pure Python and byte-identical to
    `git hash-object` over the plan body; a process creation costs 25.3ms to
    compute what sha1 already answers. Not ``blitz_land :: _git_blob_sha``, which
    hashes the WHOLE FILE and would report every plan stale the moment its own
    stamp landed.
  - Does NOT re-write an unchanged certification. A plan already CERTIFIED
    against its current body, whose recorded findings are the ones the gate
    recomputes now, returns the file's own bytes, which ``locked_rmw`` then
    skips writing — that is how idempotence is spelled here, and it keeps
    ``mise_prepped_at`` meaning "when this certification was made" rather than
    "when someone last ran the op". Findings that differ are a different
    certification and are re-stamped.
  - Does NOT repair a hand-written partial stamp in a shape it cannot express.
    A block-scalar or nested-block value under one of the four keys is refused by
    name; the four fields are written together or not at all, and silently
    flattening an indented value would orphan its continuation lines.
  - Does NOT do a full YAML re-emit. Frontmatter surgery goes through the shared
    text primitives, whose ``rebuild`` is byte-identical outside the mutated
    lines — the anti-clobber guarantee every other lifecycle mutator in this repo
    relies on.

Spec backlink: DoE-claude coordinator/docs/wiki/mise-prepped-attest.md
               .coordinator-local/memo-outbox/sent/mise-prepped-shape-ruling.md § 1, § 2
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # writes the four mise_prepped_* fields of the caller-named plan only

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.frontmatter.primitives import (
    _is_nested_block_key,
    insert_fm_field,
    insert_fm_field_raw,
    read_fm_field,
    rebuild,
    replace_fm_field,
    replace_fm_field_raw,
    serialize_yaml_scalar,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.roadmap.prep_gate import (
    CERTIFIED,
    PREPPED,
    STAMP_FIELDS,
    gate_plan,
    read_stamp,
)

# Generator-provenance: writes only the caller-named plan path, in place. The
# `MUTATES` declaration above names the shape rather than a fixed file because
# the target is data-dependent — the same treatment `plan_status_transition`
# gives its own `--plan`.
GENERATES: list = []

#: Anchor chain for the four inserted lines: each field anchors on the one before
#: it, and the first on `status`, so a fresh stamp lands as a contiguous quartet
#: directly under the field it is an attest beside. `insert_fm_field` falls back
#: to append-at-end when an anchor is absent, so a plan with no `status:` still
#: gets all four.
_ANCHORS = {
    "mise_prepped_by": "status",
    "mise_prepped_at": "mise_prepped_by",
    "mise_prepped_sha": "mise_prepped_at",
    "mise_prepped_findings": "mise_prepped_sha",
}


def _refuse_unwritable_shape(fm_text: str) -> None:
    """Refuse a hand-authored value this op cannot rewrite without corrupting it.

    Reapplies the two guards ``replace_fm_field`` applies to a scalar, for the
    array field that must route through ``replace_fm_field_raw`` (which applies
    none) — the same explicit reapplication ``handoff_author_fork.
    _stamp_fork_provenance`` makes at its own raw-replace call site.
    """
    for key in STAMP_FIELDS:
        current = read_fm_field(fm_text, key)
        if current is None:
            continue
        if current.startswith(">") or current.startswith("|"):
            raise MutateAbort(
                f"{key} holds a block-scalar value; rewriting it as one line would "
                f"truncate it. Delete the four mise_prepped_* lines and re-run."
            )
        if _is_nested_block_key(fm_text, key):
            raise MutateAbort(
                f"{key} holds an indented YAML block; rewriting only its key line would "
                f"orphan the continuation lines. Delete the four mise_prepped_* lines "
                f"and re-run."
            )


def _set_scalar(fm_text: str, key: str, value: str, *, numeric_quoting: bool = False) -> str:
    if read_fm_field(fm_text, key) is not None:
        return replace_fm_field(fm_text, key, value, numeric_quoting=numeric_quoting)
    return insert_fm_field(
        fm_text, key, value, after_key=_ANCHORS[key], numeric_quoting=numeric_quoting
    )


def _set_inline_array(fm_text: str, key: str, values: List[str]) -> str:
    """Write ``key: [a, b]`` — or ``key: []`` for the declared-empty.

    ``serialize_yaml_scalar`` per element so quoting matches every other field
    the primitives write; the enclosing ``[...]`` is pre-serialized text
    ``replace_fm_field``'s scalar serializer cannot produce, which is why this
    routes through the raw entry points rather than re-forking their regex.
    """
    serialized = "[" + ", ".join(serialize_yaml_scalar(v) for v in values) + "]" if values else "[]"
    if read_fm_field(fm_text, key) is not None:
        return replace_fm_field_raw(fm_text, key, serialized)
    return insert_fm_field_raw(fm_text, key, serialized, after_key=_ANCHORS[key])


def _resolve_plan(raw: str, worktree_root: Path) -> Path:
    """The plan path, resolved against the main worktree and contained by it.

    ``contained_path(candidate, [worktree_root])`` — ``queue_close``'s call site
    verbatim, with the whole worktree as the containment domain because a plan
    document has no single conventional subdirectory this op may assume.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = worktree_root / candidate
    if contained_path(candidate, [worktree_root]) is None:
        raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
    return candidate


@register_op("plan.stamp_prepped")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.stamp_prepped" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.stamp_prepped requires a resolved repo_root")

    raw_plan = params.get("plan")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError("plan must be a non-empty string naming one plan file")
    by = params.get("by")
    if by is not None and (not isinstance(by, str) or not by.strip()):
        raise ValueError("by must be a non-empty string when supplied")

    worktree_root = Path(main_worktree_root(repo_root))
    plan_path = _resolve_plan(raw_plan.strip(), worktree_root)
    try:
        rel = plan_path.relative_to(worktree_root).as_posix()
    except ValueError:  # pragma: no cover - contained_path already refused this
        rel = plan_path.as_posix()

    if by is None:
        from coordinator_core.session.core import attributable_session_id

        by = attributable_session_id()
    if not by:
        raise ValueError(
            "plan.stamp_prepped could not resolve a certifying session — pass by=<session>"
        )

    state: Dict[str, Any] = {}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {rel}")
        _refuse_unwritable_shape(split.fm_text)

        report = gate_plan(worktree_root, plan_path, text=old_text)
        state["report"] = report
        if report["verdict"] != PREPPED:
            raise MutateAbort(report["message"])

        stamp = read_stamp(old_text)
        state["stamp"] = stamp
        # CERTIFIED is a body verdict; the findings are the gate's, recomputed just
        # above. A stamp whose body is unchanged but whose recorded findings the gate
        # no longer produces — a bar fixed after the stamp landed — is re-stamped, or
        # no driver could ever correct it: every later run would read it as done.
        findings_drifted = sorted(stamp["findings"] or []) != sorted(report["withheld_rows"])
        if stamp["state"] == CERTIFIED and not findings_drifted:
            # Byte-identical return: locked_rmw skips the write entirely, so a
            # re-run neither churns the mtime nor moves `mise_prepped_at` off the
            # moment this body was actually certified.
            state["outcome"] = "already-certified"
            return old_text

        body_sha = stamp["body_sha"]
        if not body_sha:
            raise MutateAbort(f"could not compute a body sha for {rel}")
        findings = list(report["withheld_rows"])
        stamped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        fm_text = split.fm_text
        fm_text = _set_scalar(fm_text, "mise_prepped_by", by)
        fm_text = _set_scalar(fm_text, "mise_prepped_at", stamped_at)
        # numeric_quoting: an all-digit or `1958e194`-shaped sha is otherwise
        # YAML-coerced to a number and reads back as one.
        fm_text = _set_scalar(fm_text, "mise_prepped_sha", body_sha, numeric_quoting=True)
        fm_text = _set_inline_array(fm_text, "mise_prepped_findings", findings)

        state["outcome"] = "stamped"
        state["sha"] = body_sha
        state["at"] = stamped_at
        state["findings"] = findings
        return rebuild(split, fm_text)

    try:
        locked_rmw(plan_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        raise ValueError(f"no such plan: {rel}")
    except LockTimeout as exc:
        raise ValueError(f"timed out waiting for the file lock on {rel}: {exc}")
    except MutateAbort as exc:
        report = state.get("report")
        message = exc.args[0] if exc.args else "mutation aborted"
        return {
            "plan": rel,
            "stamped": False,
            "outcome": "refused",
            "verdict": report["verdict"] if report else None,
            "mise_prepped_sha": None,
            "mise_prepped_findings": [],
            "message": message,
            "classes": report["classes"] if report else {},
            "stamp": state.get("stamp"),
        }

    report = state["report"]
    already = state["outcome"] == "already-certified"
    return {
        "plan": rel,
        "stamped": not already,
        "outcome": state["outcome"],
        "verdict": report["verdict"],
        "mise_prepped_sha": state["stamp"]["recorded_sha"] if already else state["sha"],
        "mise_prepped_findings": (
            state["stamp"]["findings"] or [] if already else state["findings"]
        ),
        "message": report["message"],
        "classes": report["classes"],
        "stamp": read_stamp(plan_path.read_text(encoding="utf-8", errors="replace")),
    }
