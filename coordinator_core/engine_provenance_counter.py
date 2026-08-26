"""coordinator_core.engine_provenance_counter — append-only sink for
`cc_invoke.py`'s wrapper-level provenance reports.

WHY THIS EXISTS. `docs/plans/2026-08-26-the-seam-reports-what-it-got.md` § C1-C4
wired every `*_on_path` wrapper and `_seam_present` in `coordinator/bin/lib/
cc_invoke.py` to ask "did the already-imported `coordinator_core` come from the
root I resolved?" (`provenance_against`, wrapped per-call-site as a
`ProvenanceReport` carrying `caller` and `axis`). That query answers the
question; this module is where the answer goes. § C6's own body records the
choice and its grounds in full (`state/sizings/2026-08-26-the-provenance-gap-
closed-end-to-end-no.yaml`'s `pm_resolution.warn_sink`) — summarized here only
to the extent this module's shape depends on it:

  - A counter under `state/`, read at a cadence — NOT a per-invocation stderr
    line. `docs/decisions/DR-287-emit-cadence-halted-pending-consumer-pur.md`
    is direct precedent: a synchronous per-invocation emitter nobody was
    reading masked a fleet-wide failure mode for days.
  - `cc_invoke.py`'s reporting call sites sit under `UserPromptSubmit` and
    other hot, blocking paths on a 50-70-concurrent-session box; a line per
    call is noise `docs/wiki/guard-messaging.md` § Register independently
    forbids.
  - The record must carry per-carrier identity and axis, not a bare tally —
    that is what `docs/plans/2026-08-26-the-seam-reports-what-it-got.md` § C7
    (the per-carrier divergence inventory) reads back out of this file.

SHAPE MIRRORS `coordinator_core/registry_fallback_counter.py` and
`coordinator_core/guard_advisory_counter.py` deliberately (append-only JSONL,
`resolve_git_root_cheap` for the target root, no-op on an unresolvable root,
raises on an actual write failure and leaves swallow-and-continue to the
caller) with ONE structural difference: those two are per-SESSION files under
`state/subagent-share/<session_id>/`, because their callers (`ipc.py`,
`write_guards/engine.py`, `bash_guards/dispatch.py`) always run inside a
resolved coordinator session. `cc_invoke.py`'s wrappers do not — they are
called from ~201 `coordinator/bin` CLIs, many of which run outside any
session context and carry no `session_id` this module could key on. This file
is therefore a single fleet-wide append target, `state/engine-provenance-
counts.jsonl`, keyed by nothing narrower than the repo. Concurrent-append
safety rests on the same property POSIX gives every other append-only JSONL
sink in this tree: each record here is one `json.dumps(...) + "\n"` write,
comfortably under the `PIPE_BUF` atomic-write threshold, so interleaved writes
from concurrent processes land as distinct whole lines, never as spliced
bytes — no lock file, no rotation, matching this module's own read-at-a-
cadence contract (nothing in this file reads the counter back; C7 does, as a
distinct process, not concurrently with a write it needs to coordinate with).
(Review: code-reviewer P2 — this PIPE_BUF/O_APPEND atomicity argument is
POSIX-only; Windows FILE_APPEND_DATA concurrent-writer behavior is a
different, unaddressed guarantee, and this module's ~201-CLI caller
population is more likely to run under a Windows dispatch path than the
guard/dispatch-only siblings this shape was copied from — inherited, not
newly introduced here, and not fixed in this pass.)
(Review: code-reviewer P3 — `state/engine-provenance-counts.jsonl` is a
single fleet-wide, unbounded, never-rotated file; no retention/rotation
policy is committed anywhere in this plan yet — deferred to C7/ops, no named
owner.)

CANNOT-BREAK-THE-CALLER CONTRACT: `record_engine_provenance` does not itself
swallow exceptions — an unresolvable root degrades to a silent no-op (see
below), but an actual write failure (unwritable directory, disk full) still
raises. `cc_invoke.py::_report_provenance` is the sole caller and wraps its
whole body — the query call, this sink call, and the `ProvenanceReport`
construction — in one outer `except Exception`, so a raise here degrades that
call to an `unresolved`-verdict report rather than propagating past the
wrapper it backs (hard constraint 3: never raise, on any input or filesystem
state).

NEGATIVE SPEC — what this is deliberately NOT:

  - NOT a guard. It has no verdict and blocks nothing; a divergent report is
    a fact to count, not a violation to refuse.
  - NOT read back by anything in this module. Append only; C7's inventory is
    a separate reducer process.
  - NOT widened past caller/axis/verdict/imported_file/engine_root. No
    params, no payload, no command text, no session content — mirrors the two
    sibling counters' own record-shape freeze.
  - Does NOT take a `ProvenanceReport` (the `cc_invoke.py`-local NamedTuple)
    as its parameter. `coordinator_core` must not import from
    `coordinator/bin/lib` — that is the wrong direction across the DR-047
    boundary this repo's own CLAUDE.md documents. This module's signature is
    five plain values instead, so `cc_invoke.py` unpacks its own NamedTuple
    at the call site.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.subagent_sandbox import resolve_git_root_cheap

_COUNTS_FILENAME = "engine-provenance-counts.jsonl"

#: This module's own repo root (`coordinator_core/` -> repo). Used only by the
#: under-pytest destination guard in `record_engine_provenance` — see its
#: docstring for why the guard keys on destination rather than on pytest alone.
_OWN_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Corpus-mutator declaration (generator-provenance sweep): this recorder
#: appends to one fleet-wide file under state/ — see module docstring for why
#: it is not session-scoped like its two sibling counters.
MUTATES = ["state/engine-provenance-counts.jsonl"]


def record_engine_provenance(
    caller: str,
    axis: str,
    verdict: str,
    imported_file: Optional[str],
    engine_root: Optional[str],
    cwd: Optional[str] = None,
) -> None:
    """Append one `{"caller", "axis", "verdict", "imported_file", "engine_root",
    "at"}` record for one `ProvenanceReport` produced at a `cc_invoke.py`
    wrapper or `_seam_present` call site.

    `caller` is the wrapper name (`"ensure_engine_on_path"`,
    `"require_engine_on_path"`, `"require_colocated_engine_on_path"`,
    `"require_dispatch_engine_on_path"`, or `"_seam_present"`); `axis` is
    `"dispatch"` or `"locator"` — both taken verbatim from the caller's
    `ProvenanceReport`, never re-derived here. `verdict` is one of
    `provenance_against`'s four literals (`"unimported"`, `"match"`,
    `"divergent"`, `"unresolved"`); this module does not validate it against
    that set — `cc_invoke.py` owns that vocabulary, this module only records
    it.

    No-op (not an error) when the git root is unresolvable — the same
    degrade-quietly posture `record_registry_fallback` and
    `record_advisory_fire` take on their own unresolvable-root case, so a
    provenance report produced outside any repo checkout this module can
    locate is dropped rather than routed to an invented fallback location.

    May raise on an actual write failure (unwritable directory, disk full);
    the caller (`cc_invoke.py::_report_provenance`) is responsible for the
    swallow-and-continue — see module docstring's CANNOT-BREAK-THE-CALLER
    CONTRACT.

    NO-OP UNDER PYTEST, and that is a correctness requirement rather than a
    tidiness one. The tests covering the reporting seams call the REAL
    `_report_provenance` with sentinel roots under `tmp_path`, so every run
    of the suite appended `divergent` records naming pytest temp directories
    into the live counter. Measured once before this guard: 14 of 23 records
    were test sentinels. C7's inventory reads this file as its runtime view —
    the one view that is supposed to be ground truth about real divergence —
    so a reader would have concluded carriers were diverging in production
    when nothing was, and every future suite run would have deepened it. A
    counter that records its own test fixtures is the same defect class this
    whole seam exists to close: an instrument wired to its own output.

    Guarding here rather than in the tests is deliberate. A convention that
    each test must remember to patch the sink is one a future test author
    silently breaks, and the breakage is invisible — it looks like data.

    The guard keys on the DESTINATION, not merely on pytest: under pytest it
    refuses only writes that would land in THIS repo's own counter, and still
    writes normally to a `tmp_path` root. That keeps the sink genuinely
    testable end to end — a test that mocks the sink cannot catch a sink that
    silently no-ops, which is precisely the defect that shipped here — while
    making the polluting write the impossible one.
    """
    git_root = resolve_git_root_cheap(cwd)
    if not git_root:
        return
    if os.environ.get("PYTEST_CURRENT_TEST") and Path(git_root).resolve() == _OWN_REPO_ROOT:
        return
    path = Path(git_root) / "state" / _COUNTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "caller": caller,
        "axis": axis,
        "verdict": verdict,
        "imported_file": imported_file,
        "engine_root": engine_root,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
