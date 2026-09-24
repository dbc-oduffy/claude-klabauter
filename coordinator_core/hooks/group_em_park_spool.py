"""
coordinator_core.hooks.group_em_park_spool — Stop-hook engine op, the
Group-EM wake spool producer.

Purpose: warm command/native-door counterpart of DoE-claude's
`coordinator/hooks/scripts/group-em-park-spool.py` — verbatim port of its
one-append-only producer contract. See the source script's own module
docstring for the full PURPOSE / NEGATIVE SPEC write-up this module
implements without re-deriving it: this file never classifies, never
advances the parked map, only ever appends, and treats spool absence as
"nothing spooled yet" (create-on-append), never as "don't spool".

Rides the same event as the source (`Stop`), ordered — per the source
docstring — AFTER the receiver-state ladder has already written this turn's
verdict; this op reads that verdict via `coordinator_core.session.
receiver_state.read_receiver_state` (the claude-klabauter-native reader — NOT the
cross-plane `coordinator.lib.receiver_state_reader` the DoE script imported
by path, which has no analogue on this side of the port; see
`coordinator_core/receiver_state_reader.py`'s own module docstring for why
the two readers are a distinct surface).

Op contract: `params` reaches this op in either shape a `hooks.*` handler
receives — wrapped as `params["payload"]` by both engine doors, flat by the
cold chain; `_envelope.payload_of` reads both, supplying the Stop payload
dict (`session_id`, `cwd`, ...) — never `os.environ` or this process's own
`cwd`. Always
returns `no_advisory()` (empty dict): this producer never surfaces advisory
text and never blocks a Stop, matching the source script's own
stdout-always-empty, exit-0-on-every-path contract.

Graceful degradation: unresolvable repo root, absent `state/` directory,
absent/unreadable receiver-state record, or an unwritable spool all degrade
to silence — matching the source script's own "every failure mode ...
degrades to silence" contract.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C14
DoE source: coordinator/hooks/scripts/group-em-park-spool.py
"""

from __future__ import annotations

import json
import os
from typing import Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op
from coordinator_core.session.receiver_state import read_receiver_state

#: The drain owns this filename; it sits beside `state/group-em-watch.json`
#: and `state/group-em-watch-parked.json`, which is why `state/` is the
#: anchor and why a repo without that directory has no watch line and is
#: skipped rather than scaffolded.
SPOOL_RELPATH = ("state", "group-em-watch-spool.jsonl")

#: The ladder's bare tag for a parked session. Only this verdict spools.
_PARKED_VERDICT = "PAUSED"

#: Diagnostic only -- names the producing guard so a spool line can be traced
#: back here. The drain never branches on it.
_WRITER = "receiver-state-sensor"


def spool_path(repo_root: str) -> str:
    return os.path.join(repo_root, *SPOOL_RELPATH)


def build_record(session_id: str, verdict: Optional[dict]) -> "Optional[dict]":
    """The ladder's verdict in, one spool record out -- or None to not spool.

    `state` is the ladder's own two fields joined and otherwise untouched.
    `at` is the record's OWN `stamped_at`, never `now()`: the drain compares
    it against `last_tick_at`, so it must be the instant the ladder decided.
    """
    if not isinstance(verdict, dict):
        return None
    if verdict.get("verdict") != _PARKED_VERDICT:
        return None
    stamped_at = verdict.get("stamped_at")
    if not isinstance(stamped_at, str) or not stamped_at:
        return None
    reason = verdict.get("reason")
    reason = reason if isinstance(reason, str) and reason else None
    state = f"{_PARKED_VERDICT}:{reason}" if reason else _PARKED_VERDICT
    return {
        "session_id": session_id,
        "state": state,
        "at": stamped_at,
        "writer": _WRITER,
    }


def append_record(path: str, record: dict) -> None:
    """One `open(..., "a")`, one `write()` of one line. Create-on-append.

    Mode `"a"` creates the file when absent; there is deliberately no lock,
    no read-modify-write, and no `os.replace`.
    """
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


@register_op("hooks.group_em_park_spool")
def _handler(params: dict, repo_root=None) -> dict:
    """Stop: append one Group-EM wake-spool record if this session just
    parked, never otherwise.

    `repo_root` (the framework-supplied handler argument) is unused — this
    op resolves its own repo root from `params["payload"]["cwd"]`, matching
    every other payload-cwd-resolving `hooks.*` op in this family. The whole
    body is wrapped fail-open, matching the source script's own "exit 0 on
    EVERY path" contract.
    """
    payload = payload_of(params)
    try:
        session_id = payload.get("session_id") or ""
        if not isinstance(session_id, str) or not session_id:
            return no_advisory()

        cwd = payload.get("cwd") or os.getcwd()
        if not isinstance(cwd, str):
            return no_advisory()
        root = show_toplevel(cwd)
        if not root:
            return no_advisory()

        if not os.path.isdir(os.path.join(root, SPOOL_RELPATH[0])):
            return no_advisory()

        # Deliberately NOT re-checking the carrier before this read (source
        # docstring, review finding 4): a vanished carrier between
        # precondition and invocation just falls through `read_receiver_state`
        # to None, which `build_record` already treats as "do not spool".
        verdict = read_receiver_state(session_id, root)
        record = build_record(session_id, verdict)
        if record is None:
            return no_advisory()
        append_record(spool_path(root), record)
    except Exception:
        return no_advisory()

    return no_advisory()
