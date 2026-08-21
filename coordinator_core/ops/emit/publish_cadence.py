"""
coordinator_core.ops.emit.publish_cadence — the two-and-only-two scheduled call sites
for `emission.publish`: workday-close and workweek-close.

Purpose: name ONE reachable function the close-ceremony orchestration scripts
(`coordinator/bin/workday-complete-close.py`, and its workweek-complete-close.py
sibling — neither is edited by this module; wiring a call into either is the EM's
follow-up, out of this chunk's `writes:` scope) call to fire `emission.publish` on a
schedule. This module is the negative-test anchor for AC10 ("the only scheduled publish
call sites are workday-close and workweek-close; a test asserts no publish call site
exists on the emit path") — see `tests/test_publish_cadence.py`'s AST scan of
`ops/artifact_emit.py` and `ops/emit/envelope.py`, the two modules that make up "the emit
path".

PM Trigger-model ruling, 2026-08-21 (`docs/plans/2026-08-21-emission-publish-producer.md`
§ Trigger model): "They pull; we do not push on their behalf." Emit-frequency publishing
(a push per `artifact.emit`) was tried before and refused outright as wasteful. Our own
cadence is exactly two sites, bounded and cheap; beyond that, cockpit pulls via the
`coordinator/bin/publish-emission.py` trampoline (C4) whenever THEY want fresher data.

Hard constraints this module honors (each is the plan's own ruling in code form):
  - Does NOT hook the emit path. This module is never imported by, and never imports,
    `coordinator_core.ops.artifact_emit` or `coordinator_core.ops.emit.envelope`'s
    `emit()` writer — see AC9's own posture on `emission_publish.py`, mirrored here.
  - Does NOT re-emit before publishing. `run_publish_cadence` calls the already-
    registered `emission.publish` op verbatim (C4, `coordinator_core.ops.
    emission_publish._emission_publish`), which reads whatever artifact is already on
    disk. If the calling close ceremony refreshed that artifact earlier in its own
    sequence (e.g. via a separate `emit.cadence` step), that freshness is the ceremony's
    doing, not this module reaching for one.
  - Fails loud, not just at the transport (C3's own posture) but here too: this module
    makes no fatal/non-fatal decision. "A publish failure at close must not abort the
    close ceremony" (this chunk's own hard constraint) is the CALLING close-ceremony
    script's decision to catch-and-continue, not this module's to swallow. Catching the
    exception here would take that decision away from the caller and make a real
    transport failure indistinguishable from success.

In-process call, not a subprocess: `coordinator/bin/publish-emission.py` (C4's CLI
trampoline) exists for cockpit's out-of-session pull and deliberately avoids importing
`coordinator_core.ops` at module scope (measured 343.8ms cold-import cost, § Performance
plan). The two ceremony scripts this module is written for already import
`coordinator_core` directly at module scope (e.g. `coordinator_core.daily_day.local_day`,
`coordinator_core.machine_resolver.compute_machine` in `workday-complete-close.py`) and
already pay that cost once per ceremony run — spawning a second interpreter through the
CLI trampoline here would be a pure subprocess-count regression against DR-344's spawn
budget for no benefit. `run_publish_cadence` therefore calls the op's handler function
directly, in-process.

Spec backlink: docs/plans/2026-08-21-emission-publish-producer.md § C5 (AC10)
"""

from __future__ import annotations

from coordinator_core.ops.emission_publish import _emission_publish


def run_publish_cadence(repo_root: str) -> dict:
    """Fire `emission.publish` for one of the two sanctioned cadence sites.

    repo_root: the git common-dir (or any worktree root) of the calling ceremony's
    repo — forwarded to `emission_publish._emission_publish` unchanged; that handler
    performs its own `main_worktree_root` derivation and its own fail-loud `None`
    guard, so this function neither re-derives nor re-validates it.

    Returns the op's own result dict (`ok`, `repo_slug`, `doc_id`,
    `bytes_published`, `emission_path`) on success.

    Raises whatever `_emission_publish` raises on failure (transport failure, absent
    config, single-flight refusal) — unchanged, uncaught. See the module docstring's
    fail-loud note: turning that into a non-fatal skip is the calling ceremony
    script's job, not this function's.
    """
    return _emission_publish({}, repo_root=repo_root)
