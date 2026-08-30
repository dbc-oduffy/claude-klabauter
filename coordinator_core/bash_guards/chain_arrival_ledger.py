"""coordinator_core.bash_guards.chain_arrival_ledger — append-only "the chain
ran" record, one line per PreToolUse Bash/PowerShell evaluation that reaches
``dispatch.py::evaluate_payload_json``.

WHY THIS EXISTS. 2026-08-29 incident: a dispatched subagent ran `git stash`
on this shared worktree and THREE separately-registered, correctly-written
guards (`block_subagent_stash_creation`, `block_subagent_destructive_action`,
`block_reviewer_bash_outside_allowlist`) were silent for it at once. The
post-mortem could not settle WHY, because there was (and, before this module,
still is) no artifact anywhere that distinguishes "the chain ran and every
guard allowed" from "the chain never ran at all" -- the two are identical on
disk. This module exists to answer exactly one question after the fact: did
the chain reach evaluation for session X at time T? Nothing else.

WHAT THIS IS NOT:
  - NOT a guard. It carries no verdict, computes no classification, and
    ``check(payload)`` does not exist in this module -- there is nothing here
    for ``dispatch.py``'s guard loop to register.
  - NOT capable of changing an allow/deny verdict. The record path runs
    strictly before ``guard_chain`` is even built and its result is never
    read back by anything in this process.
  - NOT a second identity resolution. The caller (``dispatch.py``) already
    parses ``session_id``/``agent_id`` out of the one payload parse per the
    module docstring's "Parse-once contract" -- this module accepts those as
    plain arguments and computes nothing about identity itself.
  - NOT unbounded. See ``_ROTATE_MAX_BYTES``/``_ROTATE_GENERATIONS`` below --
    this file is capped and rotated from the first write, unlike the fail-open
    log this incident's own post-mortem found had grown to 80MB with no
    rotation (a peer's concurrent, separate fix -- this module does not import
    or extend that one).

RECORD SHAPE. One JSON object per line: ``{"session_id", "at", "has_agent_id"}``.
  - ``session_id`` -- the raw ``payload["session_id"]`` the caller already
    extracted; also implied by the per-session directory this file lives in
    (mirrors ``guard_advisory_counter.record_deny_fire``'s own inclusion of a
    redundant ``"session"`` field for exactly this reason: a reader of one
    line should not have to trust the directory it was found under).
  - ``at`` -- UTC ISO-8601 timestamp of this arrival.
  - ``has_agent_id`` -- the RAW PRESENCE of ``payload["agent_id"]``, not its
    resolved kind. This is the same discriminator
    ``block_subagent_stash_creation.py`` documents under its own "IDENTITY-
    GATE POSTURE": raw presence is what the harness actually supplies
    per-call, and is available even when a resolver downstream would fail to
    resolve a kind. ``True`` reads as "a dispatched subagent's call reached
    the chain"; ``False`` reads as "an EM main-loop call reached the chain".

NEGATIVE SPEC -- what a future editor must not add here:
  - No command text, no file paths, no tool_input, no cwd. This is an
    ARRIVAL record, not an audit log -- the same content-shape restraint
    ``guard_advisory_counter``'s own module docstring states for its record
    ("Guard name and UTC timestamp only -- no payload, no command text, no
    file path, no session content").
  - No read-back. Nothing in this module, and nothing else in this repo as of
    this module's introduction, reads ``chain-arrival-ledger.jsonl`` back to
    make a decision. Widening that is a fresh PM ruling, same posture as
    ``guard_advisory_counter``'s own "KEEP IT COUNT-AND-LOG" clause.
  - No process spawn, no git call, no read-then-write of the ledger's own
    content. The one filesystem read this module performs is a cheap
    ``stat()`` on the ledger file to decide whether to rotate -- never a read
    of its bytes.

FAIL-SILENT, ALWAYS. ``record_chain_arrival`` never raises. Every failure
mode (unresolvable session id, unwritable directory,
disk full, a rotation race) degrades to a silent no-op. Unlike
``guard_advisory_counter.record_advisory_fire``/``record_deny_fire`` (which
may raise on a write failure and push the swallow onto their callers), this
module owns its own swallow internally -- ``dispatch.py`` calls this at a
site with no established try/except-around-a-recorder convention yet, and the
brief for this change is unconditional: instrumentation must never be able to
break the guard chain, full stop, so the belt-and-braces sits here rather
than trusting every future call site to add its own.

COST. One ``open(..., "a")`` + one ``write()`` on the hot PreToolUse path,
plus (rarely -- only once the file crosses the size cap) a handful of
``os.replace`` renames. No process spawn, no git subprocess, no scan over
existing content -- cost is O(1) per call, never O(history). Measured
per-call cost is reported in this change's dispatch brief response, not
repeated here (a docstring number goes stale the next time hardware changes;
the measurement method -- ``time.perf_counter`` around the call -- does not).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home

_LEDGER_FILENAME = "chain-arrival-ledger.jsonl"

#: Size cap, per generation, before rotation kicks in. Deliberately small --
#: this file answers a point-in-time question ("did the chain run for THIS
#: session recently"), not a historical archive; a few hundred KB of recent
#: arrivals is ample.
_ROTATE_MAX_BYTES = 512 * 1024

#: Fixed number of rotated generations kept alongside the live file
#: (``chain-arrival-ledger.jsonl.1`` .. ``.<N>``). Bounded from birth -- total
#: disk footprint per session directory is capped at
#: ``(_ROTATE_GENERATIONS + 1) * _ROTATE_MAX_BYTES`` regardless of how long a
#: session lives or how many calls it makes.
_ROTATE_GENERATIONS = 3

#: Settings-home-rooted, machine-scoped, NOT `<repo_root>/state/...`, for the
#: reason `block_subagent_destructive_action._fail_open_log_path` states for
#: its own sibling log: a git root is frequently unresolvable on exactly the
#: calls this exists to record, and subagent traffic routinely spans several
#: repos in one session. A repo-rooted ledger reintroduces the ambiguity this
#: module exists to remove -- a missing record would mean EITHER "the chain
#: never ran" OR "the chain ran in a directory with no resolvable git root",
#: and the whole value here is that absence has exactly one meaning.
_LEDGER_RELPATH = ("state", "chain-arrival-ledger")

#: Corpus-mutator declaration (generator-provenance sweep). Rooted at the
#: settings home, so this pattern is machine-scoped and matches nothing in
#: any repo tree.
MUTATES = ["state/chain-arrival-ledger/**/chain-arrival-ledger.jsonl*"]


def _rotate_if_oversize(path: Path) -> None:
    """Cheap, best-effort rotation: stat the live file only, never read its
    bytes. Any failure here (permission race, concurrent renamer, missing
    file between the stat and the rename) is swallowed by the caller -- this
    function may raise, and does so deliberately, so its own try/except stays
    visible at the single call site rather than doubly nested here.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < _ROTATE_MAX_BYTES:
        return
    oldest = path.with_name(path.name + ".%d" % _ROTATE_GENERATIONS)
    if oldest.exists():
        oldest.unlink()
    for gen in range(_ROTATE_GENERATIONS - 1, 0, -1):
        src = path.with_name(path.name + ".%d" % gen)
        if src.exists():
            dst = path.with_name(path.name + ".%d" % (gen + 1))
            os.replace(src, dst)
    os.replace(path, path.with_name(path.name + ".1"))


