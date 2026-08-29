"""coordinator_core.write_guards.guard_class_relay — detect a CLASS transition
between two versions of a guard module's source, without importing either.

C1 of docs/plans/2026-08-29-a-guard-class-flip-announces-itself.md: a pure
function taking the OLD and NEW source text of one guard module and reporting
whether its `CLASS` (`"hard-deny"` / `"advisory"`) flipped between them.

Reuses `engine._parse_guard_literals` — the same AST-literal source reader
`engine._cheap_guard_metadata` uses — rather than a second parser. This module
applies its OWN narrower CLASS-only check against `engine._VALID_CLASSES`: it
does not require MATCHERS to be present or valid to report a CLASS
transition, because a module whose MATCHERS are missing or invalid would
otherwise silently hide a real CLASS flip from this relay.

NEGATIVE SPEC (scoped to `detect_class_transition` only, not this module as a
whole — C3 of docs/plans/2026-08-29-a-guard-class-flip-announces-itself.md
adds `stage_class_transition_memo` below, which DOES compose and stage a memo
via in-process `memo.draft`/`memo.compose` op calls): `detect_class_transition`
itself performs no git call, no I/O, and no memo composition. It takes two
strings and returns a verdict. Where the two source versions come from
(working tree vs. a prior commit, a diff, etc.) is out of scope for that
function.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from coordinator_core.write_guards.engine import _VALID_CLASSES, _parse_guard_literals

# Shared uncovered-shape statement (C6 of docs/plans/2026-08-29-a-guard-class-
# flip-announces-itself.md) — the SSOT both the relay memo (C3) and the
# reference doc (docs/reference/guard-class-relay.md, C5) read verbatim, so
# the two surfaces cannot announce different coverage. A Python constant does
# not bind a Markdown file by itself; only the anti-drift test (C4/C6) that
# asserts this exact string appears in both surfaces enforces the match.
#
# States plainly what this relay does and does NOT see: it fires on a
# module-level CLASS flip in coordinator_core/write_guards/ only. It does
# NOT fire on an intra-module branch-contract change (the worked example is
# the C15 shape — a guard whose deny/advisory behaviour changed without its
# module-level CLASS literal changing), and it does NOT cover bash_guards or
# hooks.
UNCOVERED_SHAPE = (
    "This relay fires on a module-level CLASS flip in "
    "coordinator_core/write_guards/; it does NOT fire on an intra-module "
    "branch-contract change (the C15 shape), and it does NOT cover "
    "bash_guards or hooks."
)


def _class_only(source: Optional[str]) -> Optional[str]:
    """Return the guard module's literal `CLASS` value, or `None` when the
    source is missing, unparseable, non-literal, or lacks a valid CLASS.
    """
    if source is None:
        return None
    parsed = _parse_guard_literals(source)
    if parsed is None or not parsed["found_cls"]:
        return None
    cls_val = parsed["cls"]
    if cls_val not in _VALID_CLASSES:
        return None
    return cls_val


def detect_class_transition(
    old_source: Optional[str], new_source: Optional[str]
) -> Optional[Tuple[str, str]]:
    """Return `(old_class, new_class)` when a guard module's `CLASS` flipped
    between the two given source strings, else `None`.

    `None` is returned — never raised, never guessed — when either source is
    missing (a wholesale file add or delete is not a transition), when either
    side's source is unparseable or its `CLASS` is a non-literal expression,
    or when both sides resolve to the same valid class. `MATCHERS` presence
    or validity plays no part in this check.

    NEGATIVE SPEC: this function performs no git call, no I/O, and no memo
    composition — a pure string-in, verdict-out check (see module docstring).
    """
    old_cls = _class_only(old_source)
    new_cls = _class_only(new_source)
    if old_cls is None or new_cls is None:
        return None
    if old_cls == new_cls:
        return None
    return (old_cls, new_cls)


# ---------------------------------------------------------------------------
# Emission (C3) — compose and stage a memo announcing one detected transition.
#
# Deliberately calls the registered `memo.draft` / `memo.compose` op
# functions IN-PROCESS (plain Python import + call), never the
# `cross-repo-memo` CLI — a subprocess on the commit path is brightline-
# forbidden (claude-klabauter CLAUDE.md § The brightline). `detect_class_transition`
# above stays pure; this is the module's separate, additive emission surface.
# ---------------------------------------------------------------------------

import re as _re

#: Fixed receiver for every guard-class-relay memo (interface fact,
#: verified by running the op — see this chunk's dispatch brief).
_MEMO_TO = "doe-claude-em"

#: The claude-klabauter/DoE boundary this memo is scoped to — every guard-class-relay
#: memo names the same seam, since the relay itself always crosses it.
_MEMO_SEAM = "claude-klabauter-guard-semantics / DoE-hooks-and-tests boundary"

#: The general ruling governing guard CLASS reclassification
#: (docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md) —
#: cited in every emitted memo body as the ruling reference; a specific
#: transition may additionally be covered by its own module-level ruling
#: comment (see docs/reference/guard-class-relay.md § two hand-written
#: remembrance comments), which this generic mechanism does not attempt to
#: look up per-module.
_RULING_REFERENCE = (
    "DR-277 (docs/decisions/DR-277-guards-are-advisory-by-default-two-"
    "named.md) governs guard CLASS reclassification generally; a specific "
    "module may additionally carry its own hand-written ruling comment."
)


def _topic_for(module: str, sha: str) -> str:
    """`guard-class-<module-stem>-<short-sha>` — lowercase, hyphenated, only
    `[a-z0-9-]` (memo.draft's `_TOPIC_SLUG_RE` gate). Chosen so a second flip
    of the SAME guard in a DIFFERENT commit gets its own topic, while a
    re-run against the SAME commit collides on purpose (see
    `stage_class_transition_memo`'s idempotent-no-op handling).
    """
    stem = Path(module).stem
    slug = _re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return f"guard-class-{slug}-{sha[:7]}"


def stage_class_transition_memo(transition: dict, *, repo_root: Any) -> dict:
    """Compose and stage a memo announcing one detected guard CLASS
    transition, via in-process `memo.draft` + `memo.compose` op calls.

    `transition` is one entry from `commit_v2._guard_class_relay_step`'s
    `transitions` list: `{"module", "old_class", "new_class", "sha"}`.
    `repo_root` is the git common dir (same value `ceremony.commit_v2`'s own
    handler receives) — both `memo.draft` and `memo.compose` resolve the
    calling repo's worktree from it themselves.

    Never raises: every failure mode is caught and returned as a dict for
    the caller to fold into a NAMED `skips` entry — this is deliberately NOT
    "never raise" reinterpreted as "silently do nothing" (C2's "never raise"
    spec must not become indistinguishable from a quiet, broken relay).

    Returns `{"staged": bool, "topic": str, "reason": str | None}`.
    `"staged": True` covers two cases: a fresh draft was composed and
    staged, OR a draft already exists at this EXACT topic (same module +
    same sha — the idempotent no-op: re-detecting an already-announced
    transition on a re-run is not a new fact, and `reason` names the
    no-op explicitly rather than reading like a fresh emission). Any other
    failure (setup error, write error, compose error) returns
    `"staged": False` with `reason` naming what happened — never silently
    dropped.
    """
    from coordinator_core.ops.fleet.memo_compose import _memo_compose
    from coordinator_core.ops.fleet.memo_draft import _memo_draft

    module = transition["module"]
    old_class = transition["old_class"]
    new_class = transition["new_class"]
    sha = transition["sha"]
    topic = _topic_for(module, sha)

    summary = f"guard CLASS flip: {Path(module).stem} {old_class}->{new_class}"
    if len(summary) > 120:
        summary = summary[:119] + "…"

    draft_params = {
        "dry_run": False,
        "topic": topic,
        "to": _MEMO_TO,
        "title": f"guard CLASS flip: {module} ({old_class} -> {new_class})",
        "kind": "fyi",
        "summary": summary,
        "scoped_to": {"artifact": module, "sha": sha, "seam": _MEMO_SEAM},
    }
    try:
        draft_result = _memo_draft(draft_params, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 -- never raise, C3 negative spec
        return {
            "staged": False, "topic": topic,
            "reason": f"memo.draft raised: {exc!r}",
        }

    if draft_result.get("exit_code") == 1:
        return {
            "staged": False, "topic": topic,
            "reason": f"memo.draft setup error for topic {topic!r}: {draft_result}",
        }

    draft_failed = draft_result.get("failed") or []
    if draft_failed:
        first_reason = draft_failed[0].get("reason", "")
        if first_reason.startswith("collision"):
            # Idempotent no-op -- a draft at this exact topic already exists
            # (same module + same sha already announced). Not an error.
            return {
                "staged": True, "topic": topic,
                "reason": f"already staged (collision no-op): {first_reason}",
            }
        return {
            "staged": False, "topic": topic,
            "reason": f"memo.draft failed for topic {topic!r}: {first_reason}",
        }

    body = (
        f"Guard CLASS transition detected on `{module}`.\n\n"
        f"- old_class: {old_class}\n"
        f"- new_class: {new_class}\n"
        f"- sha: {sha}\n"
        f"- ruling: {_RULING_REFERENCE}\n\n"
        f"{UNCOVERED_SHAPE}\n"
    )
    compose_params = {"dry_run": False, "topic": topic, "body": body}
    try:
        compose_result = _memo_compose(compose_params, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 -- never raise, C3 negative spec
        return {
            "staged": False, "topic": topic,
            "reason": f"memo.compose raised: {exc!r}",
        }

    if compose_result.get("exit_code") not in (0, None) or compose_result.get("failed"):
        return {
            "staged": False, "topic": topic,
            "reason": f"memo.compose failed for topic {topic!r}: {compose_result}",
        }

    return {"staged": True, "topic": topic, "reason": None}
