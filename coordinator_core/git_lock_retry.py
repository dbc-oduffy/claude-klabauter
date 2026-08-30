"""coordinator_core.git_lock_retry — the shared `.git/index.lock` contention
predicate and bounded-retry runner every ceremony commit seam composes.

Purpose: this repo is a deliberately shared working tree (git worktrees are
banned by doctrine — N concurrent EM sessions contend for ONE `.git/index`).
Prior to this module, the lock-contention predicate and its bounded-backoff
retry loop existed in exactly one place
(`coordinator_core/ops/ceremony/detached_render_commit.py`) and nothing
outside that module used it — `contract/apply_base.py::scoped_commit`,
`ops/ceremony/detached_render_commit.py`, and
`backlog_grind_assemble/apply.py::_commit_one` each shelled out to `git add`/
`git commit` with no retry at all, so the FIRST `index.lock` collision from a
sibling session's concurrent commit crashed the ceremony mid-transaction —
artifact written to disk, commit never made, later directives never run.
Observed live 2026-08-07 (`baton-assemble apply handoff`, five peer sessions
within 17 seconds). This module lifts the existing, correctly-designed
predicate out of `detached_render_commit.py` into a LEAF module every
commit seam consumes, so the policy is get-right-once rather than
hand-copied and independently drift-prone. The default backoff schedule
itself is NOT lifted from `detached_render_commit.py` -- see
`DEFAULT_BACKOFF_SCHEDULE_S`'s own comment for why and its measured
source.

This module has no git opinion of its own: `run_with_lock_retry` takes a
ZERO-ARG CALLABLE that performs one git invocation and returns a
`CompletedProcess`-shaped result (`.returncode`, `.stderr`) — it never
imports `subprocess` and never shells out itself. A caller with its own git
runner (an in-process fake in tests, a real `subprocess.run` wrapper, a
consumer-specific `_run_git`) closes over it in a zero-arg lambda/partial and
hands it in unchanged.

Spec backlink: state/handoffs/2026-08-07-git-index-lock-retry-at-the-ceremony-commit-seam.md

Negative-spec:
    - Retries lock-shaped stderr ONLY (`LOCK_CONTENTION_SIGNATURES`) — every
      other failure (permissions, hook rejection, bad pathspec, bad config)
      is terminal on FIRST sight, zero retries burned. A blanket retry
      around any git failure would mask a real error and turn a loud crash
      into a slow one.
    - Never retries unboundedly — `DEFAULT_MAX_ATTEMPTS` /
      `DEFAULT_BACKOFF_SCHEDULE_S` are a fixed-length schedule, not an
      open-ended spin, and both are caller-overridable parameters rather
      than hardcoded constants a second call site would need to re-derive.
    - Never deletes or otherwise touches `.git/index.lock` itself — a
      concurrent peer is genuinely mid-write; orphan-lock reaping is a
      separate, existing, age-and-stability-gated mechanism
      (`ops/reap_stale_locks.py`), never this module's job.
    - Never wraps a whole ceremony (multi-directive `apply()` run, a whole
      `commit_own_artifact` add-diff-commit sequence, etc.) in a retry loop
      — retry belongs at the SINGLE git invocation. Wrapping a whole
      ceremony re-runs already-landed side effects on every retry (proven
      the hard way authoring the handoff this module implements: a whole-
      ceremony retry loop around `baton-assemble apply spinoff` minted a
      new timestamped artifact per attempt, since `already_satisfied` never
      fires against a freshly-created path).
    - Imports stdlib plus `time` only — no import from `coordinator_core.
      ops.*`, `coordinator_core.contract.*`, or any assembler package. This
      is a leaf module every commit seam (including future ones) can depend
      on without a layering cycle.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

#: Substrings that identify a `.git/index.lock` contention failure in git's
#: own stderr -- the ONLY class of failure `run_with_lock_retry` retries.
#: Any other stderr (permissions, hook rejection, bad config, etc.) is
#: terminal on first sight. Lifted verbatim from
#: `ops/ceremony/detached_render_commit.py::_LOCK_CONTENTION_SIGNATURES`.
LOCK_CONTENTION_SIGNATURES = (
    "index.lock",
    "Another git process seems to be running",
)

#: Substrings that identify a `.git/refs/...` compare-and-swap contention
#: failure -- a PEER SESSION'S commit holding a ref lock across its whole
#: commit (hooks included), not a stale `index.lock` file. Observed live
#: this session, 2026-08-30, on work/machine-a/2026-08-18to30: eight
#: consecutive scoped `git commit` invocations failed under peer load,
#: seven of the eight with `cannot lock ref 'HEAD': is at <sha> but
#: expected <sha>` and only one with the index wording above -- i.e. this
#: is the DOMINANT contention shape on this tree, not a rare second case.
#: A separate tuple rather than folding this wording into
#: `LOCK_CONTENTION_SIGNATURES`: a ref lock is a different file, a
#: different git subsystem, and a different hold duration than an index
#: lock, and collapsing the two would leave that constant's name assert
#: something narrower than its contents.
REF_LOCK_CONTENTION_SIGNATURES = (
    "cannot lock ref",
)

#: `cannot lock ref` is ALSO what git prints for a remote push rejection
#: (`hooks/auto_push.py`'s class) on the exact same "refs/heads/<branch>"
#: wording a local ref race produces -- a narrower substring cannot tell
#: these apart, only the line git prefixes the remote form with can. A
#: local ref race wants this module's immediate bounded retry; a remote
#: rejection wants `hooks/auto_push.py`'s own seconds-scale ladder, and
#: neither is the other's mechanism. Belt-and-braces today: no push path
#: routes through this helper, so this exclusion is currently inert -- a
#: fact about the current call-site list, not a property of the
#: predicate, and it is what keeps the predicate honest the day a push
#: path is wired here.
_REMOTE_REJECTION_MARKER = "[remote rejected]"

#: Default bounded retry policy: 1 initial attempt + 7 retries (8 total),
#: exponential backoff (seconds, slept BEFORE attempts 2..8) starting at
#: 0.1s and doubling to a 1.0s cap: (0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0).
#: Never spins indefinitely -- the schedule is fixed-length, not open-ended.
#:
#: NOT the 4-attempt/1.1s figure this module originally lifted verbatim
#: from `ops/ceremony/detached_render_commit.py` -- that figure was
#: unmeasured. This repo's own evidence contradicts it:
#: `ops/fleet/_common.py::_update_index_with_retry` documents a fixed
#: 3x0.05s (150ms) index-resync retry budget being "reliably exhausted by a
#: lock held for as little as ~0.2-0.3s" because this tree's commit path
#: execs `prepare-commit-msg`/`post-commit` hooks that themselves shell
#: out -- which is why fleet moved to 8 attempts / exponential / 1.0s cap
#: (`_INDEX_RETRY_MAX_ATTEMPTS` / `_INDEX_RETRY_INITIAL_SLEEP_S` /
#: `_INDEX_RETRY_BACKOFF_CAP_S`). Three same-date bug-backlog records
#: (`state/bug-backlog/2026-08-13-index-resync-failed-archive-and-commit-c-
#: e4ef4012149f.yaml` and siblings) show even THAT 8-attempt exponential
#: budget being exhausted by `index.lock` contention on this tree, i.e.
#: fleet's number is a floor, not a ceiling -- but this module has no
#: further measurement to derive past it, so it aligns with fleet's
#: existing precedent rather than inventing a third unsourced number.
#:
#: Worst-case wall-clock for the default schedule: 0.1+0.2+0.4+0.8+1.0+1.0
#: +1.0 = 4.5s of sleeping across up to 8 git invocations, before this
#: caller's own git-exec time. Against `docs/wiki/machine-load-norm.md`
#: (spawn-per-call engine, end-to-end invocation budget, 50-70 concurrent
#: LLM sessions as this machine's average): 4.5s is a real cost a seam
#: pays on EVERY genuine lock collision, not a hung timeout -- callers with
#: a tighter budget than this pass their own `max_attempts` /
#: `backoff_schedule_s` rather than forking the default (see module
#: negative-spec: both stay caller-overridable parameters).
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BACKOFF_SCHEDULE_S = (0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0)


def is_lock_contention(stderr: str) -> bool:
    """True iff `stderr` names a `.git/index.lock` OR `.git/refs/...`
    contention failure — a peer session genuinely mid-write, not a stale
    lock file to reap and not a remote push rejection (excluded below even
    though it shares the ref wording) — the only classes of failure
    `run_with_lock_retry` retries. Returns a bool only, never which class
    matched: callers ask one question, retry or not."""
    if any(sig in stderr for sig in LOCK_CONTENTION_SIGNATURES):
        return True
    if _REMOTE_REJECTION_MARKER in stderr:
        return False
    return any(sig in stderr for sig in REF_LOCK_CONTENTION_SIGNATURES)


_ResultT = TypeVar("_ResultT")


def run_with_lock_retry(
    invoke: Callable[[], _ResultT],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_schedule_s: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE_S,
) -> _ResultT:
    """Calls the zero-arg `invoke` up to `max_attempts` times, retrying ONLY
    while the previous call's result has a non-zero `.returncode` AND
    `is_lock_contention(result.stderr)` is true. Sleeps `backoff_schedule_s
    [attempt - 2]` before attempts 2..N (nothing before attempt 1). Returns
    the LAST result obtained, whatever it is — a success, a persisted lock
    failure after the schedule is exhausted, or an immediate non-lock
    failure returned from the very first attempt. Never raises on the
    caller's behalf and never inspects any field but `.returncode`/
    `.stderr` — classifying the result (raise, log, succeed) is the
    caller's job, exactly as it was before this helper existed.

    `backoff_schedule_s` must have at least `max_attempts - 1` entries when
    overridden together with `max_attempts`. `max_attempts` must be >= 1.
    """
    if max_attempts < 1:
        raise ValueError(
            f"max_attempts must be >= 1, got {max_attempts}"
        )
    if len(backoff_schedule_s) < max_attempts - 1:
        raise ValueError(
            f"backoff_schedule_s has {len(backoff_schedule_s)} entries but "
            f"max_attempts={max_attempts} requires at least "
            f"{max_attempts - 1}"
        )
    result: _ResultT = invoke()
    for attempt in range(2, max_attempts + 1):
        if result.returncode == 0 or not is_lock_contention(result.stderr):
            return result
        time.sleep(backoff_schedule_s[attempt - 2])
        result = invoke()
    return result
