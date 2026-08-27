"""
coordinator_core.ops.gate_liveness.reconcile — "gate_liveness.reconcile" JSON-RPC op.

Purpose: the WRITER half of the gate-closure-signal contract, downstream of
`gate_liveness.resolve` (C1, the reader/joiner) and `gate_liveness.
emit_discharge` (C4, the producer). Turns a `discharged` verdict into the
one clearing path the schema names — `cleared: true` — never on its own
initiative: `apply` defaults to False, and a dry run is the only surfacing
channel this plan ships (nothing here writes to the emit envelope).

Two modes, one param (`apply`, default False):

  - `apply: false` (the default) — proposes flips, writes nothing. Returns
    `{exit_code: 0, applied: false, proposed_flips: [...]}`. This is the
    failure-mode-avoidance default the chunk brief names: an automatic flip
    on inferred evidence is exactly the class of bug this whole plan exists
    to close, so a caller must opt into `apply: true` to write anything.

  - `apply: true` — writes ONLY `discharged` entries (never `holds`/
    `undetermined`, regardless of `apply` — see `_classify_entries`),
    through the same field-write primitives `plan_tasks_mutate._stamp` uses
    (`locate_fenced_block`, `_validate_all`, `_dump_rows`) under ONE
    `locked_rmw` transaction, so the whole batch is all-or-nothing per the
    module's F1 round-trip-fidelity/idempotency guarantees.

    `_stamp` itself is NOT called directly: `_stamp` assigns whole fields
    verbatim (`row[field] = value`), so reconcile passes the ENTIRE
    `external_gate` list per row — but it has no hook for the PRECONDITION
    this op requires (AC6). `locked_rmw`'s read-modify-write covers the
    spine write only; the read this op did to decide WHAT to flip (the
    `resolve`-equivalent scan, done before the lock is acquired) is a
    separate, unlocked read. At this box's 50-70 concurrent sessions, on
    the one field a human is expected to hand-edit, a peer's edit to a
    sibling `external_gate` entry landing between that scan and this op's
    own lock acquisition would be silently clobbered by `_stamp`'s blind
    whole-field overwrite. So this module runs its OWN mutate closure
    (reusing `_validate_all`/`_dump_rows`/`locate_fenced_block` — the same
    primitives `_stamp` uses, not a reimplementation of them) that RE-READS
    each targeted row's `external_gate` INSIDE the lock and aborts the
    WHOLE batch — a returned refusal naming the drifted entry, never an
    exception trace — if any targeted entry differs from what the pre-lock
    scan saw. `MutateAbort` inside the closure means `locked_rmw` writes
    nothing (F1), so an abort here is a true no-op, not a partial write.

Each flipped entry gets `cleared: true` plus an appended provenance line on
`closure_evidence` (a plain string field — see plan-tasks.schema.json
1.10.0) naming resolver, evidence, and date, so a later reader can tell a
machine flip from an author's assertion. `_dump_rows` re-serialises the
WHOLE fence on any write (inherited from `_stamp`'s own mechanics), but the
resulting DIFF is now confined to the rows actually changed.

CORRECTED 2026-08-21 — this paragraph previously read "the diff touches
every row, which is expected, not a defect to chase". That was wrong, and
the "not a defect to chase" clause was actively harmful: it told the next
reader to stop looking at a real defect. `_dump_rows` was flattening every
`body: |` literal block scalar into a double-quoted single line with `\n`
escapes, so a one-row mutation rewrote all N rows (measured: 297+/629- on a
17-row plan). Re-serialising the whole fence is inherent; producing a
whole-file diff was not. Fixed in `plan_tasks_mutate._PlanTasksDumper`,
which selects `style='|'` for any value containing a newline — this op
consumes that fix by importing `_dump_rows` directly, so it is inherited
here rather than reimplemented. See
`state/bug-backlog/2026-08-21-plan-tasks-mutate-flattens-every-block-scalar-in-the-spine.yaml`.

Refuses `holds` and `undetermined` regardless of `apply` — neither is ever
a flip candidate (mirrors `resolve`'s own never-`holds`-from-absence
posture). Refuses (whole call, `exit_code: 1`, zero writes) a batch in
which any `discharged` candidate entry lacks a citation (missing
`evidence`/`memo_path` on the matched `discharges` record) — a malformed
citation on a flip candidate is a data-integrity concern serious enough to
withhold the WHOLE call, not silently skip one entry and flip the rest.

ZERO process spawns — no `subprocess`, no git, per DR-344 §4, mirroring
`resolve`. Same five registration surfaces as C1 (`ops/__init__.py`,
`authz/classification.py`, `op_scopes.py`, `ops/_registry_map.py`, plus
this module itself).

Spec backlink: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C2

Negative-spec:
  - Does NOT call `_stamp` — see the module-level rationale above (no
    precondition hook).
  - Does NOT flip `holds`/`undetermined` verdicts under any `apply` value.
  - Does NOT partially apply a batch — one `locked_rmw` transaction, one
    precondition check across every targeted row, all-or-nothing.
  - Does NOT invent a citation — a flip candidate's provenance line quotes
    exactly the resolver name and evidence `resolve_closure_key` already
    produced; this module does not re-derive either.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.schema_validate import is_governed_plan, parse_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.gate_liveness.resolve import (
    VERDICT_DISCHARGED,
    VERDICT_HOLDS,
    VERDICT_UNDETERMINED,
    _resolve_closure_key,
    _scan_discharge_records,
)
from coordinator_core.ops.plan_tasks_mutate import _dump_rows, _validate_all
from coordinator_core.ops.plan_tasks_render import load_rows

_OP_NAME = "gate_liveness.reconcile"


class _PathNotContained(Exception):
    """Mirrors plan_tasks_mutate._PathNotContained — plan_path escapes docs/plans/."""


def _resolve_path(plan_path: str, worktree: Path) -> Path:
    """Resolve plan_path to an absolute Path, contained under docs/plans/.

    Identical containment rule to `plan_tasks_mutate._resolve_path` (F0) —
    reconcile writes through the same spine, so it is held to the same
    boundary.
    """
    p = Path(plan_path)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "docs" / "plans"]
    resolved = contained_path(p, allowed_roots)
    if resolved is None:
        raise _PathNotContained(f"plan_path escapes docs/plans/: {plan_path!r}")
    return resolved


def _err(message: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": message}


def _entry_citation(evidence: Any) -> Optional[dict]:
    """Return the well-formed citation triple from a `discharged` verdict's
    `evidence` dict, or None if it's missing a required sub-field.

    `evidence` here is `_resolve_closure_key`'s third return value —
    `{memo_path, evidence, landed_at}` — never re-derived, only checked for
    completeness before this module will act on it.
    """
    if not isinstance(evidence, dict):
        return None
    memo_path = evidence.get("memo_path")
    evidence_str = evidence.get("evidence")
    if not isinstance(memo_path, str) or not memo_path.strip():
        return None
    if not isinstance(evidence_str, str) or not evidence_str.strip():
        return None
    return evidence


def _closure_key_identity(closure_key: Any) -> Optional[tuple]:
    if not isinstance(closure_key, dict):
        return None
    kind = closure_key.get("kind")
    id_value = closure_key.get("id")
    if not isinstance(kind, str) or not isinstance(id_value, str):
        return None
    return (kind, id_value)


def _classify_entries(rows: list, records: list) -> tuple:
    """Walk every `external_gate` entry across `rows`, resolving each
    against `records` (the pre-lock `_scan_discharge_records` scan).

    Returns `(candidates, citation_errors)`:
      - `candidates`: list of `{row_id, closure_key, entry, resolver,
        citation}` dicts for entries that resolved `discharged`, are not
        already `cleared: true`, and carry a complete citation.
      - `citation_errors`: list of human-readable strings, one per
        `discharged` entry whose citation is incomplete — non-empty means
        the WHOLE call refuses (module docstring).

    Never includes a `holds`/`undetermined` entry in `candidates` — those
    verdicts are simply skipped, not refused.
    """
    candidates: list = []
    citation_errors: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        external_gate = row.get("external_gate")
        if not isinstance(external_gate, list):
            continue
        for entry in external_gate:
            if not isinstance(entry, dict):
                continue
            owner_repo = entry.get("owner_repo")
            if not isinstance(owner_repo, str) or not owner_repo.strip():
                continue
            closure_key = entry.get("closure_key")
            verdict, resolver, evidence, _reason = _resolve_closure_key(
                closure_key, owner_repo, records
            )
            if verdict in (VERDICT_HOLDS, VERDICT_UNDETERMINED):
                continue
            if verdict != VERDICT_DISCHARGED:
                continue
            if entry.get("cleared") is True:
                continue  # already flipped — idempotent no-op, not a candidate
            citation = _entry_citation(evidence)
            if citation is None:
                identity = _closure_key_identity(closure_key)
                citation_errors.append(
                    f"row {row_id!r} closure_key {identity!r}: discharged verdict "
                    "lacks a complete citation (memo_path/evidence) — refusing "
                    "the whole batch, no writes applied"
                )
                continue
            candidates.append(
                {
                    "row_id": row_id,
                    "closure_key": closure_key,
                    "entry": entry,
                    "resolver": resolver,
                    "citation": citation,
                }
            )
    return candidates, citation_errors


def _provenance_line(resolver: str, citation: dict, today: str) -> str:
    """Render the appended `closure_evidence` provenance line — names
    resolver, evidence, and date (module docstring), never re-derived
    elsewhere."""
    return (
        f"[gate_liveness.reconcile {today}] resolver={resolver} "
        f"memo={citation.get('memo_path')} evidence={citation.get('evidence')}"
    )


def _apply_flip(entry: dict, resolver: str, citation: dict, today: str) -> None:
    """Mutate one `external_gate` entry in place: set `cleared: true` and
    append the provenance line to `closure_evidence` (plain string field)."""
    entry["cleared"] = True
    line = _provenance_line(resolver, citation, today)
    existing = entry.get("closure_evidence")
    if isinstance(existing, str) and existing.strip():
        entry["closure_evidence"] = existing.rstrip() + "\n" + line
    else:
        entry["closure_evidence"] = line


def _proposed_flip_report(candidates: list, plan_path: Path) -> list:
    return [
        {
            "plan": str(plan_path),
            "row_id": c["row_id"],
            "closure_key": c["closure_key"],
            "resolver": c["resolver"],
            "citation": c["citation"],
        }
        for c in candidates
    ]


def reconcile_gate_liveness(
    plan_path_str: str, apply: bool, worktree: Path, repo_root: Path, today: str
) -> dict:
    """Core reconcile logic, shared by the op handler and tests.

    `plan_path_str` is resolved/contained exactly as `plan_tasks_mutate`'s
    verbs resolve it (F0). `today` is caller-injected (never `date.today()`
    called from inside this function) so tests control the stamped date
    deterministically — mirrors `emit_discharge`'s own `today` param shape.
    """
    try:
        path = _resolve_path(plan_path_str, worktree)
    except _PathNotContained as exc:
        return _err(f"{_OP_NAME}: {exc}")

    if not path.is_file():
        return _err(f"{_OP_NAME}: plan not found: {plan_path_str}")

    records = _scan_discharge_records(repo_root)

    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    loaded = load_rows(source)
    if loaded.status is not LocateStatus.LOCATED:
        return _err(f"{_OP_NAME}: task spine is absent or malformed — nothing to reconcile")

    candidates, citation_errors = _classify_entries(loaded.rows, records)
    if citation_errors:
        return _err(
            f"{_OP_NAME}: refusing batch — " + "; ".join(citation_errors)
        )

    if not apply:
        return {
            "exit_code": 0,
            "applied": False,
            "proposed_flips": _proposed_flip_report(candidates, path),
        }

    if not candidates:
        return {"exit_code": 0, "applied": True, "flipped": []}

    # Baseline snapshot for the precondition check — captured from the
    # SAME unlocked read `candidates` was derived from (module docstring).
    baseline_by_row: dict = {}
    for c in candidates:
        baseline_by_row.setdefault(c["row_id"], []).append(
            (_closure_key_identity(c["closure_key"]), dict(c["entry"]))
        )

    flip_identities = {
        (row_id, _closure_key_identity(c["closure_key"]))
        for c in candidates
        for row_id in (c["row_id"],)
    }
    resolver_and_citation_by_key: dict = {
        (c["row_id"], _closure_key_identity(c["closure_key"])): (c["resolver"], c["citation"])
        for c in candidates
    }

    _state: dict = {"applied": False, "flipped": []}

    def mutate(old_text: str) -> str:
        result = locate_fenced_block(old_text)
        if result.status is LocateStatus.MALFORMED:
            raise MutateAbort(
                f"{_OP_NAME}: task spine is malformed (multiple 'yaml plan-tasks' "
                "fences, or a fence not directly under the '## Tasks' heading)"
            )
        if result.status is LocateStatus.ABSENT:
            raise MutateAbort(f"{_OP_NAME}: task spine is absent — nothing to reconcile")

        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None
        # `_validate_all`'s own contract: `governed` is PLAN-scoped and must be
        # resolved by the caller from frontmatter. Letting it default False
        # validates a governed plan's touched rows against the LEGACY per-row
        # schema variant, which can accept a row `_stamp` would reject.
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False

        rows = yaml.safe_load(result.body) or []
        if not isinstance(rows, list):
            raise MutateAbort(f"{_OP_NAME}: task spine body is not a YAML list")

        rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

        flipped_ids: list = []
        touched_ids: set = set()
        for row_id, baseline_entries in baseline_by_row.items():
            row = rows_by_id.get(row_id)
            if row is None:
                raise MutateAbort(
                    f"{_OP_NAME}: precondition failed — row {row_id!r} no longer "
                    "present, aborting whole batch, no writes applied"
                )
            current_gate = row.get("external_gate")
            if not isinstance(current_gate, list):
                raise MutateAbort(
                    f"{_OP_NAME}: precondition failed — row {row_id!r} "
                    "external_gate is no longer a list, aborting whole batch, "
                    "no writes applied"
                )
            current_by_identity = {
                _closure_key_identity(e.get("closure_key")): e
                for e in current_gate
                if isinstance(e, dict)
            }
            for identity, baseline_entry in baseline_entries:
                current_entry = current_by_identity.get(identity)
                if current_entry is None or current_entry != baseline_entry:
                    raise MutateAbort(
                        f"{_OP_NAME}: precondition failed — row {row_id!r} "
                        f"external_gate entry {identity!r} drifted since it was "
                        "read; aborting whole batch, no writes applied"
                    )
                resolver, citation = resolver_and_citation_by_key[(row_id, identity)]
                _apply_flip(current_entry, resolver, citation, today)
                flipped_ids.append({"row_id": row_id, "closure_key": identity})
            touched_ids.add(row_id)

        try:
            _validate_all(
                rows,
                touched_ids=touched_ids,
                governed=governed,
                plan_created=plan_created,
            )
        except MutateAbort as exc:
            raise MutateAbort(f"{_OP_NAME}: {exc.args[0] if exc.args else exc}") from exc

        body_yaml = _dump_rows(rows)
        start, end = result.span
        new_text = old_text[:start] + body_yaml + old_text[end:]
        _state["applied"] = True
        _state["flipped"] = flipped_ids
        return new_text

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"{_OP_NAME}: plan not found: {plan_path_str}")
    except LockTimeout as exc:
        return _err(f"{_OP_NAME}: timed out waiting for file lock on {plan_path_str}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else f"{_OP_NAME}: mutation aborted")

    return {"exit_code": 0, "applied": _state["applied"], "flipped": _state["flipped"]}


@register_op(_OP_NAME)
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "gate_liveness.reconcile" handler.

    Params:
        plan_path (str)  — repo-relative or absolute path to the plan whose
                            spine carries the `external_gate` entries to
                            reconcile (contained under docs/plans/, F0).
        apply     (bool) — OPTIONAL, default False. False (the default):
                            dry run, proposes flips, writes nothing. True:
                            writes discharged entries through the
                            precondition-checked locked transaction (module
                            docstring).
        repo_root (str)  — OPTIONAL wire override of the injected
                            `repo_root` (worktree-scoped "show_top", same
                            resolution as `gate_liveness.resolve`).

    Returns `{exit_code: 0, applied, proposed_flips|flipped}` on success,
    `{exit_code: 1, applied: false, error}` on refusal or path/lock failure.
    """
    plan_path = params.get("plan_path")
    if not isinstance(plan_path, str) or not plan_path.strip():
        return _err(f"{_OP_NAME}: missing required param: plan_path")

    apply = params.get("apply", False)
    if not isinstance(apply, bool):
        return _err(f"{_OP_NAME}: 'apply' must be a bool if supplied")

    root_param = params.get("repo_root")
    if isinstance(root_param, str) and root_param.strip():
        resolved_root = Path(root_param.strip())
    elif repo_root is not None:
        resolved_root = Path(repo_root)
    else:
        return _err(f"{_OP_NAME}: repo_root could not be resolved (no wire param, no injected worktree root)")

    # "show_top" scope (op_scopes.py): the injected/wire repo_root IS the
    # worktree root already — never main_worktree_root(repo_root), which
    # expects a git COMMON dir (".git") as input (plan_tasks_mutate's
    # "common_dir"-scoped verbs do that derivation; this op does not need
    # it, mirroring gate_liveness.resolve's identical repo_root-as-worktree
    # usage).
    worktree = resolved_root
    today = date.today().isoformat()

    return reconcile_gate_liveness(plan_path.strip(), apply, worktree, resolved_root, today)
