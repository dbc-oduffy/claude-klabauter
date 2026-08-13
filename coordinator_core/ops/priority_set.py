"""
coordinator_core.ops.priority_set — the SOLE writer of a priority-ledger entry
(``priority.set`` op).

Purpose: writes one authored priority assignment to
``<coordinator-state-root --central>/priority-ledger/<target_id>.yaml`` — the
one-file-per-target ledger schema authored in coordinator-claude at
``coordinator/schemas/priority-ledger.schema.json`` (C1 of the same plan wave).
Every write goes through this op; there is no second writer of this directory.

Storage location: resolved via ``coordinator_core.state_root.coordinator_state_root(
central=True)`` (the native in-process peer of ``coordinator-state-root.py
--central``) — NEVER a literal ``state/priority-ledger/`` string. See that
module for the resolution ladder (meta-repo detection -> claude-klabauter central state).

Write mechanics: ``locked_rmw`` (coordinator_core.locked_write, atomic +
cross-process-locked, ``missing_ok=True`` — the target-id YAML need not already
exist). Every write stamps ``set_by`` / ``set_at`` (UTC ISO-8601, seconds
precision, "Z" suffix) / ``source``. ``source`` defaults to the literal
``"op"`` for every direct caller — the ``"external-intent"`` value (C7,
``coordinator_core.ops.priority_drain``) is opt-in via the ``source``/
``source_repo`` keyword-only params on ``set_priority()`` below, added
specifically so priority.drain can stamp externally-originated entries
WITHOUT opening a second write path into the ledger. The "source_repo null
iff source: op" invariant is stated only in the schema's prose
``description`` (no JSON Schema ``if/then`` conditional enforces it) —
``set_priority()`` is the SOLE enforcement of that invariant, Python-side
(see its ``ValueError`` guards below).

CLEARING WRITES THE ``none`` SENTINEL — this op MUST NOT delete the ledger
file. Deletion cannot express "explicitly not a priority" (a real authored
decision that terminates an upward inheritance walk) and would silently let a
consumer re-inherit an ancestor's priority instead. A caller that wants to
clear a priority calls this op with ``priority="none"``.

Post-write schema validation GATES the write (mirrors handoff_transition's
discipline): the proposed new YAML text is round-tripped through
``validate_frontmatter`` against the vendored priority-ledger schema before
``locked_rmw``'s mutate callback returns it — a schema violation raises
``MutateAbort`` inside the callback, which aborts the write cleanly (lock
released, nothing written) rather than landing a half-valid ledger entry.

Schema location: this op vendors a local copy of ``priority-ledger.schema.json``
under ``coordinator_core/frontmatter/schemas/`` (handoff_transition's
``handoff.schema.json`` precedent), pin-tracked in
``coordinator_core/frontmatter/tests/test_schema_validate.py::_QUEUE_SCHEMA_PINS``.
Coordinator-claude remains the schema's AUTHOR — this is a vendored copy, not a fork —
and drift from coordinator-claude's tree is caught by the pin's gating tamper-check plus
``schema_drift_watch``'s advisory probe, never by a live read of coordinator-claude's working
tree at call time. Resolution is a fixed path against the vendored directory
and cannot fail (the file ships in this repo); ``_resolve_schema_path`` still
returns ``Path``, never ``None`` — the ``Optional`` return type is kept only
so a defensive caller does not have to change shape, not because resolution
can actually fail in normal operation.

This op does NOT git-commit — consistent with every other claude-klabauter mutation op.

Registered as ``priority.set``, classified ``OpClass.MUTATING``
(coordinator_core/authz/classification.py), "none"-scoped
(coordinator_core/op_scopes.py — same class as ``ping`` / ``goal.set_kr_status``:
the ledger root is resolved centrally, not derived from a caller repo_root).

Spec backlink: coordinator-claude docs/plans/2026-07-26-priority-ledger.md § C3
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.state_root import coordinator_state_root

# Enum mirrors — kept as plain tuples (not imported from the schema) since the
# schema lives in a sibling repo and may not be resolvable at import time (see
# module docstring "Schema location"). These are the ratified, frozen field
# values from the plan's § C3 field list; any drift from the schema's actual
# enum is caught by post-write schema validation on a best-effort basis.
_TARGET_KINDS = ("handoff", "plan", "roadmap", "deliverable")
_PRIORITIES = ("urgent", "high", "medium", "low", "none")

# TRUST BOUNDARY — target_id becomes a FILENAME (ledger_dir / f"{target_id}.yaml"
# below), and priority.set is directly callable over JSON-RPC. Hardcoded rather
# than schema-loaded, mirroring priority_drain._TARGET_ID_PATTERN's discipline
# EXACTLY (same rationale, same non-skippable posture): schema validation
# (_apply_priority_set, below) is best-effort and only runs AFTER this module
# has already committed to a target_file path, so it must never be this path's
# sole defense against a traversal-shaped target_id — a pattern that only
# fires post-hoc, inside a callback that a corrupted/missing vendored schema
# file could skip, is not a trust boundary. This guard runs UNCONDITIONALLY,
# before any path interpolation, regardless of whether schema resolution
# succeeds, and must never become skippable. Mirrors the vendored
# priority-ledger.schema.json's target_id `pattern` exactly (see that
# schema's own description for the traversal shapes it rejects: '..', '/',
# '\\', leading '.', trailing '.').
_TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9_-]$|^[A-Za-z0-9]$")

# Vendored schema this op validates writes against. See module docstring
# "Schema location" — pin-tracked in test_schema_validate.py's
# _QUEUE_SCHEMA_PINS, re-vendored only via bin/claude-klabauter-revendor-schema.py.
_VENDORED_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "frontmatter" / "schemas" / "priority-ledger.schema.json"
)


def _utc_now_stamp() -> str:
    """UTC ISO-8601 timestamp, seconds precision, 'Z' suffix (set_at shape)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_schema_path() -> Optional[Path]:
    """Resolution of the vendored priority-ledger schema.

    Returns the fixed vendored path, or None (never raises) in the
    defensive case where the vendored file is somehow missing from this
    repo's own tree — see module docstring "Schema location". Unlike the
    pre-vendoring live-coordinator-claude-tree read, this is no longer an EXPECTED failure
    mode; it exists so a corrupted/deleted vendored copy still degrades to a
    skipped validation rather than crashing the write path outright.
    """
    if not _VENDORED_SCHEMA_PATH.is_file():
        return None
    return _VENDORED_SCHEMA_PATH


