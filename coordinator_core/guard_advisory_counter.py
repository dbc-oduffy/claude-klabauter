"""coordinator_core.guard_advisory_counter — append-only advisory-fire counter.

The minimum signal DR-277's "we can always revert" needs (docs/plans/
2026-08-06-apply-guard-class-census.md, chunk C21): a COUNT, not a log, of
every true advisory-class guard firing. Guard name and UTC timestamp only —
no payload, no command text, no file path, no session content. Do not widen
this record shape without a fresh PM ruling; C21's own plan body is written
to give a later reader something to argue against.

Written to a per-session file under `machinery_paths.share_dir` —
`<machinery root>/subagent-share/<session_id>/advisory-fire-counts.jsonl`;
the accessor is the authority on that root, which has moved once already and
must never be restated here. Mirrors the fleet's existing subagent-share
convention (`bash_guards/bump_outside_repo_write.py::_sandbox_root`,
`bash_guards/bump_foreign_repo_write.py::_sandbox_root_hint`) — a shared
append target across the six-plus sessions this repo routinely carries at
once would be exactly the concurrency hazard this per-session design exists
to avoid. Aggregation across sessions is a read-time concern (a future
reader/reducer over the per-session files), not this module's.

Two call sites:
  - `write_guards/engine.py::evaluate`, the advisory-phase `return out` (the
    legacy `aggregate=False` default). Since `docs/plans/2026-08-06-windows-
    hot-path-less-work-per-interpreter.md` chunk C6, that same function's
    opt-in `aggregate=True` branch calls this recorder in a loop, once per
    RETURNED advisory — so THIS call site can append more than one record
    per `evaluate()` invocation when a caller opts in. See that function's
    own docstring for the arity note; a reader of `advisory-fire-counts.jsonl`
    should not assume "at most one record per triggering call" holds for
    every session.
  - `bash_guards/dispatch.py::evaluate_payload_json`, the `return out` /
    `return emitted` exits — gated by the caller on `not fail_closed` since
    those are also the exit for an annotated hard-deny envelope. Still a
    single-advisory-return seam.

CANNOT-BREAK-THE-GUARD CONTRACT: this module does not itself swallow
exceptions — `record_advisory_fire` may raise (unresolvable git root,
unwritable directory, disk full). Both call sites wrap the call in their
own `try/except Exception: pass`, placed AFTER the guard's own return value
is already decided, so a failed write can only ever fail to produce a
record; it never turns an advisory into a deny and never reaches the
caller.

DENY-PATH COUNTER (`docs/plans/2026-08-13-em-exercisable-in-band-grant-
route.md` chunk C6): `record_advisory_fire`'s 5,569 recorded fires say
nothing about which HARD-denies actually obstruct people, since both call
sites above sit on the advisory path only. `record_deny_fire` mirrors it
for the deny path, at the two engine call sites where a hard-deny envelope
(`permissionDecision == "deny"`) is actually about to be returned or
cleared: `bash_guards/dispatch.py::evaluate_payload_json` (the in-session-
unlock seam around `_consume_unlock`) and `write_guards/engine.py::evaluate`
(its own hard-deny-phase loop, same seam). Same append-only per-session
file, a sibling filename (`deny-fire-counts.jsonl`) so the two record
streams never interleave, same no-op-on-unresolvable-session-id and
raise-on-write-failure contract as `record_advisory_fire` above — callers
wrap the call in their own `try/except Exception: pass`, placed AFTER the
guard's own return/continue decision is already final.

KEEP IT COUNT-AND-LOG, NEVER AN ENFORCEMENT INPUT. Per `docs/wiki/guard-
messaging.md`: "A guard's advisory/audit trail ... must never be read back
to silently convert a subsequent deny into an allow." Neither this module
nor either call site reads `deny-fire-counts.jsonl` back — it is written,
never consulted, by any code in this repo. Widening that would need a
fresh PM ruling, same as `record_advisory_fire`'s own record-shape freeze.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: `resolve_git_root_cheap`, NOT `resolve_git_root`. Both recorders below run
#: on the PreToolUse path, and the authoritative resolver spawns `git rev-parse
#: --show-toplevel` -- a subprocess charged to DR-344's budget purely to decide
#: where a TELEMETRY line goes. These are miss-mode callers by their own
#: contract: both already degrade to a silent no-op when the root does not
#: resolve, so the cheap walk's one documented weakness (a symlinked ancestor
#: yielding a non-realpath'd root) costs at worst a fire-count file written
#: under a differently-spelled root, never a wrong guard verdict. See
#: `resolve_git_root_cheap`'s own docstring for why a verdict-affecting caller
#: must never make this substitution.
from coordinator_core.subagent_sandbox import resolve_git_root_cheap
from coordinator_core.session.machinery_paths import share_dir as _share_dir

_COUNTS_FILENAME = "advisory-fire-counts.jsonl"
_DENY_COUNTS_FILENAME = "deny-fire-counts.jsonl"

#: Corpus-mutator declaration (generator-provenance sweep): both recorders
#: append to a per-session file under the machinery root's per-session
#: sandbox bucket (`machinery_paths.share_dir`) -- the target file set is
#: data-dependent on session_id.
MUTATES = [".coordinator-local/subagent-share/**/*.jsonl"]


def record_advisory_fire(guard_name: str, session_id: str, cwd: Optional[str] = None) -> None:
    """Append one `{"guard", "at"}` record for a true advisory-class firing.

    No-op (not an error) when `session_id` is empty/unresolvable — the same
    fail-closed-never-grant posture the in-session unlock seam already
    applies to this exact payload field in both engines
    (`write_guards/engine.py:173-174`, `bash_guards/dispatch.py:692-694`).
    Does not invent a fallback shared path on an unresolvable git root
    either — that recreates the concurrency hazard this design avoids, so
    this, too, degrades to a silent no-op rather than a write.

    May raise on an actual write failure (unwritable directory, disk full);
    callers are responsible for the swallow-and-continue (see module
    docstring).
    """
    if not session_id:
        return
    git_root = resolve_git_root_cheap(cwd)
    if not git_root:
        return
    path = Path(_share_dir(git_root, session_id)) / _COUNTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"guard": guard_name, "at": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_deny_fire(
    guard_name: str, session_id: str, cleared: bool, cwd: Optional[str] = None
) -> None:
    """Append one `{"guard", "session", "at", "cleared"}` record for a hard-deny firing.

    The deny-path mirror of `record_advisory_fire` above (see module
    docstring's DENY-PATH COUNTER paragraph) — written to a sibling
    per-session file, `deny-fire-counts.jsonl`, so the two streams never
    interleave. `cleared` distinguishes an in-session-unlock-cleared firing
    (the deny never reached the caller) from an uncleared one (the deny
    envelope was actually returned) — AC-8's "distinguishing cleared from
    uncleared".

    Same no-op-on-unresolvable-session-id posture as `record_advisory_fire`
    (empty/unresolvable `session_id`, or an unresolvable git root, both
    degrade to a silent no-op rather than inventing a fallback shared
    path — the same concurrency hazard that design avoids).

    May raise on an actual write failure (unwritable directory, disk full);
    callers are responsible for the swallow-and-continue, exactly as they
    already are for `record_advisory_fire` (see module docstring).

    COUNT-AND-LOG ONLY: this function only ever appends. Nothing in this
    module, or either call site, reads `deny-fire-counts.jsonl` back — see
    module docstring's KEEP IT COUNT-AND-LOG paragraph.
    """
    if not session_id:
        return
    git_root = resolve_git_root_cheap(cwd)
    if not git_root:
        return
    path = Path(_share_dir(git_root, session_id)) / _DENY_COUNTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "guard": guard_name,
        "session": session_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "cleared": bool(cleared),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