def record_chain_arrival(
    session_id: str, has_agent_id: bool, cwd: Optional[str] = None
) -> None:
    """Append one ``{"session_id", "at", "has_agent_id"}`` record marking that
    this PreToolUse evaluation reached ``dispatch.py``'s guard chain.

    NEVER RAISES. No-op (not an error) when ``session_id`` is empty/
    unresolvable -- same posture as every
    sibling per-session counter in this package (``guard_advisory_counter``).
    A write failure (unwritable directory, disk full, a rotation race) is
    caught and swallowed HERE, not left to the caller, because the caller
    (``dispatch.py``) is the guard chain itself: this call must be a pure
    side effect with zero ability to turn a would-be allow into a deny, or a
    would-be deny into a crash-deny detour.

    ``has_agent_id`` is the raw ``bool(payload.get("agent_id"))`` presence
    the caller already computed -- this function does not resolve or
    re-derive it.
    """
    if not session_id:
        return
    try:
        path = (
            settings_home()
            / Path(*_LEDGER_RELPATH)
            / session_id
            / _LEDGER_FILENAME
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _rotate_if_oversize(path)
        except OSError:
            pass
        record = {
            "session_id": session_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "has_agent_id": bool(has_agent_id),
            "cwd": cwd or "",
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 -- fail-silent contract; see module docstring
        pass
