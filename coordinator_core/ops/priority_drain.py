"""
coordinator_core.ops.priority_drain — priority.drain op: drains cross-repo
priority-intent records dropped by example-cockpit-repo into the priority-ledger.

Purpose: example-cockpit-repo's Tactical route is dual-tier (Electron + hosted
web) and every shell-out seam it owns is loopback-gated / local-tier-only, so
a shell-out write into the ledger would be durable except from the web — the
exact failure the cross-repo contract forbids. Cockpit instead DROPS a
per-record YAML file into
``<coordinator-state-root --central>/priority-intent-inbox/`` (never a
literal ``state/priority-intent-inbox/`` string — resolved the SAME way
``priority_set.py`` resolves its central root, via
``coordinator_core.state_root.coordinator_state_root(central=True)``), and
this op DRAINS that inbox: it is an ASK, not a write. The record schema is
authored in example-doctrine-repo and vendored locally at
``coordinator_core/frontmatter/schemas/priority-intent.schema.json``,
pin-tracked (structural cousin of ``lessons-outbox.schema.json`` — see that
schema's own description for why it is a cousin, not a full precedent: the
write direction is inverted, cockpit writes INTO our tree).

``priority.set`` (``coordinator_core.ops.priority_set``) remains the SOLE
writer of a priority-ledger entry. This op does NOT open a second write path
into ``<central-state>/priority-ledger/`` — every applied record goes
through ``priority_set.set_priority()``, which (as of C7) accepts optional
``source``/``source_repo`` keyword-only params precisely so this op can
stamp ``source: external-intent`` / ``source_repo: <requesting repo>`` on the
resulting entry while a single module still owns every ledger write.
Bypass therefore stays detectable: any ledger entry stamped
``source: external-intent`` was, by construction, cockpit-originated and
routed through this drain — never an in-process direct write.

TRUST BOUNDARY — a record arrives from a process outside our review
pipeline, and its ``target_id`` becomes a FILENAME under the ledger's
one-file-per-target design (``priority_set.py`` writes
``<target_id>.yaml``). ``_TARGET_ID_PATTERN`` below therefore mirrors the
vendored ``priority-intent.schema.json``'s ``target_id`` pattern EXACTLY,
hardcoded rather than schema-loaded — this guard runs UNCONDITIONALLY,
independent of whether schema validation succeeds, unlike the best-effort
SCHEMA validation layered on top of it (``_schema_validate``, mirrors
``priority_set._resolve_schema_path``'s tolerant-skip shape kept as
defense-in-depth — see "Schema resolution" below). A traversal-shaped
``target_id`` (``../../foo``, an absolute path, anything containing ``/`` or
``\\``, or a leading ``.``) is REJECTED before it is ever used to build a
path — see ``_own_validate``.

TARGET-KIND INFERENCE — the intent schema deliberately carries no
``target_kind`` field (cockpit does not know our ledger's kind taxonomy);
this op infers it from the SAME ``<kind>-<slug>`` prefix convention already
used fleet-wide for target_id values (see
``coordinator_core/ops/tests/test_priority_set.py``'s own fixtures:
``handoff-abc123``, ``plan-xyz``, ``roadmap-1``). A target_id whose prefix is
not one of ``handoff-`` / ``plan-`` / ``roadmap-`` / ``deliverable-`` names an
UNKNOWN target and is rejected (dangling), never guessed at or defaulted.

ORDERING — pending records are applied in ``sequence`` order (the intent
schema's producer-assigned monotonic integer), NEVER by file mtime — mtime is
worthless on a shared tree with concurrent writers and clock skew (ratified
rule, matches the schema's own description).

IDEMPOTENCY / REPLAY — adopts the lessons-outbox ``drained/`` subdirectory
convention (structural cousin, not full precedent — see schema description
above). A successfully-applied record is moved (``Path.rename``, same
filesystem — the inbox and its ``drained/``/``rejected/`` subdirectories are
siblings under the same central-state root) to
``priority-intent-inbox/drained/`` immediately after the ledger write
succeeds; a malformed or unknown-target record moves to
``priority-intent-inbox/rejected/``. Draining the same record twice therefore
yields exactly one ledger write and one provenance stamp: the second drain
call's directory scan simply never sees the file again (it is no longer
directly under ``priority-intent-inbox/``) — no candidate-id round-trip, no
dedup-by-content needed. A record is NEVER left in place after processing and
NEVER silently dropped: every candidate ends in exactly one of drained /
rejected / failed (a failed record is left where it is, retried on the next
drain).

HOSTILE / MALFORMED INPUT — beyond the target_id trust boundary above:
  - size limit (``_MAX_RECORD_BYTES``) enforced before the file is even read
    past a stat() call.
  - safe YAML load only (``yaml.safe_load``) — never a loader that can
    construct arbitrary Python objects.
  - malformed records (unparsable YAML, wrong shape, missing/invalid
    required fields) and records naming an unknown target are REPORTED as
    rejected with a reason (see ``rejected`` in the return envelope) — never
    silently dropped, never turned into a ledger entry.

CADENCE — scheduled on the session-init / boot-sweep cadence
(``coordinator_core.ops.session.boot_sweep``, mirroring the
``fleet.reap_unintegrated_findings`` precedent that module already composes),
so a drop that nobody remembers to drain still lands on the next session
boot. Also independently invocable as the ``priority.drain`` op for a PM who
wants it drained immediately.

Self-registration: importing this module calls
register_op("priority.drain", _priority_drain) as a side-effect.

Registered as ``priority.drain``, classified ``OpClass.MUTATING``
(coordinator_core/authz/classification.py), "none"-scoped
(coordinator_core/op_scopes.py) — same class as ``priority.set``: the inbox
and ledger roots are both resolved centrally, not derived from a caller repo_root.

Spec backlink: example-doctrine-repo docs/plans/2026-07-26-priority-ledger.md § C7
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import MutateAbort
from coordinator_core.ops import priority_set
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.scope import relocate_touched_path
from coordinator_core.state_root import coordinator_state_root

_LOG = logging.getLogger(__name__)

_INBOX_DIRNAME = "priority-intent-inbox"
_DRAINED_SUBDIR = "drained"
_REJECTED_SUBDIR = "rejected"

# Mirrors coordinator_core.ops.priority_set._PRIORITIES exactly (kept as a
# local tuple rather than imported, same enum-mirror rationale as that
# module's own docstring: the priority-intent schema lives in a sibling repo
# and this op's own baseline guard must not depend on it being resolvable).
_PRIORITIES = ("urgent", "high", "medium", "low", "none")

_KNOWN_KINDS = ("handoff", "plan", "roadmap", "deliverable")

# Mirrors coordinator/schemas/priority-intent.schema.json's target_id pattern
# EXACTLY (example-doctrine-repo) — same pattern as priority_set._TARGET_ID_PATTERN,
# including its excluded-trailing-dot final-char class (a bare `[A-Za-z0-9._-]*`
# would accept a trailing `.`, which the schema's own x-bump-note calls out as
# a Windows filename-aliasing hazard: Windows silently strips a trailing dot,
# so `target_id: "foo."` and `target_id: "foo"` would alias to the same
# on-disk file — a silent-collision hazard, not a traversal one). See module
# docstring "TRUST BOUNDARY" — this guard is NEVER skippable, unlike the
# best-effort schema validation below.
# Review: code-reviewer — was `^[A-Za-z0-9][A-Za-z0-9._-]*$`, whose
# unrestricted trailing-char class let a trailing `.` through where this
# module's own docstring claimed an EXACT mirror; tightened to match.
_TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9_-]$|^[A-Za-z0-9]$")

_KIND_PREFIX_RE = re.compile(r"^(" + "|".join(_KNOWN_KINDS) + r")-")

# Generous ceiling for a small per-record YAML intent file — a genuine
# priority-intent record is a handful of scalar fields.
_MAX_RECORD_BYTES = 16 * 1024

# Vendored schema this op validates records against. See module docstring
# "Schema resolution" below — pin-tracked in test_schema_validate.py's
# _QUEUE_SCHEMA_PINS, re-vendored only via bin/claude-klabauter-revendor-schema.py.
_VENDORED_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "frontmatter" / "schemas" / "priority-intent.schema.json"
)

_DEFAULT_SET_BY = "priority.drain"


# ---------------------------------------------------------------------------
# Schema resolution — lazy, best-effort (mirrors priority_set._resolve_schema_path)
# ---------------------------------------------------------------------------


def _resolve_schema_path() -> Optional[Path]:
    """Resolution of the vendored priority-intent schema.

    Returns the fixed vendored path, or None (never raises) in the
    defensive case where the vendored file is somehow missing from this
    repo's own tree. Schema validation then degrades to skipped, exactly
    like priority_set's own resolution. The hardcoded
    ``_TARGET_ID_PATTERN``/``_own_validate`` guards below remain the
    non-skippable trust boundary regardless of this resolution's outcome —
    unlike the pre-vendoring live-example-doctrine-repo-tree read, a missing vendored file is
    no longer an EXPECTED failure mode.
    """
    if not _VENDORED_SCHEMA_PATH.is_file():
        return None
    return _VENDORED_SCHEMA_PATH


# ---------------------------------------------------------------------------
# Inbox resolution
# ---------------------------------------------------------------------------


def _resolve_inbox_dir() -> Path:
    """<central-state-root>/priority-intent-inbox — same resolution ladder as
    priority_set.py's ledger root (coordinator_state_root(central=True)).
    Never a literal path.
    """
    central_root = coordinator_state_root(central=True)
    inbox = Path(central_root) / _INBOX_DIRNAME
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


# ---------------------------------------------------------------------------
# Target-kind inference (see module docstring "TARGET-KIND INFERENCE")
# ---------------------------------------------------------------------------


def _infer_target_kind(target_id: str) -> Optional[str]:
    match = _KIND_PREFIX_RE.match(target_id)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Record loading + validation
# ---------------------------------------------------------------------------


def _load_record(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    """Size-gated, safe-YAML-only load. Returns (parsed, error_reason) — never
    raises; every failure mode degrades to a reported rejection.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"stat failed: {exc}"
    if size > _MAX_RECORD_BYTES:
        return None, f"record exceeds size limit ({size} > {_MAX_RECORD_BYTES} bytes)"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"read failed: {exc}"
    try:
        parsed = yaml.safe_load(text)  # safe_load ONLY — never a loader that constructs objects
    except yaml.YAMLError as exc:
        return None, f"YAML parse error: {exc}"
    return parsed, None