def _render_entry(
    target_id: str,
    target_kind: str,
    priority: str,
    set_by: str,
    set_at: str,
    note: str,
    source: str = "op",
    source_repo: Optional[str] = None,
) -> str:
    """Serialized via ``yaml.safe_dump`` — fixed key order preserved via
    ``sort_keys=False``.

    Review: code-reviewer — the previous hand-rolled ``"\\n".join(...)`` form
    did not escape embedded newlines in ``note``/``source_repo``. Since C7
    made both fields externally-reachable (example-cockpit-repo's priority-intent
    records, routed through priority_drain.py — "outside our review
    pipeline" per that module's own docstring), an embedded newline in either
    field let an attacker inject a second ``source:``/``source_repo:`` line;
    PyYAML's duplicate-key-last-wins resolution then let the forged line win
    on parse, silently erasing the ``source: external-intent`` attribution
    this module's bypass-detectability guarantee depends on. The
    "small, fully-controlled field set" rationale for hand-rolling (mirroring
    queue_append/goal_kr_status) stopped holding the moment those two fields
    became externally-supplied; ``safe_dump`` closes the class of bug, not
    just this instance.

    ``source``/``source_repo`` (C7, priority.drain): the only caller passing
    non-default values here is ``coordinator_core.ops.priority_drain``, which
    threads them through ``set_priority()`` below so the ledger keeps exactly
    ONE writer (this module) even for externally-originated entries — see
    that op's module docstring for why a second write path is not an option.
    """
    entry: dict = {
        "target_id": target_id,
        "target_kind": target_kind,
        "priority": priority,
        "set_by": set_by,
        "set_at": set_at,
        "source": source,
        "source_repo": source_repo,
    }
    if note:
        entry["note"] = note
    return yaml.safe_dump(entry, default_flow_style=False, sort_keys=False)


def _apply_priority_set(
    _old_text: str,
    target_id: str,
    target_kind: str,
    priority: str,
    set_by: str,
    note: str,
    source: str = "op",
    source_repo: Optional[str] = None,
) -> str:
    """Pure mutate step for locked_rmw: builds the full replacement ledger
    entry text (whole-document overwrite — the ledger schema is
    ``additionalProperties: false`` and this op is the SOLE writer, so there
    is no pre-existing sibling data to preserve across a rewrite) and gates
    it through schema validation before returning it.

    Raises MutateAbort (locked_rmw releases the lock, writes nothing) when the
    resolved schema rejects the proposed entry. Never raises when the schema
    itself could not be resolved — see _resolve_schema_path.
    """
    new_text = _render_entry(
        target_id=target_id,
        target_kind=target_kind,
        priority=priority,
        set_by=set_by,
        set_at=_utc_now_stamp(),
        note=note,
        source=source,
        source_repo=source_repo,
    )

    schema_path = _resolve_schema_path()
    if schema_path is not None:
        from coordinator_core.frontmatter.schema_validate import validate_frontmatter

        entry_dict = yaml.safe_load(new_text)
        errors = validate_frontmatter(entry_dict, schema_path)
        if errors:
            raise MutateAbort(
                f"priority.set: schema validation failed for target_id={target_id!r}: {errors}"
            )

    return new_text


_SOURCES = ("op", "external-intent")


