"""
coordinator_core.ops.goal_append — goal-event appender (goal.append op).

Purpose: Port of: append-goal-event.sh (DoE b5a4192c, 2026-07-20). Appends a single goal-event JSON line to
the per-machine append-only JSONL log
``<central_state_root>/goals-log.<machine>.jsonl``.

A DISTINCT WRITER op — NOT folded into the P06 goals reader (sections/goals.py),
which is read-only. This op is the authoritative write path for goal events.

Goal-event schema. Required/always-present fields:
    goal_id              — an explicitly-supplied id (verbatim, when the caller provides
                           one) OR, when absent/empty/whitespace-only, the deterministic
                           12-hex-char prefix of sha1(content key) (fallback, unchanged
                           from the pre-existing behaviour). See append_goal()'s
                           `goal_id` parameter for the precedence rule and the shape
                           validated on an explicit supply.
    repo                 — meta-repo slug (D7a) or --repo override
    coordinator_root_path — coordinator root path (default ".")
    period               — one of: day | week | repo
    period_value         — period key value (e.g. "2026-07-04" for day, "2026-W27" for week)
    declared_by_machine  — hostname of the emitting machine
    declared_at          — ISO-8601 UTC timestamp
    text                 — goal text string
    status               — "active" by default at append time; overridable via the
                           optional `status` param (see GoalStatus, entities/goal.py)
                           to express supersession/completion/drop directly at append.

Optional passthrough fields (2026-07-13 close-weekly-goal-loop field map — the row
schema now includes these per the wire's D9 nullability split; see
cross-repo/archive/2026-07-13-claude-central-em-close-weekly-goal-loop-claude-klabauter-emit-engine.md
and archive/specs/2026-07/2026-07-13-claude-klabauter-emit-goal-wire-2130-projection.md):
    key_results_status   — absent-when-absent; written only when a non-empty list
    weekly_perceptible    — absent-when-absent; written only when a real bool
    parent_goal_id        — absent-in-row when no parent; the emitter projects the
                            absent case as present-as-null on the wire (D9)

Content-hash key: "<repo>|<coordinator_root_path>|<period>|<period_value>|<text>"
(goal identity — the optional passthrough fields above are metadata and are NEVER
included in this key).
Machine slug: lowercase, non-[a-z0-9] runs collapsed to '-', strip leading/trailing '-'.

MUTATING op: writes coordinator substrate ONLY (the per-machine goals-log JSONL shard
under central_state_root). NEVER writes into rag's relational store (dual-write ban,
DR-208 / tri-plane DD#1).

Registered as ``goal.append`` in ops/__init__.py and classified ``OpClass.MUTATING``
in authz/classification.py (same chunk, per plan § C6 / AC10).

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C6
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.emit.envelope import resolve_context
from coordinator_core.ops.fleet._common import main_worktree_root

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Generator-provenance declaration (C2, generator_provenance.py's AST reader).
# append_goal() writes central_state_root/goals-log.<machine>.jsonl -- a
# per-machine shard whose filename is data-dependent (machine hostname slug),
# so the write SET is not a fixed artifact list; MUTATES over GENERATES.
MUTATES = ["state/goals-log.*.jsonl"]

# Valid period values.
_VALID_PERIODS = frozenset({"day", "week", "repo"})

# Valid status values — derived from the contract's GoalStatus Literal
# (source of truth: coordinator_core/contract/cockpit_schema/entities/goal.py::GoalStatus)
# so this op cannot drift from the contract's permitted set.
#
# Computed lazily (first call to append_goal(), not module scope) — importing
# cockpit_schema.entities.goal here was pulling the ~40ms pydantic entity tree into
# every eager op-module load, including the read-only /pickup brief path that never
# calls append_goal(). Spec: docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
_VALID_STATUSES: Optional[frozenset] = None


def _valid_statuses() -> frozenset:
    global _VALID_STATUSES
    if _VALID_STATUSES is None:
        from coordinator_core.contract.cockpit_schema.entities.goal import GoalStatus

        _VALID_STATUSES = frozenset(GoalStatus.__args__)
    return _VALID_STATUSES

# Log filename template: per-machine append-only shard.
_LOG_NAME_TEMPLATE = "goals-log.{machine}.jsonl"

# Shape an explicitly-supplied goal_id must match: exactly 12 lowercase hex chars —
# the SAME shape _goal_id() emits (12-hex-char sha1 prefix), not a looser "any hex
# string" or a longer-sha-truncate-on-write acceptance. Chosen deliberately over
# accepting/truncating a longer (e.g. full 40-char) sha1: a truncate-on-accept
# policy would let two distinct explicit callers each pass a different full hash
# that happens to share a 12-char prefix and never notice they'd collided on
# write — silently smaller odds of catching a caller bug than requiring the exact
# emitted shape up front, which fails loud on anything but the wire-native form.
# Two producers minting the SAME logical goal (the motivating bug this feature
# fixes) are expected to agree on the full id string, not merely a prefix.
_GOAL_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# Cross-repo memo naming the upstream (DoE emit-goal-from-artifact.sh) caller bug that
# motivates the absolute->relative rewrite in _normalize_coordinator_root_path(). Cited
# in the observability breadcrumb emitted on the absolute-rewrite branch (Finding 2).
_PHANTOM_REPO_FK_MEMO = (
    "cross-repo/inbox/2026-07-21-example-retrieval-repo-em-goals-repo-fk-coordinator-root-split-latent.md"
)

# Windows drive-letter absolute form, e.g. "C:\Users\..." or "C:/Users/...".
_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# Git-for-Windows MSYS toplevel form, e.g. "/c/Users/...".
_MSYS_ABSOLUTE_RE = re.compile(r"^/[A-Za-z]/")


def _is_absolute_crp(crp: str) -> bool:
    """Return True if ``crp`` is absolute by ANY platform's convention.

    ``os.path.isabs`` alone is not airtight cross-platform: on a Windows engine,
    ``ntpath.isabs`` returns False for MSYS/POSIX-style absolute paths (verified:
    ``ntpath.isabs('/Users/x/repo') == False``), which would let such a value bypass
    both the normalizer and the append_goal() guard and reach disk verbatim — the
    phantom-repo_fk incident this module exists to fix, just on Windows instead of
    POSIX. This helper is the single absoluteness test both call sites route through
    so they cannot diverge.

    Catches: the native platform's ``os.path.isabs``, a Windows drive-letter form
    (``C:\\`` / ``C:/``), an MSYS form (``/c/...``), or any leading-slash form.
    """
    return bool(
        os.path.isabs(crp)
        or _WINDOWS_DRIVE_ABSOLUTE_RE.match(crp)
        or _MSYS_ABSOLUTE_RE.match(crp)
        or crp.startswith("/")
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _machine_slug(hostname: Optional[str] = None) -> str:
    """Return the hostname slug used in the log filename.

    Mirrors the bash oracle's machine-slug derivation exactly: lowercase, collapse
    every run of non-``[a-z0-9]`` characters to a single '-', then strip
    leading/trailing '-'.
    """
    raw = hostname if hostname is not None else socket.gethostname()
    raw = (raw or "unknown").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or "unknown"


def _goal_id(repo: str, coordinator_root_path: str, period: str, period_value: str, text: str) -> str:
    """Compute the deterministic 12-hex-char goal_id.

    Content key: "<repo>|<coordinator_root_path>|<period>|<period_value>|<text>".
    Goal-id is the first 12 hex chars of the SHA-1 digest of that key (UTF-8 encoded).
    Identical to the bash oracle's ``sha1_hex "$HASH_INPUT"`` → ``${FULL_HASH:0:12}``.
    """
    content_key = f"{repo}|{coordinator_root_path}|{period}|{period_value}|{text}"
    full_hash = hashlib.sha1(content_key.encode("utf-8")).hexdigest()
    return full_hash[:12]


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format (bash oracle: ``date -u +%FT%TZ``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_coordinator_root_path(crp: Optional[str], repo_root: Path) -> str:
    """Rewrite an absolute incoming coordinator_root_path to repo-root-relative.

    The contract (contract/cockpit_schema/entities/coordinator_root.py) declares
    coordinator_root_path as repo-root-relative — "." for a single-root repo, "subdir"
    for a monorepo sub-root. This is the sole write choke point that rewrites an
    absolute path to that shape, defending against callers (notably DoE's
    emit-goal-from-artifact.sh) that pass an absolute git-toplevel path instead — an
    absolute (machine-specific) path resolves the same logical repo to a phantom
    second repo_fk in rag's (owner, repo, coordinator_root_path) keying, causing
    duplicate/inflated goals.

    Relative inputs (the common case, e.g. ".", "subdir") pass through UNVALIDATED —
    this function does not check that a relative input actually resolves in-repo (a
    "../other"-shaped escape would pass through unchanged). That is deliberate: a
    relative escape is machine-independent, so it cannot spawn the phantom repo_fk
    this function exists to prevent (see the Staff Engineer review, Finding 1). Absolute inputs
    are resolved against repo_root and rewritten to the equivalent repo-relative path;
    an absolute path that resolves outside repo_root raises ValueError rather than
    writing machine-specific garbage to disk. Absoluteness is tested via
    ``_is_absolute_crp()`` (not bare ``os.path.isabs``) so MSYS/drive-letter forms on a
    Windows engine cannot bypass the rewrite (Finding 0).

    Spec backlink: cross-repo/inbox/2026-07-21-example-retrieval-repo-em-goals-repo-fk-coordinator-root-split-latent.md
    """
    crp = (crp or ".").strip() or "."
    if not _is_absolute_crp(crp):
        return crp
    rel = os.path.relpath(os.path.realpath(crp), os.path.realpath(str(repo_root)))
    if rel == os.curdir:
        rel = "."
    elif rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError(
            f"coordinator_root_path {crp!r} resolves outside repo_root {str(repo_root)!r} "
            "— refusing to write a machine-specific/out-of-repo path"
        )
    print(
        f"[goal.append] WARNING: coordinator_root_path {crp!r} was absolute — normalized to "
        f"{rel!r}. This indicates an upstream caller is emitting an absolute path where the "
        f"contract requires repo-root-relative; see {_PHANTOM_REPO_FK_MEMO}.",
        file=sys.stderr,
    )
    return rel


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_goal(
    period: str,
    period_value: str,
    text: str,
    *,
    repo: Optional[str] = None,
    coordinator_root_path: str = ".",
    hostname: Optional[str] = None,
    central_state_root: Optional[Path] = None,
    key_results_status: Optional[list] = None,
    weekly_perceptible: Optional[bool] = None,
    parent_goal_id: Optional[str] = None,
    status: Optional[str] = None,
    goal_id: Optional[str] = None,
) -> dict:
    """Append one goal-event row to the per-machine goals-log JSONL shard.

    Parameters:
        period              — one of: day | week | repo (required).
        period_value        — period key value (required).
        text                — goal text string (required).
        repo                — meta-repo slug override; if None, derived from context (D7a).
        coordinator_root_path — coordinator root path recorded in the row; default ".".
        hostname            — machine hostname override; defaults to socket.gethostname().
        central_state_root  — override for the JSONL shard directory; defaults to the
                              coordinator_state_root --central seam.
        key_results_status  — optional list of already-projected {id, text, kind, status}
                              per-KR dicts (2026-07-13 field map). Written to the row only
                              when it is a non-empty list; malformed shapes (non-list) are
                              quarantined (omitted, not raised) mirroring the emitter's
                              per-record posture. Absent-when-absent (D9).
        weekly_perceptible   — optional bool. Written to the row only when it is a real
                              bool; malformed shapes (non-bool, e.g. the string "true")
                              are quarantined (omitted, not raised), mirroring the
                              key_results_status list guard. Absent-when-absent (D9).
        parent_goal_id       — optional parent goal id string. Written to the row only
                              when truthy (a real parent). Absent-in-row when no parent —
                              the emitter projects the absent case as present-as-null on
                              the wire (D9); this writer never writes an explicit null.
        status               — optional status override. When None (the default), the
                              row gets "active" exactly as before this parameter existed
                              (non-breaking default). When provided, MUST be one of the
                              contract's GoalStatus values (active | done | superseded |
                              dropped — see entities/goal.py::GoalStatus); any other value
                              raises ValueError rather than silently coercing to "active",
                              since a silent coercion would write a wrong-but-plausible row.
        goal_id               — optional EXPLICIT goal identity override. Precedence:
                              a non-empty (post-`.strip()`) value WINS and is used
                              verbatim, skipping the content-hash derivation entirely;
                              absent, empty, or whitespace-only falls back to the
                              existing `_goal_id()` content-hash derivation, byte-
                              identical to pre-this-parameter behaviour (the
                              compatibility invariant every existing caller relies on).
                              This exists so two independent producers minting the
                              SAME logical goal can agree on one identity up front
                              instead of each deriving their own hash and duplicating
                              the goal in the fleet (the motivating cross-repo bug —
                              see cross-repo memo referenced in the module docstring
                              precedence note). A supplied value MUST match
                              `_GOAL_ID_RE` (exactly 12 lowercase hex chars, the same
                              shape `_goal_id()` emits) or this raises ValueError —
                              never silently coerced/truncated, since writing a
                              malformed identity into the fleet's append-only wire is
                              unrecoverable (append-only: no in-place fix later).

                              KNOWN CROSS-CUTOVER GAP (named, not fixed, by this
                              change — direction-class, outside this task's scope):
                              the read-side dedup key in
                              ops/emit/sections/goals.py::collect() only recomputes
                              the content-hash for a row that has NO goal_id at all;
                              a legacy (no-goal_id) row and a later row for the SAME
                              logical goal carrying a DIFFERENT explicit goal_id will
                              NOT unify via supersession — both survive as distinct
                              records. See
                              ops/emit/tests/test_goals_dedup.py::
                              test_legacy_content_hash_row_does_not_unify_with_later_explicit_goal_id_row
                              for the pinned current behaviour.

    Returns a summary dict:
        {log_file, machine, goal_id, row: {<appended row>}}.

    Raises:
        ValueError: if period or text or period_value or status is invalid.
    """
    # Validate required fields.
    period = period.strip() if period else ""
    if not period:
        raise ValueError("period is required (one of: day | week | repo)")
    if period not in _VALID_PERIODS:
        raise ValueError(f"period must be one of: day | week | repo (got: {period!r})")
    period_value = period_value.strip() if period_value else ""
    if not period_value:
        raise ValueError("period_value is required")
    text = text.strip() if text else ""
    if not text:
        raise ValueError("text is required")
    if status is not None and status not in _valid_statuses():
        raise ValueError(
            f"status must be one of: {' | '.join(sorted(_valid_statuses()))} (got: {status!r})"
        )
    goal_id = goal_id.strip() if goal_id else ""
    if goal_id and not _GOAL_ID_RE.match(goal_id):
        raise ValueError(
            f"goal_id must be exactly 12 lowercase hex characters (the same shape "
            f"_goal_id() derives) when explicitly supplied (got: {goal_id!r}); "
            "omit goal_id entirely to fall back to content-hash derivation"
        )
    if _is_absolute_crp(coordinator_root_path):
        raise ValueError(
            f"coordinator_root_path must be repo-root-relative (got absolute path: "
            f"{coordinator_root_path!r}); the goal.append handler normalizes absolute "
            "input before reaching this API — direct callers must pass a relative path "
            "(e.g. '.' or 'subdir')"
        )

    # Resolve run context for central_state_root and repo_name when not supplied.
    _ctx = None
    if central_state_root is None or repo is None:
        _ctx = resolve_context()

    if central_state_root is None:
        central_state_root = _ctx.central_state_root  # type: ignore[union-attr]
    if repo is None:
        repo = _ctx.repo_name  # type: ignore[union-attr]

    # Derive computed fields.
    machine_hostname = hostname if hostname is not None else socket.gethostname()
    declared_by_machine = machine_hostname
    declared_at = _utc_now()
    status = status if status is not None else "active"
    # Precedence: explicit non-empty goal_id (validated above) wins verbatim; else
    # derive exactly as before this parameter existed (compatibility invariant).
    goal_id = goal_id if goal_id else _goal_id(repo, coordinator_root_path, period, period_value, text)

    # Assemble the row.
    row: dict = {
        "goal_id": goal_id,
        "repo": repo,
        "coordinator_root_path": coordinator_root_path,
        "period": period,
        "period_value": period_value,
        "declared_by_machine": declared_by_machine,
        "declared_at": declared_at,
        "text": text,
        "status": status,
    }

    # Optional passthrough fields (2026-07-13 field map, D9 nullability split — NOT
    # included in the goal_id content key above; they are metadata, not identity).
    if isinstance(key_results_status, list) and key_results_status:
        row["key_results_status"] = key_results_status
    # Review: code-reviewer (Finding 3) — mirror the key_results_status list guard so a
    # malformed weekly_perceptible (e.g. the string "true" instead of the bool True) is
    # quarantined (omitted) rather than written to disk and passed through to the wire.
    if isinstance(weekly_perceptible, bool):
        row["weekly_perceptible"] = weekly_perceptible
    if parent_goal_id:
        row["parent_goal_id"] = parent_goal_id

    # Write to shard — per-machine, append-only, never rewrite.
    machine = _machine_slug(machine_hostname)
    log_file = Path(central_state_root) / _LOG_NAME_TEMPLATE.format(machine=machine)
    Path(central_state_root).mkdir(parents=True, exist_ok=True)

    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")

    return {
        "log_file": str(log_file),
        "machine": machine,
        "goal_id": goal_id,
        "row": row,
    }


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------

def _goal_append_batch(
    events: list,
    *,
    repo: Optional[str],
    coordinator_root_path: str,
    central_state_root,
) -> dict:
    """Append N goal events in one call — the batch half of `events` support.

    Each entry of ``events`` carries the SAME per-event fields a single-event
    call's params dict would (period/period_value/text plus the optional
    key_results_status/weekly_perceptible/parent_goal_id/status/goal_id);
    ``repo``/``coordinator_root_path`` are shared across the whole batch,
    already resolved/normalized by the caller (`_goal_append`) exactly as
    they are for a single-event call.

    A per-event failure (ValueError from `append_goal()`, or a malformed
    non-dict entry) does NOT abort the batch — every OTHER event still gets
    its append attempt, matching the caller's (append-goal-event.py's
    `--events-file` batch) actual need: an unrelated goal file's bad
    frontmatter should not block every other goal in the same run. Returns
    ``{"events": [{"ok": bool, "result"|"error": ...}, ...]}`` in input
    order — never raises for a per-event failure, only for a malformed
    ``events`` value itself (see `_goal_append`).
    """
    outcomes: list[dict] = []
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            outcomes.append({"ok": False, "error": f"events[{i}] is not an object"})
            continue
        try:
            result = append_goal(
                period=event.get("period", ""),
                period_value=event.get("period_value", ""),
                text=event.get("text", ""),
                repo=repo,
                coordinator_root_path=coordinator_root_path,
                central_state_root=central_state_root,
                key_results_status=event.get("key_results_status"),
                weekly_perceptible=event.get("weekly_perceptible"),
                parent_goal_id=event.get("parent_goal_id"),
                status=event.get("status"),
                goal_id=event.get("goal_id"),
            )
        except ValueError as exc:
            outcomes.append({"ok": False, "error": str(exc)})
            continue
        outcomes.append({"ok": True, "result": result})
    return {"events": outcomes}


@register_op("goal.append")
def _goal_append(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'goal.append' handler — append a goal event to the per-machine JSONL log.

    MUTATING (writes the per-machine goals-log JSONL shard under per-repo state).
    Delegates to ``append_goal()``.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating worktree.
    The handler derives the main-worktree root via main_worktree_root(repo_root), builds an
    EmitContext from it, then passes central_state_root and repo_name as EXPLICIT overrides to
    append_goal() — the internal param-less resolve_context() fallback inside append_goal()
    is NOT reached (AC2 / C4a wiring requirement).
    Fails loud when repo_root is None (AC5 — no silent fallback to meta-repo).

    Required params:
        period       (str) — one of: day | week | repo.
        period_value (str) — period key value.
        text         (str) — goal text string.

    Optional params:
        repo                  (str) — repo slug override (default: resolved from per-repo context).
        coordinator_root_path (str) — coordinator root path recorded in the row (default ".").
        key_results_status    (list) — per-KR {id, text, kind, status} dicts (2026-07-13 field
                                       map). Passed through to append_goal(); D9 absent-when-absent.
        weekly_perceptible    (bool) — passed through to append_goal(); D9 absent-when-absent.
        parent_goal_id        (str)  — passed through to append_goal(); D9 present-as-null on
                                       the emitted wire, absent-in-row here when no parent.
        status                (str) — optional status override, passed through to
                                       append_goal(); default "active" when omitted; must be
                                       one of the contract's GoalStatus values or append_goal()
                                       raises.
        goal_id               (str) — optional EXPLICIT goal identity override, passed
                                       through to append_goal(). Non-empty (post-strip) wins
                                       verbatim over content-hash derivation; absent, empty,
                                       or whitespace-only falls back to derivation unchanged.
                                       Must match 12-lowercase-hex-char shape or append_goal()
                                       raises. See append_goal()'s docstring for the full
                                       precedence rule and rationale.
        events                 (list) — BATCH FORM (2026-08-19 amplification-gate fix): when
                                       present, this call appends EVERY entry in `events`
                                       instead of the single event described by the fields
                                       above (period/text/etc. at the top level are ignored
                                       when `events` is present). Each entry is shaped like a
                                       single-event params dict (period/period_value/text plus
                                       the optional key_results_status/weekly_perceptible/
                                       parent_goal_id/status/goal_id). `repo`/
                                       `coordinator_root_path` stay TOP-LEVEL and apply to
                                       every event in the batch — the same values a caller
                                       making N separate single-event calls with the same
                                       repo/root would have supplied N times. Returns
                                       {"events": [{"ok": bool, "result"|"error": ...}, ...]}
                                       in input order instead of the single-event return
                                       shape below; a per-event ValueError is caught and
                                       reported in that event's outcome rather than aborting
                                       the batch (see `_goal_append_batch()`). This exists so
                                       ONE coordinator_core.invoke spawn can carry N goal
                                       writes — collapsing the per-item subprocess fan-out
                                       that used to live in append-goal-event.py's caller
                                       (emit-goal-from-artifact.py) down to a single process,
                                       not just moving it into this op's own dispatcher.

    Returns the append summary: {log_file, machine, goal_id, row: {<appended row>}} — or,
    when `events` was supplied, {"events": [...]} (see above).

    Raises:
        ValueError (propagated as JSON-RPC error) for missing or invalid required params,
        an invalid status value, or (batch form) a non-list `events`.

    Amendment: docs/plans/2026-07-07-per-repo-emission-cutover.md § C4a
    """
    if repo_root is None:
        raise ValueError(
            "goal.append requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope must be 'common_dir' and _origin_worktree must be "
            "present in the JSON-RPC envelope. No silent fallback to meta-repo (AC5)."
        )
    derived_root = main_worktree_root(repo_root)
    ctx = resolve_context(derived_root)
    repo = params.get("repo") or ctx.repo_name  # explicit override; internal fallback not reached
    coordinator_root_path = _normalize_coordinator_root_path(
        params.get("coordinator_root_path", "."), derived_root
    )

    events = params.get("events")
    if events is not None:
        if not isinstance(events, list):
            raise ValueError(f"events must be a list of event objects (got: {type(events).__name__})")
        return _goal_append_batch(
            events,
            repo=repo,
            coordinator_root_path=coordinator_root_path,
            central_state_root=ctx.central_state_root,
        )

    return append_goal(
        period=params.get("period", ""),
        period_value=params.get("period_value", ""),
        text=params.get("text", ""),
        repo=repo,
        coordinator_root_path=coordinator_root_path,
        central_state_root=ctx.central_state_root,  # explicit override; internal fallback not reached
        key_results_status=params.get("key_results_status"),
        weekly_perceptible=params.get("weekly_perceptible"),
        parent_goal_id=params.get("parent_goal_id"),
        status=params.get("status"),
        goal_id=params.get("goal_id"),
    )