def _own_validate(record: Any) -> Optional[str]:
    """Baseline structural validation, enforced UNCONDITIONALLY regardless of
    whether the priority-intent schema is resolvable (see module docstring
    "TRUST BOUNDARY"). Returns an error reason, or None when valid.
    """
    if not isinstance(record, dict):
        return "record is not a YAML mapping"

    target_id = record.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        return "target_id missing or not a non-empty string"
    if not _TARGET_ID_PATTERN.match(target_id):
        return (
            f"target_id {target_id!r} fails the safe-filename-component pattern "
            f"(traversal-shaped or otherwise unsafe values are rejected before "
            f"ever being used as a path component)"
        )

    priority = record.get("priority")
    if priority not in _PRIORITIES:
        return f"priority {priority!r} not in {_PRIORITIES}"

    sequence = record.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return f"sequence must be an integer, got {sequence!r}"

    # Review: code-reviewer — defense-in-depth backstop for the YAML
    # injection closed at the render layer (priority_set._render_entry now
    # routes through yaml.safe_dump, which escapes these correctly). Reject
    # embedded newlines here too, same discipline already applied to
    # target_id above — belt and braces, not the primary fix.
    note = record.get("note")
    if isinstance(note, str) and ("\n" in note or "\r" in note):
        return "note must not contain embedded newlines"
    requested_by = record.get("requested_by")
    if isinstance(requested_by, str) and ("\n" in requested_by or "\r" in requested_by):
        return "requested_by must not contain embedded newlines"

    return None


