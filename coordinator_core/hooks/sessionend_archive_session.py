"""
coordinator_core.hooks.sessionend_archive_session — SessionEnd warm-door op.

Purpose: warm-door counterpart of DoE-claude's
`coordinator/hooks/scripts/sessionend-archive-session.py`, which today shells to
`coordinator/bin/archive-session-scope.py archive-session --sid <sid>` — a spawn
onto a trampoline in THIS repo (149 lines) whose whole job is
`coordinator_core.session.scope.archive(sid)`. This op deletes BOTH spawns: no
`coordinator/bin/archive-session-scope.py` process, and no interpreter start for
the hook itself, by calling `archive()` in-process on the resident engine.

Classification per `docs/reference/warm-hook-migration.md`'s SessionEnd row: the
live gate's `_RECOVERABLE_REGISTRATIONS` (which supersedes `DR-warm-hook-miss-
policy`'s literal Terminal text where the two disagree) classifies this
registration **Reconstructable**, backstopped by the 24h reaper
(`coordinator_core.ops.session.reap`) — a missed archive-on-miss is not lost, it
is picked up by the next reap sweep. This op therefore needs no synchronous-
confirm arm; a miss records nothing special and the reaper closes it later.

Every input is read from `params["payload"]` — the shape
`warm/hook_http.py :: payload_from_event` builds from the fired event — and
NEVER from this process's own `os.environ` or `cwd`: the resident engine serves
~50 concurrent sessions, and neither its environment nor its cwd belongs to any
one of them (see `hook_http`'s own module docstring, obligation 2, and the
worked example `nudge_autonomous_askuserquestion`'s docstring for the same
precedent). `payload["cwd"]` is threaded to `archive()`'s own `cwd` parameter —
the same role the source CLI's ambient process cwd played when it ran as a
per-session subprocess.

Behavior ported verbatim from `archive-session-scope.py`'s `_cmd_archive_session`:
non-fatal by design. A missing/empty `session_id`, or any exception `archive()`
raises, is reported (this module does not have the CLI's stderr channel, so it
is swallowed rather than printed) and the op still returns the no-op envelope —
this is a SessionEnd hook, which surfaces no advisory text and blocks nothing;
the source script's own stderr diagnostics are non-fatal narration, not part of
its return contract. `archive()`'s own `ValueError` on an empty sid is
pre-empted by this handler's own guard, mirroring the CLI's own precondition
check ("this CLI checks the same precondition before the call so the error
message names the CLI, not a bare Python traceback" — same reasoning, this op's
own site instead of the CLI's).

Spec backlink: docs/plans/2026-08-31-six-hook-scripts-become-engine-ops.md
(chunk C2); docs/reference/warm-hook-migration.md (candidate-selection input,
SessionEnd row).
"""

from __future__ import annotations

from typing import Mapping

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.session.scope import archive


@register_op("hooks.sessionend_archive_session")
def _handler(params: dict, repo_root=None) -> dict:
    """SessionEnd: archive this session's claim directory (idempotent, non-fatal).

    `params["payload"]` is the dict `warm/hook_http.py :: payload_from_event`
    builds from the fired event. `session_id` and `cwd` are read from that
    payload, never from `os.environ` or this process's own `cwd`.

    Always returns `no_advisory()` (empty dict) — this hook never surfaces
    advisory text and never blocks, matching the source script's own always-
    exit-0 contract.
    """
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()

    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""

    try:
        archive(session_id, cwd=cwd or None)
    except Exception:
        # Non-fatal by design (module docstring): a failure here is reported,
        # not raised — the 24h reaper is the backstop, per this registration's
        # Reconstructable classification.
        pass

    return no_advisory()
