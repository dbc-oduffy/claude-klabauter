"""
coordinator_core.ops.cutover_advance — the SOLE sanctioned route that writes
a cutover record's ``phase`` field.

Purpose: this is D4's other half. ``cutover.gate`` (``ops/cutover_gate.py``)
is COMPUTE_ONLY — it re-derives the consumer set and evaluates the two-way
AGREEMENT test, but writes nothing back to the record (see that module's
C4b docstring, point 3: "persisting it is cutover.advance's (C5, MUTATING)
job, not this read-only gate's"). This module is that job: it calls
``cutover.gate`` INTERNALLY, refuses the phase bump outright on anything
short of a clean PASS, and — only on PASS — writes the next ``phase`` value
plus the gate's own ``derivation_history_entry`` to the record.

No ungated advance path exists (D4): the ONLY way this module ever writes
``phase:`` is by first obtaining a fresh PASS from ``cutover.gate`` in the
same call — there is no parameter, flag, or code path that skips the gate
and writes the bump directly. Combined with
``write_guards/block_cutover_phase_hand_edit.py`` (C4d, hard-denies a direct
agent Write/Edit/MultiEdit of the ``phase:`` line) and the operator-facing
forwarder ``coordinator/bin/cutover-cli advance`` (C7, the only CLI surface
that calls this op), the phase field has exactly one writer in the whole
system: this handler, gated.

Refusal is design-as-offers, never a bare denial (chunk C5 spec): on
anything short of PASS, the returned ``notes`` name the SPECIFIC unconfirmed
consumer ids (derived-but-not-confirmed) and, for each, the exact
``cutover-cli confirm-consumer`` invocation that would confirm it — plus,
verbatim, whichever of ``cutover.gate``'s own diagnostic notes explain the
non-PASS verdict (empty derivation, signal-2 re-verification failure,
foreign-repo fail-closed, narrowing). An operator reading a REFUSE always
has a next action, never just a "no".

Phase sequence (mirrors ``coordinator/schemas/cutover.schema.json``'s
``phase`` enum order exactly — the schema is the source of truth for the
sequence, this tuple is a same-order restatement, not an independent
authority): ``reader-widen -> dual-write -> retiring -> retired``. Each
call advances exactly one step; ``retired`` is terminal (no further step
exists) and a call against a record already at ``retired`` is a setup
error, not a REFUSE (there is nothing for the gate to have agreed or
disagreed with).

Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § C5, D1, D4

Negative-spec:
  - Does NOT re-implement any leg of ``cutover.gate``'s two-way AGREEMENT
    test. Every REFUSE/INDETERMINATE reason this module surfaces beyond the
    unconfirmed-ids list is ``cutover.gate``'s own ``notes``, forwarded
    verbatim — this module adds context, it never second-guesses the gate's
    verdict.
  - Does NOT accept a caller-supplied target phase. The next phase is
    computed solely from the record's current ``phase`` and the fixed
    sequence above — an operator cannot skip a phase or jump backward
    through this op.
  - Does NOT write anything when the gate's verdict is not PASS
    (``exit_code != 0``) — INDETERMINATE and REFUSE both leave the record
    byte-for-byte unchanged.
  - Does NOT call ``cutover.gate``'s handler function directly by import.
    It resolves the op through ``coordinator_core.ipc.get_op_handler``
    (the same in-process op-invocation shape as
    ``ops/ceremony/tail_ops.py::run_coverage_gate`` and this module's own
    sibling ``_reverify_probe_op_key`` in ``cutover_gate.py``) so the call
    goes through the SAME path any other caller of ``cutover.gate`` would
    use — there is no private back door into the gate's logic.

DR-208 five-question affirmation (MUTATING; citing ``_cutover_advance``):
  1. Writes, deletes, or reorders any state file, queue, or git object?    Yes.
     On a clean gate PASS, rewrites the cutover record's frontmatter in
     place: bumps ``phase`` to the next sequence value and appends the
     gate's ``derivation_history_entry`` to ``derivation_history``. This is
     the intended, sole write surface this op exists to provide (D4) — no
     write occurs on any other verdict.
  2. Writes into rag's relational store?                                   No.
  3. Opens any file for write (including sentinel creation)?               Yes.
     The cutover record markdown file itself, via
     ``coordinator_core.frontmatter.primitives.rebuild`` + a single
     ``Path.write_text`` — no sentinel, no tempfile, no other file touched.
  4. Mutates shared mutable state outside its own module?                  No.
     The only external state mutated is the one record file named by the
     caller; the in-process call to ``cutover.gate`` is read-only on its
     own account (COMPUTE_ONLY, affirmed in that module).
  5. Persistent state changes observable across process boundaries?        Yes.
     The rewritten record file is the intended, sole persistent effect —
     exactly the "no operator ever types frontmatter" property this op
     exists to provide.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from coordinator_core.frontmatter.primitives import rebuild, split_frontmatter
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

#: Phase sequence — same order as coordinator/schemas/cutover.schema.json's
#: `phase` enum. The schema is the authority; this is a same-order restatement
#: for the engine's own use, not an independent source of truth.
PHASE_SEQUENCE: tuple[str, ...] = ("reader-widen", "dual-write", "retiring", "retired")


def _reply(verdict_line: str, notes: list[str], exit_code: int, **extra: Any) -> dict:
    """Build the coverage_gate-shaped verdict envelope (verdict_line/notes/exit_code) —
    mirrors `cutover_gate.py::_gate_reply` so both ops share one wire shape
    (`cutover-cli`'s `_run_gate_shaped_op` renders either identically)."""
    reply: dict[str, Any] = {"verdict_line": verdict_line, "notes": notes, "exit_code": exit_code}
    reply.update(extra)
    return reply


def _read_frontmatter(record_path: Path) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Read a cutover record's frontmatter, returning (fm, fm_text, error).

    Distinct from `cutover_gate.py::_read_record`, which returns only the
    parsed frontmatter dict — this op also needs the raw frontmatter text
    (via `split_frontmatter`) to `rebuild()` the file on write, so it keeps
    its own copy of the split rather than re-deriving it a second time from
    the dict alone.
    """
    try:
        text = record_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, None, f"cannot read {record_path}: {exc}"
    split = split_frontmatter(text)
    if split is None:
        return None, None, f"{record_path} has no parseable frontmatter"
    try:
        fm = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError as exc:
        return None, None, f"{record_path} frontmatter is not valid YAML: {exc}"
    if not isinstance(fm, dict):
        return None, None, f"{record_path} frontmatter is not a mapping"
    return fm, split.fm_text, None


def _unconfirmed_consumer_notes(
    derived_ids: list[str],
    confirmed_consumers: list[Any],
) -> list[str]:
    """Design-as-offers refusal detail: name every derived-but-not-confirmed
    consumer id, and the exact `cutover-cli confirm-consumer` invocation that
    would confirm it (chunk C5 spec: "NAME the unconfirmed consumers and NAME
    what would confirm each one. Never a bare denial.").
    """
    confirmed_ids = {
        str(entry.get("id", ""))
        for entry in confirmed_consumers
        if isinstance(entry, Mapping)
    }
    unconfirmed = sorted(set(derived_ids) - confirmed_ids)
    if not unconfirmed:
        return []
    notes: list[str] = [
        f"cutover.advance: {len(unconfirmed)} derived consumer(s) are not yet in "
        f"confirmed_consumers: {unconfirmed}"
    ]
    for consumer_id in unconfirmed:
        notes.append(
            f"cutover.advance: to confirm {consumer_id!r}, run `cutover-cli "
            f"confirm-consumer <record> --id {consumer_id} --verified-by-kind "
            "<test-node-id|probe-op-key|commit-sha> --verified-by-ref <ref>` "
            "once you have evidence this consumer handles the new form."
        )
    return notes


@register_op("cutover.advance")
async def _cutover_advance(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'cutover.advance' handler — the ONLY writer of a cutover
    record's `phase` field (D4). Calls `cutover.gate` internally; writes the
    next-phase bump plus the gate's own `derivation_history_entry` ONLY on a
    clean PASS. Refuses (never writes) on REFUSE/INDETERMINATE/setup-error,
    naming the unconfirmed consumers and how to confirm each one.

    Params:
        record (str, required) — path to the cutover record markdown file,
            absolute or relative to the resolved coordinator-claude worktree. Same
            contract as `cutover.gate`'s `record` param — forwarded to it
            verbatim.

    Returns (coverage_gate-shaped verdict envelope, same shape as
    `cutover.gate`):
        {
            "verdict_line": str,   # "surface=... phase=... VERDICT=..." —
                # phase reflects the NEW phase only on VERDICT=ADVANCED;
                # every other verdict reports the CURRENT (unchanged) phase.
            "notes":        list,  # cutover.gate's own notes, forwarded
                # verbatim, PLUS (on non-PASS) the design-as-offers
                # unconfirmed-consumer detail this handler adds.
            "exit_code":    int,   # 0 ADVANCED, 2 REFUSE/INDETERMINATE (HALT),
                # 1 setup error (bad params, unreadable record, terminal phase).
        }

    Negative-spec:
      - Does NOT write when `cutover.gate`'s own `exit_code != 0` — REFUSE
        and INDETERMINATE both leave the record on disk unchanged.
      - Does NOT accept a caller-supplied target phase — see module
        docstring negative-spec.
      - Does NOT bypass `cutover.gate` under any parameter or condition —
        there is no flag that skips the internal gate call.
    """
    if repo_root is None:
        return _reply(
            "",
            ["cutover.advance: repo_root is required (common_dir scope) — no repo context supplied"],
            1,
        )
    worktree = main_worktree_root(repo_root)

    raw_record = str(params.get("record") or "").strip()
    if not raw_record:
        return _reply("", ["cutover.advance: 'record' param is required"], 1)

    candidate = Path(raw_record)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    resolved = contained_path(candidate, [worktree])
    if resolved is None or not resolved.is_file():
        return _reply(
            "",
            [f"cutover.advance: record path {raw_record!r} not found or escapes {worktree}"],
            1,
        )

    fm, fm_text, err = _read_frontmatter(resolved)
    if err is not None or fm is None or fm_text is None:
        return _reply("", [f"cutover.advance: {err}"], 1)

    current_phase = str(fm.get("phase", ""))
    surface = str(fm.get("surface", ""))
    if current_phase not in PHASE_SEQUENCE:
        return _reply(
            "",
            [
                f"cutover.advance: record phase {current_phase!r} is not one of "
                f"{PHASE_SEQUENCE} — cannot compute a next phase"
            ],
            1,
        )
    idx = PHASE_SEQUENCE.index(current_phase)
    if idx == len(PHASE_SEQUENCE) - 1:
        return _reply(
            "",
            [
                f"cutover.advance: record is already at the terminal phase "
                f"{current_phase!r} ({PHASE_SEQUENCE}) — nothing to advance"
            ],
            1,
        )
    next_phase = PHASE_SEQUENCE[idx + 1]

    gate_handler = get_op_handler("cutover.gate")
    if gate_handler is None:
        return _reply(
            "",
            [
                "cutover.advance: 'cutover.gate' op is not registered — the advance is "
                "refused because the gate it must pass through is unavailable, never "
                "because the gate was skipped (D4: no ungated advance path exists)"
            ],
            1,
        )
    gate_result = await gate_handler({"record": str(resolved)}, repo_root=repo_root)

    gate_notes = list(gate_result.get("notes") or [])
    gate_exit_code = gate_result.get("exit_code", 1)
    derivation_entry = gate_result.get("derivation_history_entry") or {}
    derived_ids = list(derivation_entry.get("derived_ids") or [])

    header = f"surface={surface!r} phase={current_phase!r}"

    if gate_exit_code != 0:
        confirmed_consumers = fm.get("confirmed_consumers")
        if not isinstance(confirmed_consumers, list):
            confirmed_consumers = []
        notes = list(gate_notes)
        notes.extend(_unconfirmed_consumer_notes(derived_ids, confirmed_consumers))
        notes.append(
            f"cutover.advance: REFUSED the advance to {next_phase!r} — cutover.gate did "
            "not return PASS (see the notes above for exactly why, and this op's own "
            "notes for exactly which consumer(s) to confirm)."
        )
        gate_verdict_line = str(gate_result.get("verdict_line") or "")
        verdict_tag = (
            gate_verdict_line.split("VERDICT=", 1)[-1].split()[0]
            if "VERDICT=" in gate_verdict_line
            else "SETUP-ERROR"
        )
        exit_code = gate_exit_code if gate_exit_code in (1, 2) else 2
        return _reply(f"{header} VERDICT={verdict_tag}", notes, exit_code)

    # Review: code-reviewer — every sibling lifecycle-mutation verb
    # (_consume/_ship/_close/_repark/_supersede/_unconsume in
    # handoff_transition.py) routes its write through locked_rmw for
    # cross-process serialisation; this op previously wrote via a bare
    # write_text with no lock, so two concurrent cutover.advance calls could
    # both pass the gate and the second clobber the first's
    # derivation_history append. Routed through locked_rmw the same way, and
    # the mutate closure re-reads phase from the freshly-locked text and
    # aborts (no write) if it has changed since the gate PASS was computed —
    # a stale-gate write would otherwise bump phase past a state the gate
    # never actually evaluated.
    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"cutover.advance: {resolved} lost its parseable frontmatter "
                "between read and write"
            )
        try:
            current_fm = yaml.safe_load(split.fm_text) or {}
        except yaml.YAMLError as exc:
            raise MutateAbort(
                f"cutover.advance: {resolved} frontmatter is not valid YAML: {exc}"
            )
        if not isinstance(current_fm, dict):
            raise MutateAbort(f"cutover.advance: {resolved} frontmatter is not a mapping")
        if str(current_fm.get("phase", "")) != current_phase:
            raise MutateAbort(
                f"cutover.advance: {resolved} phase changed to "
                f"{current_fm.get('phase')!r} since the gate PASS was computed against "
                f"{current_phase!r} — refusing a stale-gate write; re-run cutover.advance"
            )
        current_fm["phase"] = next_phase
        history = current_fm.get("derivation_history")
        if not isinstance(history, list):
            history = []
        history.append(derivation_entry)
        current_fm["derivation_history"] = history
        new_fm_text = yaml.safe_dump(
            current_fm, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        return rebuild(split, new_fm_text)

    try:
        locked_rmw(resolved, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _reply("", [f"cutover.advance: record not found: {resolved}"], 1)
    except LockTimeout as exc:
        return _reply(
            "",
            [f"cutover.advance: timed out waiting for file lock on {resolved}: {exc}"],
            1,
        )
    except MutateAbort as exc:
        return _reply(
            "", [exc.args[0] if exc.args else "cutover.advance: mutation aborted"], 1
        )

    notes = list(gate_notes)
    notes.append(
        f"cutover.advance: ADVANCED {current_phase!r} -> {next_phase!r}; appended "
        f"derivation_history entry (derived_count={derivation_entry.get('derived_count')})"
    )
    return _reply(
        f"surface={surface!r} phase={next_phase!r} VERDICT=ADVANCED",
        notes,
        0,
        derivation_history_entry=derivation_entry,
    )