def _schema_validate(record: dict) -> Optional[str]:
    """Best-effort priority-intent schema validation — skipped (returns None)
    when the schema is not resolvable, mirroring priority_set's own
    tolerance. Never the sole trust boundary — _own_validate always runs
    first and is never skipped.
    """
    schema_path = _resolve_schema_path()
    if schema_path is None:
        return None
    from coordinator_core.frontmatter.schema_validate import validate_frontmatter

    errors = validate_frontmatter(record, schema_path)
    if errors:
        return f"schema validation failed: {errors}"
    return None


# ---------------------------------------------------------------------------
# Inbox scan + archival move
# ---------------------------------------------------------------------------


def _scan_inbox(inbox: Path) -> List[Path]:
    """Direct-children *.yaml files only — never recurses into drained/ or
    rejected/, so an already-processed record is never re-scanned.
    """
    if not inbox.is_dir():
        return []
    try:
        entries = list(inbox.iterdir())
    except OSError:
        return []
    return sorted(p for p in entries if p.is_file() and p.suffix == ".yaml")


def _move(path: Path, dest_dir: Path, session_id: str) -> Optional[Path]:
    """Move *path* into *dest_dir* (same filename by default). Disambiguates
    on the rare filename collision (e.g. a same-named record dropped twice)
    rather than clobbering an already-archived record.

    Routed through ``relocate_touched_path`` (plan
    ``docs/plans/2026-08-06-relocation-re-declares-the-touch-claim.md``, C5a)
    rather than a bare ``path.rename`` — *path* is a real inbox record inside
    the worktree (not a tempfile), so a T-claim this session recorded at the
    old location must be re-declared at the archival destination rather than
    left to strand; see ``relocate_touched_path``'s own docstring for the
    claim-before-move ordering contract. When *path* was never claimed (the
    overwhelmingly common case — most drains happen outside any touch-
    tracked session, e.g. the boot-sweep cadence), or the central-state root
    this inbox lives under falls outside the current worktree entirely,
    ``relocate_touched_path`` degrades to a plain move with no claim
    bookkeeping, exactly like the ``path.rename`` this replaces.

    Review: code-reviewer — this call omits ``cwd`` (defaults to None),
    unlike ``migrate_cross_repo_layout._move_one``'s explicit
    ``cwd=repo_root``. Confirmed safe rather than merely convenient: this
    op's inbox is resolved via ``coordinator_state_root(central=True)`` (see
    module docstring), which for the engine's own spawn-per-call process
    resolves to THIS repo's own ``state/`` — i.e. the process cwd already
    equals the repo root ``relocate_touched_path`` would otherwise be told
    explicitly, so an implicit None-cwd (interpreted internally as the
    process's actual cwd) and an explicit ``cwd=repo_root`` resolve
    identically here. Left implicit rather than threaded explicitly because
    this op, unlike migrate_cross_repo_layout, has no repo_root parameter to
    thread it from in the first place (it is "none"-scoped — see module
    docstring) — passing one in would mean introducing a new parameter for a
    call that is already correct, not closing a real inconsistency.

    Review: code-reviewer — the inbox scan-and-archive phase is not under any
    lock (only the ledger write itself is, via priority_set's locked_rmw), so
    two concurrent drain() calls can both discover the same candidate file
    before either archives it. The loser's move used to propagate a bare
    FileNotFoundError, which surfaced as an unhandled exception via the
    JSON-RPC handler and was silently swallowed (losing the whole remaining
    batch's results) via boot_sweep's blanket ``except Exception``. A
    vanished source means a peer drain already handled this record — return
    None (never raise) so the caller treats it as "handled elsewhere, skip"
    rather than a failure. Narrow catch (OSError, not Exception) so a
    genuine unrelated fault still propagates. ``relocate_touched_path``
    itself does not catch a ``shutil.move`` failure (see its own docstring),
    so this narrow catch still fires the same way it did around the bare
    ``rename``.

    Review: code-reviewer — TWO separate catches here do TWO different jobs;
    do not "simplify" them back into one. (1) The narrow ``except OSError``
    immediately below is the vanished-source race above — it must return
    None, never fall back to a retry, since the source is genuinely gone.
    (2) The broader ``except Exception`` further out degrades a non-OSError
    failure INSIDE ``relocate_touched_path``'s own claim-bookkeeping (a bug,
    not an operational fault) to the plain ``path.rename`` fallback, mirroring
    ``migrate_cross_repo_layout._move_one`` and
    ``coordinator_core/percolate/rewrite_basename.py::_do_rename``: a move
    this function is already committed to performing must not be taken down
    by a claim-bookkeeping nicety. Without this second catch, a bookkeeping
    bug would propagate out of ``_move`` uncaught and crash the whole
    ``drain()`` batch — unlike its two siblings, which both fail open here.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        stem, suffix = path.stem, path.suffix
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stem}.{n}{suffix}"
            n += 1

    def _plain_rename() -> Optional[Path]:
        try:
            path.rename(dest)
        except OSError:
            _LOG.info(
                "priority.drain: source %s vanished before archival move (likely "
                "handled by a concurrent drain) — skipping",
                path,
            )
            return None
        return dest

    if not session_id:
        # No resolvable session id (e.g. the boot-sweep cadence running
        # outside any touch-tracked session) — relocate_touched_path
        # requires a non-empty session_id (ValueError otherwise), and an
        # unresolvable session could never have written a touch-claim in
        # the first place, so there is nothing to re-declare. Plain
        # rename, matching this function's pre-C5a behavior exactly.
        return _plain_rename()

    try:
        relocate_touched_path(session_id, str(path), str(dest))
    except OSError:
        _LOG.info(
            "priority.drain: source %s vanished before archival move (likely "
            "handled by a concurrent drain) — skipping",
            path,
        )
        return None
    except Exception:
        _LOG.info(
            "priority.drain: relocate_touched_path claim-bookkeeping failed for "
            "%s (non-OSError); falling back to a plain rename (fail-open, "
            "matches migrate_cross_repo_layout._move_one and "
            "rewrite_basename._do_rename) — the move is not taken down by a "
            "claim-bookkeeping bug",
            path,
        )
        return _plain_rename()
    return dest


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


def drain(*, set_by: str = _DEFAULT_SET_BY) -> Dict[str, list]:
    """Drain every currently-pending priority-intent record.

    Valid records are applied through priority_set.set_priority() (the SOLE
    ledger writer — see module docstring) in ``sequence`` order, then moved
    to priority-intent-inbox/drained/. Malformed or unknown-target records
    are moved to priority-intent-inbox/rejected/ with a reason. A record that
    fails application (e.g. the resolved schema rejects the resulting ledger
    entry) is left in place in the inbox — it is retried, not lost, on the
    next drain call.

    Returns {"drained": [...], "rejected": [...], "failed": [...]}, each a
    list of dicts describing the outcome for one record.
    """
    inbox = _resolve_inbox_dir()
    candidates = _scan_inbox(inbox)
    # Resolved ONCE per drain() call (not per-file) — every archival move
    # below belongs to the same invoking session; see _move's docstring for
    # why an unresolvable ("") session id degrades to a plain rename.
    session_id = resolve_session_id()

    parsed: List[Tuple[Path, Optional[dict], Optional[str]]] = []
    for path in candidates:
        record, reason = _load_record(path)
        if reason is None:
            reason = _own_validate(record)
        if reason is None:
            reason = _schema_validate(record)
        parsed.append((path, record if reason is None else None, reason))

    valid = [(p, r) for p, r, reason in parsed if reason is None]
    invalid = [(p, reason) for p, r, reason in parsed if reason is not None]

    # ORDERING: sequence order, never mtime — see module docstring.
    valid.sort(key=lambda pr: pr[1]["sequence"])

    drained: List[dict] = []
    rejected: List[dict] = []
    failed: List[dict] = []

    for path, record in valid:
        target_id = record["target_id"]
        target_kind = _infer_target_kind(target_id)
        if target_kind is None:
            dest = _move(path, inbox / _REJECTED_SUBDIR, session_id)
            if dest is None:
                # Source vanished before this drain could archive it — a
                # concurrent drain already handled this record. Skip.
                continue
            rejected.append({
                "id": path.name,
                "target_id": target_id,
                "reason": (
                    f"unknown target — target_id does not resolve to a known "
                    f"target kind (expected one of {_KNOWN_KINDS} as a prefix)"
                ),
                "moved_to": str(dest),
            })
            continue

        source_repo = record.get("requested_by") or "unknown"
        note = str(record.get("note") or "").strip()

        try:
            result = priority_set.set_priority(
                target_id,
                target_kind,
                record["priority"],
                set_by=set_by,
                note=note,
                source="external-intent",
                source_repo=source_repo,
            )
        except (ValueError, MutateAbort) as exc:
            _LOG.error(
                "priority.drain: failed to apply record %s (target_id=%r): %s",
                path.name, target_id, exc,
            )
            failed.append({"id": path.name, "target_id": target_id, "reason": str(exc)})
            continue

        dest = _move(path, inbox / _DRAINED_SUBDIR, session_id)
        if dest is None:
            # Source vanished before this drain could archive it — a
            # concurrent drain already handled this record (the ledger write
            # above is idempotent-ish under locked_rmw either way). Skip.
            continue
        drained.append({
            "id": path.name,
            "target_id": target_id,
            "result": result,
            "moved_to": str(dest),
        })

    for path, reason in invalid:
        dest = _move(path, inbox / _REJECTED_SUBDIR, session_id)
        if dest is None:
            # Source vanished before this drain could archive it — a
            # concurrent drain already handled this record. Skip.
            continue
        rejected.append({"id": path.name, "reason": reason, "moved_to": str(dest)})

    return {"drained": drained, "rejected": rejected, "failed": failed}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("priority.drain")
def _priority_drain(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'priority.drain' handler — drains the priority-intent inbox.

    MUTATING (writes zero-or-more ``<central-state>/priority-ledger/*.yaml``
    entries via priority_set.set_priority(), and moves every processed
    record file within ``<central-state>/priority-intent-inbox/``).

    "none"-scoped (op_scopes.py): both the inbox and ledger roots are
    resolved centrally via ``coordinator_state_root(central=True)``, never
    from a caller-supplied ``repo_root`` (unused here, mirrors priority.set).

    Optional params:
        set_by (str) — identifier stamped as the ledger entry's set_by field.
            Default: "priority.drain".

    Returns: {"exit_code": 0 | 2, "drained": [...], "rejected": [...],
              "failed": [...]}. exit_code:2 iff any record failed application
              (DETERMINATE-PARTIAL — mirrors fleet.reap_unintegrated_findings'
              own exit_code convention); a nonempty "rejected" list does NOT
              by itself set exit_code:2 — a rejected record was handled
              correctly (reported + archived), not a failure of the sweep.
    """
    result = drain(set_by=params.get("set_by", "") or _DEFAULT_SET_BY)
    return {
        "exit_code": 2 if result["failed"] else 0,
        **result,
    }