def set_priority(
    target_id: str,
    target_kind: str,
    priority: str,
    *,
    set_by: str = "",
    note: str = "",
    timeout: float = 10.0,
    source: str = "op",
    source_repo: Optional[str] = None,
) -> dict:
    """Locked write of one target's priority-ledger entry.

    Parameters:
        target_id   — the prioritized target's identifier; also the ledger
                       filename stem (``<target_id>.yaml``).
        target_kind — one of "handoff" | "plan" | "roadmap" | "deliverable".
        priority    — one of "urgent" | "high" | "medium" | "low" | "none".
                      "none" is the EXPLICIT-CLEAR SENTINEL (see module
                      docstring) — this op never deletes the ledger file.
        set_by      — identifier of the session/agent/person setting this
                      priority. Optional; empty string permitted (schema does
                      not require it).
        note        — optional free-form note.
        timeout     — max seconds to wait for the cross-process lock.
        source      — "op" (default, every direct caller) or "external-intent"
                      (C7, priority.drain ONLY — never pass this from any
                      other caller; it exists so priority.drain can stamp
                      externally-originated entries while still routing
                      through this module's sole write path).
        source_repo — originating repo identifier; MUST be None when
                      source="op" (the default) and a non-empty string when
                      source="external-intent".

    Returns:
        {target_id, target_kind, priority, set_by, source} — the effective
        values written. (source_repo is NOT included, preserving the
        pre-C7 return shape for every "op"-sourced caller.)

    Raises:
        ValueError   — target_id/target_kind/priority missing or blank,
                       target_id fails the safe-filename-component pattern
                       (see _TARGET_ID_PATTERN — unconditional, independent
                       of schema resolution), target_kind/priority outside
                       the ratified enum, source outside {"op",
                       "external-intent"}, or a source/source_repo
                       combination that violates the schema's "null iff
                       source: op" invariant.
        MutateAbort  — the resolved schema rejected the proposed entry (only
                       possible when the schema WAS resolvable — see
                       _resolve_schema_path).
        LockTimeout  — the cross-process lock could not be acquired within
                       timeout.
    """
    target_id = (target_id or "").strip()
    if not target_id:
        raise ValueError("target_id is required")
    if not _TARGET_ID_PATTERN.match(target_id):
        raise ValueError(
            f"target_id {target_id!r} fails the safe-filename-component pattern "
            f"(traversal-shaped or otherwise unsafe values are rejected before "
            f"ever being used as a path component)"
        )
    target_kind = (target_kind or "").strip()
    if target_kind not in _TARGET_KINDS:
        raise ValueError(f"target_kind must be one of {_TARGET_KINDS}, got {target_kind!r}")
    priority = (priority or "").strip()
    if priority not in _PRIORITIES:
        raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}")
    set_by = (set_by or "").strip()
    note = (note or "").strip()
    source = (source or "").strip()
    if source not in _SOURCES:
        raise ValueError(f"source must be one of {_SOURCES}, got {source!r}")
    if source == "op" and source_repo is not None:
        raise ValueError("source_repo must be None when source='op'")
    if source == "external-intent" and not source_repo:
        raise ValueError("source_repo is required (non-empty) when source='external-intent'")

    central_root = coordinator_state_root(central=True)
    ledger_dir = Path(central_root) / "priority-ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    target_file = ledger_dir / f"{target_id}.yaml"

    locked_rmw(
        target_file,
        lambda old_text: _apply_priority_set(
            old_text, target_id, target_kind, priority, set_by, note,
            source=source, source_repo=source_repo,
        ),
        repo_root=ledger_dir,
        timeout=timeout,
        missing_ok=True,
    )

    return {
        "target_id": target_id,
        "target_kind": target_kind,
        "priority": priority,
        "set_by": set_by,
        "source": source,
    }


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("priority.set")
def _priority_set(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'priority.set' handler — locked priority-ledger write.

    MUTATING (writes one ``<central-state>/priority-ledger/<target_id>.yaml``
    entry under a cross-process flock). Delegates to ``set_priority()``.

    "none"-scoped (op_scopes.py): the ledger root is resolved centrally via
    ``coordinator_state_root(central=True)``, never from a caller-supplied
    ``repo_root`` — the injected ``repo_root`` argument (always None for a
    "none"-scoped op; see op_scopes.py::_OP_KEY_SCOPE) is unused.

    Required params:
        target_id   (str) — the prioritized target's identifier.
        target_kind (str) — "handoff" | "plan" | "roadmap" | "deliverable".
        priority    (str) — "urgent" | "high" | "medium" | "low" | "none".

    Optional params:
        set_by  (str) — identifier of the session/agent/person setting this.
        note    (str) — free-form note.
        timeout (float) — max seconds to wait for the lock. Default: 10.0.

    Returns: {target_id, target_kind, priority, set_by, source}.

    Raises:
        ValueError (propagated as JSON-RPC error) for missing/blank required
        params or a value outside the ratified enum.
    """
    return set_priority(
        params.get("target_id", ""),
        params.get("target_kind", ""),
        params.get("priority", ""),
        set_by=params.get("set_by", ""),
        note=params.get("note", ""),
        timeout=float(params.get("timeout", 10.0)),
    )
