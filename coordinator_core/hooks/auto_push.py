"""
coordinator_core.hooks.auto_push — naked-Python post-commit auto-push helper.

Purpose: DR-059 windows-hostile-bash -> Python port of the DoE-owned bash script
`coordinator/bin/coordinator-auto-push` (223 lines). Classifies push failures so
retryable classes (ref-lock, network, gh-transient) get a bounded jittered
retry and everything else fails loud without blocking the commit. Always
exits 0 -- auto-push must never block a commit.

This module is the claude-klabauter half of the auto-push reimplementation; the DoE half
(retiring the bash script + repointing `coordinator-ensure-post-commit-hook` to
exec this module directly, no `bash`/`nohup` layer) routes via cross-repo memo
per DR-047 (DoE owns contract/generator, claude-klabauter owns engine). Do NOT edit DoE's
`coordinator-auto-push` or `coordinator-ensure-post-commit-hook` from here.

Spec backlink: state/handoffs/2026-07-15_164501_auto-push-naked-python-reimpl.md
Doctrine: DR-059 (break-class windows-hostile bash surfaces get reimplemented in
Python, not patched in place); CLAUDE.md § Runtime conventions.

No CLI entrypoint. `main()` (the former `python3 -m coordinator_core.hooks.
auto_push` / `python3 coordinator_core/hooks/auto_push.py` invocation) is
gravestoned 2026-08-30 (overengineering-reviewer Finding 4): the post-commit
hook stopped invoking this module at all once C6/C7 landed, leaving `main()`
with no production caller. Every surviving function here is called
in-process.

Review: coordinator:code-reviewer (P1, 2026-08-30) -- this docstring
previously claimed `warm.push_cadence.sweep_repos` was "the current
production entry into `run_push_with_retry`". Traced and found false:
`sweep_repos` -> `push_outstanding` -> `coordinator_core/ops/ceremony/
push.py` implements its own push/retry logic and never called
`run_push_with_retry`, whose only remaining callers were internal to this
module (`_drain_dead_ref_record`, `drain_pending_push`) and whose only
production writer of the record it acted on (`_write_pending_record`, via
`_hold_window`) was already gravestoned. `run_push_with_retry` was
code-reachable only via the registered `workday.drain_pending_push`
JSON-RPC op acting on a stale pre-existing record file.

GRAVESTONED 2026-08-30 (docs/plans/2026-08-30-who-pushes-and-when.md C2):
`run_push_with_retry`, `drain_pending_push`, `_drain_dead_ref_record`,
`_write_pending_record`, `_remove_pending_record`,
`_clear_pending_record_if_branch`, and `_branch_resolves_locally` are
deleted outright, along with the `workday.drain_pending_push` op
(`coordinator_core/ops/workday_drain_pending_push.py`) and
`_drain_pending_push_after_sync` (`ops/ceremony/push.py`), which had zero
call sites. Nothing rides on this half of the subsystem: the retry
ladder's job is done today by `ops/ceremony/push.py`'s own ladder, and the
drain leg had no answer to "what needs it."

The record's READ primitives (`_read_pending_record`, `_pending_record_
path`, `_record_is_stale`) are gone too (2026-08-30, overengineering-
reviewer finding 2): their sole caller, `orientation/regenerate_cache.py::
emit_auto_push_health`, now reports from the live `push-failures.log`
signal directly instead of a record nothing writes -- see that module for
the migration. See
`state/lessons/2026-08-30-a-survival-citation-needs-the-same-call-19f9f746ade3.yaml`
for the citation-trace lesson that flagged the read-only shape as
insufficient on its own (an import site is not a live call site).

Behavior-preservation notes (read alongside the bash source):
  - Branch case-canonicalization (Windows case-insensitive-FS fix), the
    work/*-only gate, the ordered error-classification ladder, the retry/backoff
    policy, and the `remote: error: Trace:` preamble-skipping first-err
    extraction are all ported 1:1 in semantics -- see each function's docstring
    for the specific behavior being preserved.
  - The Windows+SSH PowerShell fallback was retired outright per the
    2026-08-06 no-shell-spawns PM ruling -- see the "Transport history" note
    below for the preserved rationale. Native `git push` is the unconditional
    happy path on every platform, including Windows+SSH.

This module also fires the DoE cockpit-contract release publish
(`.github/scripts/publish_cockpit_contract.py`) after a successful push, in
place of a GitHub Actions trigger the org's billing-blocked Actions runner
cannot execute. See the "cockpit-contract release publish" section below for
the self-scoping guard (a filesystem check for a DoE-only script path) that
keeps this a no-op in every other fleet repo.
"""

from __future__ import annotations

import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.win_portability import no_console_creationflags


def _subprocess():
    """Function-local accessor for the `subprocess` module.

    `subprocess` import machinery is a measurable interpreter-startup cost
    (Windows Defender per-file scan included) paid by every hook invocation
    even on paths that never spawn a process. This module calls into
    `subprocess` from 7+ functions, so a single shared accessor is used
    instead of repeating the import at every call site.

    Deliberately does NOT memoize the module reference into a module-level
    variable, unlike `ipc.py`'s/`lifecycle.py`'s `_log()`: `sys.modules`
    already caches the real `subprocess` import, so a repeated `import
    subprocess` here is a cheap dict lookup, not a re-import. `_log()`
    memoizes because `logging.getLogger(__name__)` does real construction
    work beyond the module lookup on every call — a cost this accessor
    doesn't have. Review: code-reviewer (P3 nit).
    """
    import subprocess
    return subprocess


def __getattr__(name):
    """PEP 562 module-level lazy attribute resolution.

    Exists ONLY so `auto_push.subprocess` (test-suite monkeypatch shape --
    `monkeypatch.setattr(auto_push.subprocess, "run", ...)`) keeps working
    without a module-level `import subprocess`. First access imports and
    caches the real module (same object `_subprocess()` returns, since both
    go through `sys.modules`), so a monkeypatched attribute on it is visible
    to every in-module call site.
    """
    if name == "subprocess":
        return _subprocess()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ---------------------------------------------------------------------------
# Transport history (rationale preserved, mechanism removed)
# ---------------------------------------------------------------------------
#
# The original bash script hopped through `powershell.exe` to push on
# Windows+SSH because Git Bash's bundled MSYS OpenSSH cannot reach 1Password's
# SSH agent named pipe (\\.\pipe\openssh-ssh-agent). The naked-Python port
# carried that branch forward behind a spike-gated constant
# (`WINDOWS_SSH_POWERSHELL_FALLBACK`, default False) pending verification of
# the hypothesis that a *native* Windows Python process spawning native
# Windows `git` reaches the agent via Windows OpenSSH without any PowerShell
# detour. That spike returned VIABLE (see
# coordinator/bin/coordinator-auto-push's module docstring) -- native
# `git push` reaches the agent on Windows+SSH with no PowerShell hop needed.
# Per the 2026-08-06 no-shell-spawns PM ruling (no shell spawns are
# tolerated, live or dormant), the PowerShell branch and the
# `WINDOWS_SSH_POWERSHELL_FALLBACK` constant were deleted outright rather than
# allowlisted. Native `git push` is now the sole, unconditional transport on
# every platform, including Windows+SSH -- this has been the live behavior
# since the spike resolved, so the deletion changes no runtime behavior.

# ---------------------------------------------------------------------------
# Subprocess wall-clock bounds
# ---------------------------------------------------------------------------
#
# Every `subprocess.run` in this module is bounded. A post-commit hook that can
# block forever is a process leak on a box that runs 50-70 concurrent sessions,
# and it makes "auto-push is healthy" unfalsifiable from the outside: a hung
# instance and a working one are indistinguishable in the log. Reported by
# example-retrieval-repo-em 2026-08-18 with a live 5.5h-resident instance, root-caused to
# an SSH leg that never returns when no usable key reaches the agent -- the
# credentials recover on their own, but the child never does.
#
# `stdin=DEVNULL` (already set at every site) stops git prompting; it does not
# bound a network or agent stall, which is what actually hangs.
#
# A timeout here means a slow op, not a hung one -- and it does NOT stop the
# engine, so every caller treats expiry as a normal failure on its existing
# fail-path, never as an exception that could block the commit.

GIT_READ_TIMEOUT_SECS = 30
"""Wall-clock bound for read-only local git queries (rev-parse, merge-base).

Local and disk-bound; 30s is far past any honest completion on this box and
is a hang detector, not a performance budget."""

GIT_PUSH_TIMEOUT_SECS = 120
"""Wall-clock bound for `git push`, the one network-bound call here.

A hang detector, not a performance budget -- `GIT_READ_TIMEOUT_SECS`'s
docstring draws the same distinction for its own 30s, and it applies here too:
cutting this too far converts a slow-but-succeeding push into an unpublished
commit, which on a branch where auto-push is the sole publisher is worse than
the hang this bound exists to catch. Load never justifies the number either
way -- this repo's own doctrine is that load RAISES the bar, and "the box was
busy" is never the answer (CLAUDE.md § Load norm); a docstring citing 50-70
concurrent LLMs as the reason a push is slow inverts that doctrine, which is
what an earlier version of this paragraph did.

There is no healthy-push duration data anywhere in this repo, so this bound is
UNVALIDATED and awaiting one: `push_once` records each attempt's observed
duration and outcome onto the detached child's existing durable stderr trail,
`.git/auto-push-respawn-stderr.log` -- read that log to accrue the data that
would make this number settable.

Expiry is classified as its own non-retryable `timeout` class by
`classify_error()`'s dedicated `_PAT_TIMEOUT` arm (see that arm's comment),
which is deliberately absent from `_RETRYABLE_CLASSES` -- it is NOT a
retryable network-class failure, despite what an earlier version of this
docstring claimed."""

CONTRACT_PUBLISH_TIMEOUT_SECS = 300
"""Wall-clock bound for the DoE cockpit-contract publish child.

Longest-running of the four (it is a full release publish), and its failure is
already advisory -- it prints a runnable remediation rather than failing the
hook, so expiry routes to that same never-block path."""

# Test seam: skip all backoff sleeps (mirrors COORDINATOR_AUTO_PUSH_NO_SLEEP=1
# in the bash source).
_ENV_NO_SLEEP = "COORDINATOR_AUTO_PUSH_NO_SLEEP"

# Forensic seam: when the hook runs through a wrapper, sys.executable names the
# wrapper's interpreter rather than the real host interpreter that launched it.
_ENV_HOST_PYTHON = "COORDINATOR_HOST_PYTHON"

# Review: overengineering-reviewer Finding 5 -- the sole-publisher
# suppression axis (this constant, `_ENV_SUPPRESS_FOR_SYNC_PUSH`, plus
# its only reader, `main()`, deleted per Finding 4) is gravestoned.
# Verified at HEAD: `git_native.ensure_post_commit_hook` no longer
# passes `skip_env` for the post-commit hook (C7 made its body
# `exit 0`), so the writer side (`_sole_publisher_env` et al. in
# `ops/ceremony/git_native.py`) is dead too -- see that file's own
# Finding-5 annotation.


#: Generator-provenance declaration: every write in this module (push-
#: failures.log, push-stderr-*.log) lands under resolve_git_common_dir() —
#: inside .git/ — never a tracked repo artifact.
GENERATES: list = []

MAX_ATTEMPTS = 3
# Classes that are safe to retry; see classify_error() for why each is/isn't.
_RETRYABLE_CLASSES = frozenset({"ref-lock", "network", "gh-transient"})

# ref-lock's own attempt budget (DEC-1, docs/plans/2026-08-30-ref-locks-ladder-
# reaches-past-the-burst.md). MAX_ATTEMPTS stays the default AND the non-FF poll
# budget, unchanged -- it is load-bearing in both places, which is why this is a
# second constant rather than a larger MAX_ATTEMPTS. network and gh-transient are
# not named here at all, so they keep the default by construction.
REF_LOCK_ATTEMPTS = 7


def _attempts_for(err_class: str) -> int:
    """Attempt budget for `err_class`.

    The retry loop cannot bound itself per-class up front: the class is only
    known after `push_once` fails and `classify_error` runs, inside the loop
    body. The loop is therefore bounded by the widest budget any class can
    reach and stopped by this function at the continue-gate, where the class
    IS known -- the loop bound is a ceiling, never the budget itself.
    """
    return REF_LOCK_ATTEMPTS if err_class == "ref-lock" else MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Error classification -- ORDER IS LOAD-BEARING (most specific first; GitHub
# server rejections before generic auth/network so a push-protection block
# doesn't classify as auth). Patterns sourced from git's refs.c (lock errors),
# receive-pack/transport (non-FF), openssh/curl network paths, and GitHub
# server-side rejection codes (GH001/008/013). Ported verbatim in semantics
# from the bash `classify_error()`.
# ---------------------------------------------------------------------------

_PAT_GH_PUSH_PROTECTION = re.compile(
    r"(GH013|push declined due to repository rule violations|"
    r"push cannot contain secrets|Secret detected|Push Protection)"
)
_PAT_GH_SIZE_LIMIT = re.compile(
    r"(GH001|exceeds GitHub's file size limit|large files detected|file size limit)"
)
_PAT_GH_LFS_QUOTA = re.compile(
    r"(GH008|exceed your.*(data )?quota|LFS.*quota|batch response: .*quota)"
)
_PAT_REF_LOCK = re.compile(
    r"(cannot lock ref|failed to lock|Reference has changed|reference already exists)"
)
# A local branch ref that no longer exists at push time (e.g. renamed out
# from under a stale pending-push record, or deleted between resolve and
# push) -- client-side, printed by `git push` itself before it ever reaches
# the remote. Matched here, ahead of the FF/transient/network arms below,
# because it is the most specific read of "does not match any": nothing
# later in the ladder describes a dead local refspec, so an earlier position
# only widens which stderrs this catches, never narrows another arm's own
# match set.
#
# Two wordings, one class. `src refspec ... does not match any` is what git
# prints when the pushed refspec matches nothing; `<ref> cannot be resolved to
# branch` is what it prints instead when the refspec IS branch-shaped (a
# `refs/heads/...` form) but names a branch that no longer exists -- exactly the
# rename-out-from-under-a-pending-push case this arm was written for, and the
# wording `resolve_branch`'s own docstring already cites. Matching only the
# first left the second falling through the whole ladder to `unknown`, which
# routes to `log_failure` and so writes `.git/push-failures.log` -- the one file
# `log_dead_ref_failure` exists to keep this class OUT of, because its counts
# are read as "crash insurance is not currently working" by
# `workday.surface_auto_push_failure_stats` and the Stop-time tripwire. A branch
# rename therefore raised a standing false alarm on both.
_PAT_DEAD_REF = re.compile(
    r"(src refspec .* does not match any|cannot be resolved to branch)"
)
_PAT_NON_FAST_FORWARD = re.compile(
    r"(non-fast-forward|rejected.*fetch first|tip of your current branch is behind|stale info)"
)
# GitHub server-side transient failures. Distinct from `network` (client-side
# connectivity) because the request reached GitHub and GitHub itself faltered:
# `fatal error in commit_refs`, HTTP 5xx via smart-http (matches both bare and
# `RPC failed; HTTP 5xx` forms), mid-stream sideband disconnects. Empirically
# self-resolving -- the 2026-05-27 cluster (7 of these, logged as `unknown`)
# all eventually pushed once GitHub recovered. Retryable like `network`, but
# with a seconds-scale backoff (ref-lock's sub-second jitter is far too short
# for a server recovering from a 502). NOTE: matched AFTER non-fast-forward so
# a rejection that also closes the connection can't be misread as transient.
# `remote end hung up unexpectedly` is intentionally in `network` below, not
# here -- it is ambiguous (server EOF vs client partition), and the real 502
# case is already caught by the sideband patterns above.
_PAT_GH_TRANSIENT = re.compile(
    r"(fatal error in commit_refs|HTTP 5[0-9][0-9]|send-pack: unexpected disconnect|"
    r"unexpected disconnect while reading sideband)"
)
_PAT_NETWORK = re.compile(
    r"(Could not resolve host|Connection (refused|timed out|reset)|TLS handshake|"
    r"SSL_connect|Temporary failure|Network is unreachable|kex_exchange_identification|"
    r"Broken pipe|early EOF|remote end hung up unexpectedly)"
)
# IGNORECASE is load-bearing, not tidying (2026-08-30). Real git prints
# "fatal: Could not read from remote repository." -- capitalized -- on an SSH
# auth/transport failure, and this pattern's `could not read from remote`
# alternative missed it by exactly that capital C. Every such push fell through
# the whole ladder to "unknown", which is 52 of the 185 rows (28%) in
# example-retrieval-repo's `.git/push-failures.log` and the largest unnamed class in it
# (memo 2026-08-30, Problem 2, Q3). The hazard the arms below record -- a
# case-insensitive fix here silently swallowing push timeouts, whose synthesized
# message also carries "Could not read from remote repository" -- is already
# retired by ORDER: `_PAT_SPAWN_ERROR` and `_PAT_TIMEOUT` are both tested ahead
# of this arm, so a timeout is classified `timeout` before it can reach here.
# `auth` is not in `_RETRYABLE_CLASSES` and neither was `unknown`, so this
# changes only the label an operator sees, never a retry decision -- the same
# shape the `spawn-error` and `timeout` arms took.
_PAT_AUTH = re.compile(
    r"(Permission denied|Authentication failed|Host key verification|publickey|"
    r"could not read from remote|access denied)",
    re.IGNORECASE,
)
# Catch-all for server-side rejections we haven't pattern-matched yet (HTTP
# 5xx via smart-http, branch-protection rules, org SSO, etc.). Distinct from
# "unknown" so the forensic stderr file is the obvious next read.
_PAT_GH_SERVER_REJECT = re.compile(r"^remote: (error|rejected|fatal):", re.MULTILINE)
# push_once's own TimeoutExpired-synthesized message (see below) -- matched
# on the literal phrase THIS module itself writes, so it is unambiguous
# regardless of what the killed subprocess had or hadn't already printed.
# Previously fell through the whole ladder to "unknown" (Defect 3, 2026-08-20
# dispatch): the message's "timed out" wording does not match _PAT_NETWORK's
# "Connection timed out" phrase, despite push_once's docstring intent that it
# land in that bucket. Given its own distinct class instead of being folded
# into `network` -- it is not in _RETRYABLE_CLASSES, matching "unknown"'s
# prior (non-retrying) behavior exactly, so this changes only the label an
# operator/banner sees, never the retry/timing decision.
_PAT_TIMEOUT = re.compile(r"^fatal: push exceeded \d+s and was killed", re.MULTILINE)
# push_once's own spawn-failure message -- the process never started, so there
# is no git stderr at all and every content-matching arm above is structurally
# inapplicable. Matched on the literal prefix THIS module writes rather than on
# the OS exception text it appends, because that text is platform- and
# locale-dependent ("[WinError 2] The system cannot find the file specified"
# vs "No such file or directory") and would make the arm a translation table.
# Previously fell through to "unknown": the 2026-08-25 cluster (three
# FileNotFoundError spawns at 0.00s) reported as "direct push/unknown after 1",
# which reads as a git rejection nobody classified rather than as "git was
# never reached". Deliberately NOT in _RETRYABLE_CLASSES -- an unresolvable
# interpreter/PATH does not heal between two attempts in one process, and
# leaving it non-retrying preserves "unknown"'s exact prior timing behavior, so
# this changes only the label an operator sees.
_PAT_SPAWN_ERROR = re.compile(r"^fatal: git push failed to spawn:", re.MULTILINE)


def classify_error(stderr_text: str) -> str:
    """Classify git-push stderr to decide whether retry is safe.

    Ordered ladder ported verbatim (in semantics) from the bash
    `classify_error()`. Order is load-bearing -- most specific first; GitHub
    server rejections must come before generic auth/network so a
    push-protection block doesn't classify as auth.
    """
    if not stderr_text or not stderr_text.strip():
        return "empty-stderr"
    if _PAT_GH_PUSH_PROTECTION.search(stderr_text):
        return "gh-push-protection"
    if _PAT_GH_SIZE_LIMIT.search(stderr_text):
        return "gh-size-limit"
    if _PAT_GH_LFS_QUOTA.search(stderr_text):
        return "gh-lfs-quota"
    if _PAT_REF_LOCK.search(stderr_text):
        return "ref-lock"
    if _PAT_DEAD_REF.search(stderr_text):
        return "dead-ref"
    if _PAT_NON_FAST_FORWARD.search(stderr_text):
        return "non-fast-forward"
    if _PAT_GH_TRANSIENT.search(stderr_text):
        return "gh-transient"
    if _PAT_NETWORK.search(stderr_text):
        return "network"
    # Ahead of _PAT_AUTH deliberately, and now REQUIRED rather than merely
    # deliberate: the synthesized timeout message carries "Could not read from
    # remote repository", which _PAT_AUTH matches since it became IGNORECASE
    # (2026-08-30, see that pattern). These two arms are what keep a push
    # timeout from reporting as "auth" and sending the operator to check
    # credentials -- do not reorder them below _PAT_AUTH.
    if _PAT_SPAWN_ERROR.search(stderr_text):
        return "spawn-error"
    if _PAT_TIMEOUT.search(stderr_text):
        return "timeout"
    if _PAT_AUTH.search(stderr_text):
        return "auth"
    if _PAT_GH_SERVER_REJECT.search(stderr_text):
        return "gh-server-reject"
    return "unknown"


# ---------------------------------------------------------------------------
# first_err extraction -- preserves the "Trace: <hex>" preamble-skipping fix.
# GitHub's smart-http rejection prints `remote: error: Trace: <hex>` as a
# server-side support correlation ID before the actual rejection reason on
# subsequent `remote:` lines. A naive first-non-blank-line grab throws away
# the real cause. Strategy: prefer the first `remote: (error|rejected|fatal):`
# line that ISN'T the Trace preamble; fall back to first non-blank line.
# ---------------------------------------------------------------------------

_PAT_TRACE_PREAMBLE = re.compile(r"^remote: error: Trace:")
_PAT_REMOTE_REJECT_LINE = re.compile(r"^remote: (error|rejected|fatal):")
_WS_COLLAPSE = re.compile(r"\s+")


def extract_first_err(stderr_text: str) -> str:
    """Extract the most informative stderr line, skipping GitHub's Trace preamble.

    Preserves the bash tail's fallback chain: prefer the first
    `remote: (error|rejected|fatal):` line that isn't the Trace preamble; else
    fall back to the first non-blank non-Trace line. Splits on '\\n' only
    (matching bash's newline-only line model, not Python's broader
    str.splitlines() boundary set), and anchor-matches the Trace-preamble and
    remote-reject patterns against the RAW (unstripped) line -- matching the
    bash greps ('^remote: error: Trace:' / '^remote: (error|rejected|fatal):'),
    which anchor at column 0 and would not match a line with leading
    whitespace. Strips CR, collapses whitespace runs to a single space
    (without also stripping a surviving leading/trailing space, matching sed
    's/[[:space:]]\\+/ /g'), truncates to 200 chars -- matching `tr -d '\\r' |
    sed 's/[[:space:]]\\+/ /g' | cut -c1-200`.
    """
    lines = [
        ln for ln in stderr_text.split("\n")
        if ln.strip() and not _PAT_TRACE_PREAMBLE.match(ln)
    ]

    def _clean(line: str) -> str:
        line = line.replace("\r", "")
        line = _WS_COLLAPSE.sub(" ", line)
        return line[:200]

    for ln in lines:
        if _PAT_REMOTE_REJECT_LINE.match(ln):
            return _clean(ln)

    if lines:
        return _clean(lines[0])

    return ""


# ---------------------------------------------------------------------------
# Branch resolution + gate
# ---------------------------------------------------------------------------

#: Sentinel distinguishing "not yet looked up" from a cached negative result,
#: so an unresolvable git is looked up exactly once, not once per spawn.
_GIT_EXE_UNRESOLVED = object()
_GIT_EXE_CACHE: object = _GIT_EXE_UNRESOLVED


def git_exe() -> str | None:
    """Resolve `git` to an absolute path, once per process.

    Every git spawn in this module goes through here instead of handing a
    bare ``"git"`` to `subprocess.run`. A bare-`git` spawn raises
    `FileNotFoundError` ([WinError 2] on Windows) whenever PATH cannot
    resolve it at spawn time -- and in the DETACHED respawn that carries the
    push, that failure is near-invisible: the 2026-08-25 cluster (three
    attempts, 21:32-21:34) left only `outcome=spawn-error:FileNotFoundError`
    at 0.00s in `.git/auto-push-respawn-stderr.log` and reported as class
    `unknown`. Resolving once collapses three PATH walks into one and turns
    "git is not reachable" from an opaque OS error raised at each call site
    into a single named, logged diagnostic.

    Handed to `subprocess.run` as `executable=`, NOT as argv[0], and that is
    not a style choice. Every spawn here keeps building a literal
    `["git", ...]` argv, because that literal is what
    `coordinator_core/tests/test_shared_git_runner.py` keys on to see this
    module at all -- `git/run.py`'s negative spec states the key outright.
    Putting the resolved path in argv[0] fixes this defect by making a
    git-spawning module invisible to the gate that inventories git spawns,
    which is not a trade this fix gets to make: both of that gate's registers
    are shrink-only and already at their ceilings, so there is no row to move
    into. `executable=` resolves the binary AND leaves the claim the gate
    reads true -- POSIX execs the absolute path with argv[0] still "git",
    Windows passes it as lpApplicationName alongside the same command line.

    Adjacent, unfixed, and NOT this function's to fix: five modules already
    spawn a `shutil.which("git")`-resolved binary from argv[0]
    (`git/ls_files.py`, `git/ls_files_bytes.py`, `ops/normalize_env.py`) and are invisible to that
    same gate today. Widening its detector surfaces all five at once against
    a register that cannot take a row, so the remedy is migrating them onto
    `coordinator_core.git.run`, which is its own change.

    Returns None when git is unresolvable -- a degrade case every caller
    reports and survives, never a raise. The negative is cached too: PATH
    does not change under a running hook, so a re-lookup would buy the same
    answer for another PATH walk.
    """
    global _GIT_EXE_CACHE
    if _GIT_EXE_CACHE is _GIT_EXE_UNRESOLVED:
        import shutil

        _GIT_EXE_CACHE = shutil.which("git") or _git_exe_off_path()
    return _GIT_EXE_CACHE  # type: ignore[return-value]


def _git_exe_off_path() -> str | None:
    """Locate `git.exe` without consulting PATH. Windows-only; None elsewhere.

    `shutil.which` above is a PATH walk, and PATH is the thing that failed --
    measured against a reproduction of the 2026-08-25 cluster, a child whose
    inherited PATH carries no Git directory gets `which("git") is None` and
    every bare-`git` spawn raises the same `[WinError 2]`. Resolving through
    PATH therefore renames that failure without preventing it: the push still
    does not happen, it just reports `spawn-error` instead of `unknown`. These
    rungs were measured RUNNING (`rev-parse` rc=0) under exactly that stripped
    PATH, which is the bar an anchor has to clear -- resolving a path that does
    not execute is not a fix.

    Order is deliberate, and each rung's standing is different. `GIT_EXEC_PATH`
    first because it names the very git that invoked us when it is present --
    measured exported into a git-spawned subprocess on this box, but NOT
    documented: `githooks(5)` promises only that variables "such as GIT_DIR,
    GIT_WORK_TREE, etc." reach a hook and never names this one. So it is a
    first rung on evidence, never a guarantee, and never the only rung -- it
    was absent in the detached respawn this function exists for. The registry
    key is the documented one: Git for Windows' installer writes
    HKLM\\SOFTWARE\\GitForWindows\\InstallPath expressly so third-party tools
    can locate the install, and removes it on uninstall. HKCU is probed after
    it purely as a cheap miss -- no per-user variant is documented. The
    well-known path is last: a guess, correct on a default install, and better
    than returning None.

    Not the cause here, but worth not re-deriving: a PATH over ~8191 chars can
    break lookups on some spawn paths (CPython #137254). The PATH in the
    failing window was 3,086 chars, so truncation is ruled out by measurement.

    Never raises -- a missing key, a denied read, or no Windows at all all mean
    "this rung has no answer", never a broken push path.
    """
    if os.name != "nt":
        return None

    def _usable(candidate: "str | None") -> "str | None":
        return candidate if candidate and os.path.isfile(candidate) else None

    exec_path = os.environ.get("GIT_EXEC_PATH")
    if exec_path:
        exec_path = exec_path.replace("/", os.sep)
        # libexec/git-core/git.exe, then the ../../bin sibling it hardlinks from.
        for candidate in (
            os.path.join(exec_path, "git.exe"),
            os.path.abspath(os.path.join(exec_path, "..", "..", "bin", "git.exe")),
        ):
            if _usable(candidate):
                return candidate

    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\GitForWindows") as key:
                    install_path = winreg.QueryValueEx(key, "InstallPath")[0]
            except OSError:
                continue
            found = _usable(os.path.join(str(install_path), "cmd", "git.exe"))
            if found:
                return found
    except Exception:
        pass

    for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles")):
        if base:
            found = _usable(os.path.join(base, "Git", "cmd", "git.exe"))
            if found:
                return found
    return None


def _run_git(repo_root: str | None, args: list[str]) -> str | None:
    """Run a git command, returning stripped stdout on success or None on failure."""
    resolved = git_exe()
    if resolved is None:
        print(
            f"coordinator-auto-push: git {' '.join(args)} skipped: "
            f"git is not resolvable",
            file=sys.stderr,
        )
        return None
    cmd = ["git"]
    if repo_root:
        cmd += ["-C", repo_root]
    cmd += args
    subprocess = _subprocess()
    try:
        result = subprocess.run(
            cmd,
            executable=resolved,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_READ_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        print(
            f"coordinator-auto-push: git {' '.join(args)} exceeded "
            f"{GIT_READ_TIMEOUT_SECS}s and was killed",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"coordinator-auto-push: git {' '.join(args)} failed to spawn: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _resolve_repo_root() -> str | None:
    """The enclosing worktree root, WITHOUT a `git rev-parse --show-toplevel` spawn
    in the ordinary case.

    `coordinator_core.git.repo_root.show_toplevel` walks parent directories for a
    `.git` entry and spawns only when the walk finds none -- same answer, same
    `None`-on-failure contract, and it memoizes. This module runs once per commit
    on the hot path, so a subprocess to learn something the filesystem already
    says is pure cost; that helper is already the convention at dozens of call
    sites in this tree, and eacbba04a migrated the harvest-deferrals path to it
    for exactly this reason.

    Falls back to the original spawn if the helper cannot be imported: this is a
    post-commit hook whose first duty is never to block a commit, so an import
    failure must degrade, not raise.
    """
    try:
        from coordinator_core.git.repo_root import show_toplevel
    except Exception:
        return _run_git(None, ["rev-parse", "--show-toplevel"])
    return show_toplevel()


def _canonical_branch_case(common_dir: Path, raw_branch: str) -> str:
    """Find `raw_branch`'s on-disk case among `refs/heads/*` (loose refs,
    then `packed-refs`) without spawning `git`, matching the case-insensitive
    lookup `resolve_branch`'s docstring describes for `for-each-ref
    --format=%(refname:short) refs/heads/`. Falls back to `raw_branch`
    unchanged if no case-insensitive match is found on disk -- same fallback
    `resolve_branch` already had for the spawn-based lookup.
    """
    raw_lower = raw_branch.lower()

    heads_dir = common_dir / "refs" / "heads"
    try:
        if heads_dir.is_dir():
            for path in heads_dir.rglob("*"):
                if not path.is_file():
                    continue
                name = path.relative_to(heads_dir).as_posix()
                if name.lower() == raw_lower:
                    return name
    except OSError:
        pass

    try:
        packed_text = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        packed_text = ""
    for line in packed_text.splitlines():
        line = line.strip()
        if not line or line[0] in "#^":
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/") and ref[len("refs/heads/"):].lower() == raw_lower:
            return ref[len("refs/heads/"):]

    return raw_branch


def _resolve_branch_no_spawn(repo_root: str) -> tuple[bool, str | None]:
    """Git-free branch resolution -- reads `HEAD` and `refs/heads/*` directly
    instead of spawning `git branch --show-current` +
    `git for-each-ref` (§ Corrections 7: two suppressed git spawns cost four
    processes each, via `no_console_creationflags()`'s extra `conhost.exe`).

    Returns `(resolved, branch)`. `resolved=True` means this function reached
    a DEFINITIVE answer -- `branch` is the current branch name, or `None` for
    a detached HEAD (a raw commit sha, or a symbolic ref onto anything other
    than `refs/heads/*`) -- matching `git branch --show-current`'s
    empty-stdout-on-detached contract exactly, so the caller must NOT fall
    back to the spawn in that case. `resolved=False` means the walk could not
    determine an answer at all (missing/unreadable `HEAD`, git-dir resolution
    failure, or the `coordinator_core.git.git_dir` import failing) and the
    caller should fall back to the spawn-based lookup as the safety net --
    same fail-open posture as `_resolve_repo_root`'s import-failure fallback.
    """
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
    except Exception:
        return False, None

    try:
        head_path = resolve_git_dir(repo_root) / "HEAD"
        head_text = head_path.read_text(encoding="utf-8")
    except OSError:
        return False, None

    line = head_text.strip()
    prefix = "ref: refs/heads/"
    if not line.startswith(prefix):
        # Detached HEAD (raw sha) or a symbolic ref outside refs/heads/ --
        # both are a definitive "no current branch", not a walk failure.
        return True, None

    raw_branch = line[len(prefix):].strip()
    if not raw_branch:
        return True, None

    try:
        common_dir = resolve_git_common_dir(repo_root)
    except OSError:
        return False, None

    return True, _canonical_branch_case(common_dir, raw_branch)


def resolve_branch(repo_root: str | None) -> str | None:
    """Resolve the current branch name, canonicalizing its case.

    On Windows's case-insensitive filesystem, `.git/HEAD` can carry a
    different case (e.g. `work/MACHINE-A/...`) than the actual ref file
    (`refs/heads/work/machine-a/...`). `git push` does ref lookups
    case-sensitively against the canonical name, so a mismatched HEAD makes
    `git push origin "$RAW_BRANCH"` fail with "cannot be resolved to branch."
    The canonical-case lookup returns the form the remote also expects. If
    lookup fails for any reason, fall back to the raw branch name so we
    don't regress on Linux/macOS where this hazard doesn't exist.

    Tries `_resolve_branch_no_spawn` first -- zero git subprocesses in the
    ordinary case (an ordinary repo with a resolvable `HEAD`/`refs/heads`) --
    and falls back to the original two-spawn `git branch --show-current` +
    `git for-each-ref` lookup only when the git-free walk could not reach a
    definitive answer (see that function's docstring for exactly which cases
    those are). `repo_root=None` (meaning "use cwd") reaches the fallback
    unconditionally, since `_resolve_branch_no_spawn` needs a concrete path
    to resolve a git-dir from.
    """
    if repo_root:
        resolved, branch = _resolve_branch_no_spawn(repo_root)
        if resolved:
            return branch

    raw_branch = _run_git(repo_root, ["branch", "--show-current"])
    if raw_branch is None or raw_branch == "":
        return None

    refs_output = _run_git(
        repo_root, ["for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    )
    if refs_output:
        raw_lower = raw_branch.lower()
        for ref in refs_output.splitlines():
            if ref.lower() == raw_lower:
                return ref

    return raw_branch


def branch_gate(branch: str) -> tuple[bool, str | None]:
    """Return (should_push, skip_message).

    `work/*` -> proceed (True, None). `migration/*|release/*|feature/*` ->
    skip with a stderr message (False, message). Anything else (including
    `main`) -> skip with a stderr message naming the branch and the work/*-only
    doctrine (False, message) -- this is the canonical/unrecognized catch-all,
    not a recognized long-lived workstream shape, so it must not be silent:
    a branch sitting unpushed here with no visible signal is exactly the
    failure mode this arm exists to prevent.

    Case-sensitivity note (the Staff Engineer R1 F7 + 2026-05-07 HTTPS-autopush spinoff):
    the prefix check accepts both `work/MACHINE-A/...` and
    `work/machine-a/...` transparently, but the *push spec* did not --
    `resolve_branch()`'s for-each-ref normalization handles this on the push
    side too. Do NOT remove that normalization.

    Doctrine: work/* only -- feature/* removed so auto-push cannot propagate a
    non-canonical branch. The create-time denier this originally leaned on
    (`block-off-daily-branch.sh`) was retired 2026-07-05, which makes this
    allowlist the surviving enforcement point, not a redundant second one.
    Review: the Staff Engineer F12 (auto-push tightening) + c474ee1 follow-up.

    AC9b (docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md):
    named owner per declined class, so a branch this gate skips is never
    silently unpublished.

      - `main` -- never pushed by this hook. Named owner: `push_with_retry`'s
        `allow_protected_branch=True` override
        (`coordinator_core/ops/ceremony/commit_pipeline.py`), a keyword-only,
        per-call escape hatch that skips this gate on purpose and prints via
        `_emit_push_policy_line("override-exercised", ...)` every time it
        fires. As of that function's own docstring, its one sanctioned
        consumer is DoE-claude's `merging-to-main` SKILL, Step 10 item 5 (the
        post-merge, on-`main`, release-notes bookkeeping commit) -- no op in
        this repo passes it. `main` otherwise reaches origin by PR merge,
        which is GitHub's own action, not a push this hook or engine issues.
      - `migration/*` / `release/*` / `feature/*` -- no automated publisher.
        Named owner: the committing operator, by this function's own skip
        message ("push manually if intended") -- deliberate, not a gap:
        these are recognized long-lived workstream shapes this doctrine
        chooses to keep off the auto-push path entirely, same as `main`.

    Every other unrecognized branch shape falls into the same
    operator-owned, "push manually if intended" bucket as migration/release/
    feature above -- the skip message names it explicitly rather than
    silently dropping it.
    """
    if branch.startswith("work/"):
        return True, None
    if branch.startswith(("migration/", "release/", "feature/")):
        msg = (
            f"coordinator-auto-push: skipping {branch} (recognized long-lived "
            "workstream shape; doctrine: work/* only). Push manually if intended."
        )
        return False, msg
    msg = (
        f"coordinator-auto-push: skipping {branch} (not a work/* branch; "
        "doctrine: work/* only). Push manually if intended."
    )
    return False, msg


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def is_windows_bash() -> bool:
    """MSYSTEM (Git Bash) or OS=Windows_NT -- mirrors the bash detection."""
    return bool(os.environ.get("MSYSTEM")) or os.environ.get("OS") == "Windows_NT"


def is_ssh_remote(remote_url: str) -> bool:
    """git@host or ssh:// URLs need the SSH agent."""
    return remote_url.startswith("git@") or remote_url.startswith("ssh://")


def route_label(windows_bash: bool, ssh_remote: bool) -> str:
    """Informational label only -- transport choice lives in push_once().

    Native `git push` is the sole transport on every platform (the retired
    PowerShell fallback is documented, not live -- see "Transport history"
    above), so this always reports "direct push". Signature retained (params
    unused) so callers/tests keep passing windows_bash/ssh_remote context.
    """
    del windows_bash, ssh_remote
    return "direct push"


def _log_push_attempt_duration(branch: str, duration_secs: float, outcome: str) -> None:
    """Record one `git push` attempt's observed wall-clock duration and
    outcome (AC16).

    Written with a plain `print(..., file=sys.stderr)` -- no new file, no new
    state path, no spawn -- onto whatever `push_once`'s caller already routed
    this process's stderr to: the detached child's existing durable trail,
    `.git/auto-push-respawn-stderr.log` (opened by `_open_respawn_stderr_log`
    and handed to the respawned child as its stderr fd). This is the data
    `GIT_PUSH_TIMEOUT_SECS`'s docstring names as what would make that bound
    settable -- there is no healthy-push duration data anywhere else in this
    repo.

    Deliberately NOT routed through `log_failure`/`.git/push-failures.log`:
    that surface's own docstrings (`log_race_resolved`, `log_dead_ref_failure`)
    twice state its counts must mean "unrecovered failures", and both
    `workday.surface_auto_push_failure_stats` and the cross-repo Stop-time
    tripwire (`runtime-tripwire-em-check.py::_check_push_failures`,
    DoE-claude) read it -- a success row landing there would poison both
    consumers' counts.
    """
    print(
        f"coordinator-auto-push: push attempt on {branch} took "
        f"{duration_secs:.2f}s (outcome={outcome})",
        file=sys.stderr,
    )


def push_once(repo_root: str, branch: str, windows_bash: bool, ssh_remote: bool) -> tuple[bool, str]:
    """Run a single push attempt. Returns (succeeded, stderr_text).

    Happy path on ALL platforms (macOS, Linux, Windows-HTTPS, Windows-SSH) is
    native `git push` -- unconditionally, including Windows+SSH. The
    once-spike-gated PowerShell fallback was deleted per the 2026-08-06
    no-shell-spawns PM ruling; see "Transport history" above for the
    preserved rationale. windows_bash/ssh_remote are accepted for call-site
    and test-seam compatibility but no longer branch transport choice.

    Records this attempt's observed duration and outcome via
    `_log_push_attempt_duration` (AC16) before returning, on every exit path
    (timeout, spawn failure, and normal completion alike).
    """
    del windows_bash, ssh_remote

    subprocess = _subprocess()
    started = time.monotonic()
    resolved = git_exe()
    if resolved is None:
        _log_push_attempt_duration(
            branch, time.monotonic() - started, "spawn-error:unresolvable-git"
        )
        return False, "fatal: git push failed to spawn: git is not resolvable"
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "push", "origin", branch, "--set-upstream"],
            executable=resolved,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_PUSH_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        # Classified as "timeout" by classify_error()'s dedicated _PAT_TIMEOUT
        # arm (matched on this literal phrase) -- not retried, same as the
        # "unknown" fallback it used to land in before that arm existed.
        _log_push_attempt_duration(branch, time.monotonic() - started, "timeout")
        return False, (
            f"fatal: push exceeded {GIT_PUSH_TIMEOUT_SECS}s and was killed "
            f"(Could not read from remote repository: timed out)"
        )
    except Exception as exc:
        # Synthesized with the same literal phrase the unresolvable-git arm
        # above uses, so classify_error()'s `_PAT_SPAWN_ERROR` labels both
        # `spawn-error` instead of the `unknown` that hid the 2026-08-25
        # FileNotFoundError cluster. The exception's own text is appended,
        # never relied on for matching -- OS spawn messages are platform- and
        # locale-dependent, and this module's own prefix is not.
        _log_push_attempt_duration(
            branch, time.monotonic() - started, f"spawn-error:{type(exc).__name__}"
        )
        return False, f"fatal: git push failed to spawn: {type(exc).__name__}: {exc}"
    duration = time.monotonic() - started
    succeeded = result.returncode == 0
    _log_push_attempt_duration(branch, duration, "success" if succeeded else "failure")
    return succeeded, result.stderr or ""


# ---------------------------------------------------------------------------
# Forensic logging
# ---------------------------------------------------------------------------

def _module_provenance() -> str:
    """Identify the module file, interpreter, and Python version that actually ran.

    The hook does not exec any fixed checkout: DoE's generated post-commit shim
    execs `coordinator/bin/coordinator-auto-push`, which resolves CLAUDE_KLABAUTER_ROOT at
    fire time and imports this module from *whatever* tree that resolves to --
    a mid-edit working tree, a second checkout, or a stale importable copy all
    produce identical log rows otherwise. Recording the resolved path plus the
    interpreter makes an internal-error row self-diagnosing instead of a
    two-repo forensic hunt (2026-07-22 `NameError: _disown_stdio` incident,
    cross-repo/archive/2026-07-23-claude-central-em-auto-push-disown-stdio-nameerror.md).

    Every path is emitted via `as_posix()`: these are TEXT fields in a log whose
    rows are all-forward-slash by contract (see `log_failure`'s forensic-path
    note), so a backslashed `sys.executable` on nt must not drift them.

    The interpreter field prefers `COORDINATOR_HOST_PYTHON` (when set and
    non-empty) over `sys.executable`: a wrapper-launched hook reports the
    wrapper's interpreter via `sys.executable`, not the real host interpreter,
    which defeats the self-diagnosing intent above.
    """
    try:
        module_path = Path(__file__).resolve().as_posix()
    except (OSError, NameError, ValueError):
        module_path = "<unresolved>"
    interp = os.environ.get(_ENV_HOST_PYTHON, "").strip() or sys.executable
    if interp:
        try:
            interp = Path(interp).as_posix()
        except ValueError:
            pass
    else:
        interp = "<unknown>"
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return f"module={module_path} interp={interp} python={version}"


def log_failure(
    repo_root: str,
    branch: str,
    route: str,
    err_class: str,
    attempts: Optional[int],
    first_err: str,
    stderr_text: str,
) -> None:
    """Append a one-line failure summary to the repo's git COMMON dir's
    push-failures.log, copying the full stderr alongside it so server-side
    rejections (GH013 push-protection, GH001 size limit, GH008 LFS quota,
    generic 5xx) whose real message is hidden behind GitHub's
    `remote: error: Trace: <hex>` preamble can still be diagnosed from the
    forensic file.

    Target = the git COMMON dir (`resolve_git_common_dir`), not a literal
    `<repo_root>/.git` join: in a linked worktree, `--separate-git-dir`
    clone, or submodule, `<repo_root>/.git` is a regular FILE, not a
    directory, and the literal join would raise `NotADirectoryError` and
    silently degrade to stderr-only, never creating the log at all. One
    shared log per repo -- a push failure is a repo-level fact, and claude-klabauter
    already keys session bookkeeping on the git common dir throughout
    `coordinator_core/op_scopes.py`.

    Forensic-copy gate matches the bash oracle's `[[ -s "$stderr_src" ]]`
    (non-empty file, any bytes including whitespace-only) -- a plain truthy
    check on `stderr_text`, not `.strip()`, so a whitespace-only stderr still
    gets copied.
    """
    git_dir = resolve_git_common_dir(repo_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    forensic_path = git_dir / f"push-stderr-{stamp}.log"

    if stderr_text:
        try:
            forensic_path.write_text(stderr_text, encoding="utf-8", errors="replace", newline="\n")
            # as_posix(), not str(): this is a TEXT artifact, not a filesystem
            # call. str(Path) emits backslashes on nt, so the log's own rows
            # drift separator-norm mid-file (pre-port bash rows are all-forward;
            # DoE observed the split in their .git/push-failures.log 2026-07-20).
            forensic = forensic_path.as_posix()
        except OSError:
            forensic = "<copy-failed>"
    else:
        forensic = "<empty>"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # `after ?` on `attempts is None` -- an explicit unknown, never a
    # substituted number. Readers of this log take `after <N>` as a measured
    # ladder depth (example-retrieval-repo-em did, memo 2026-08-30 Problem 1), so a
    # caller that did not count its legs must say so rather than name one.
    attempts_text = "?" if attempts is None else str(attempts)
    line = (
        f"[{timestamp}] PUSH FAILED on {branch} ({route}/{err_class} after "
        f"{attempts_text}) :: {first_err or '<empty>'} :: stderr={forensic}\n"
    )
    log_path = git_dir / "push-failures.log"
    try:
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError as exc:
        # This IS the failure-forensics logger; if it can't write, stderr is the
        # only remaining channel to surface the original push failure at all.
        print(f"coordinator-auto-push: cannot write {log_path}: {exc}", file=sys.stderr)
        print(line, file=sys.stderr, end="")


# ---------------------------------------------------------------------------
# Retry policy (2026-05-17, extended 2026-05-27, ref-lock re-timescaled
# 2026-08-30): three retryable classes, each with a backoff matched to its
# recovery timescale:
#   - ref-lock      -- REMOTE ref contention on a shared branch, not a local
#                       lock. The original "resolves in milliseconds" premise
#                       was falsified 2026-08-30 by two measured bursts, 45s
#                       and 70s; see docs/plans/2026-08-30-ref-locks-ladder-
#                       reaches-past-the-burst.md for the counts and DEC-2 for
#                       why the 90s ceiling is acceptable on a loaded box.
#                       Backoff min(2**attempt, 30) + jitter, ~90s over seven
#                       pushes, sized above the longer burst.
#   - network       -- client-side connectivity blip; sub-second jitter (a
#                       retry that fast either reconnects or fails again
#                       cheaply)
#   - gh-transient  -- GitHub server-side 5xx / commit_refs / sideband
#                       disconnect; the server needs SECONDS to recover, so
#                       back off 2s, 4s.
# Non-FF is not re-pushed -- a concurrent push already landed work we don't
# have, so blindly resending the same push would just collide again (and
# auto-rebasing a dirty shared tree in a post-commit hook is both unsafe and
# usually blocked). Instead it gets a bounded poll of `_is_superseded()` on
# the existing backoff for the remaining attempts: if the poll confirms our
# commit already reached origin by some other path, the race resolved on its
# own and nothing more is sent; if every poll comes back negative, this is a
# genuine divergence and fails loud without retry, same as auth/unknown.
# gh-push-protection / gh-size-limit / gh-lfs-quota are deliberately NOT
# retryable: secrets, oversized files, and quota don't self-heal.
# ---------------------------------------------------------------------------

def _backoff_seconds(err_class: str, attempt: int) -> float:
    """Compute the backoff duration for a retryable class at the given attempt.

    ref-lock: min(2**attempt, 30) + 0-500ms jitter -- 2s, 4s, 8s, 16s, 30s, 30s.
    gh-transient: attempt*2 + 0-500ms jitter -- 2s, then 4s.
    network: 200-700ms + 100ms/attempt.

    Why ref-lock's arm is seconds-scale: the "Retry policy" block above.
    """
    import random

    if err_class == "ref-lock":
        return min(2 ** attempt, 30) + random.uniform(0, 0.5)
    if err_class == "gh-transient":
        return attempt * 2 + random.uniform(0, 0.5)
    return (random.uniform(0, 0.5) + 0.2 + attempt * 0.1)


def _no_sleep() -> bool:
    return bool(os.environ.get(_ENV_NO_SLEEP))


def _is_ancestor(repo_root: str, candidate_sha: str, ref: str) -> bool:
    """True if `candidate_sha` is an ancestor of (already reachable from) `ref`.

    Wraps `git merge-base --is-ancestor`, whose exit code IS the answer (0 =
    yes, 1 = no, both expected outcomes) -- deliberately bypasses `_run_git`
    (which treats any nonzero exit as failure) rather than overloading that
    helper's error semantics with a three-way "yes/no/error" result. Any
    inability to run the check (bad ref, spawn failure) reads as False -- the
    safe default that falls through to the normal retry/fail-loud path rather
    than silently swallowing a real divergence.
    """
    resolved = git_exe()
    if resolved is None:
        return False
    subprocess = _subprocess()
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "merge-base", "--is-ancestor", candidate_sha, ref],
            executable=resolved,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_READ_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except Exception:
        # Includes TimeoutExpired. False is this helper's documented safe
        # default -- it falls through to the normal retry/fail-loud path
        # rather than silently swallowing a real divergence.
        return False
    return result.returncode == 0


def _is_superseded(repo_root: str, branch: str, local_sha: str) -> bool:
    """Read-only check: has `local_sha` (our rejected push) already reached
    origin by some other path -- a peer's push, or our own out-of-order async
    sibling for an earlier commit on the same branch?

    `git fetch` only refreshes the local remote-tracking ref; it never
    touches the working tree or local branch history. If the fetch itself
    fails (network down, etc.) this reads as "not superseded" -- we have no
    fresher information, so fall through to the normal retry/fail-loud path
    rather than guessing.
    """
    if _run_git(repo_root, ["fetch", "origin", branch]) is None:
        return False
    return _is_ancestor(repo_root, local_sha, f"refs/remotes/origin/{branch}")


def log_race_resolved(repo_root: str, branch: str, route: str, attempts: int) -> None:
    """Info-level trace for a non-fast-forward race that resolved on its own.

    Deliberately NEVER written to `.git/push-failures.log` -- that file's
    entire job is "crash insurance is not currently working," and a resolved
    race (our commit is already on origin) is the opposite signal. Both
    `workday.surface_auto_push_failure_stats` and the Stop-time mid-session
    detector (`runtime-tripwire-em-check.py::_check_push_failures`, DoE-claude)
    read that file, so keeping resolved races out of it is what keeps their
    counts meaning "unrecovered failures" rather than "lines written." Stderr
    only, matching every other advisory print in this module.
    """
    print(
        f"coordinator-auto-push: race resolved on {branch} ({route}, "
        f"after {attempts} attempt(s)) -- commit already on origin, no data at risk",
        file=sys.stderr,
    )


def log_dead_ref_failure(
    repo_root: str, branch: str, route: str, attempts: int, stderr_text: str
) -> None:
    """Stderr-only report for a `dead-ref` push rejection (AC3).

    Deliberately NEVER written to `.git/push-failures.log`, following
    `log_race_resolved()`'s precedent verbatim: that file's entire job is
    "crash insurance is not currently working" (see its docstring), and a
    `src refspec ... does not match any` rejection means the push targeted a
    branch ref that is simply gone -- there is no crash-insurance question
    to answer, and this class is deliberately excluded from
    `_RETRYABLE_CLASSES` so it is reported exactly once per attempt, never
    looped. Both `workday.surface_auto_push_failure_stats` and the
    Stop-time mid-session detector (`runtime-tripwire-em-check.py::
    _check_push_failures`, DoE-claude) read `push-failures.log`; keeping
    dead-ref rejections out of it is what keeps their counts meaning
    "unrecovered failures" rather than "lines written." This function is
    purely the forensic trace for the push attempt itself, unconditional
    repo_root parameter kept only for call-site symmetry with
    `log_failure`/`log_race_resolved`.
    """
    first_err = extract_first_err(stderr_text)
    print(
        f"coordinator-auto-push: push failed on {branch} ({route}/dead-ref "
        f"after {attempts}) -- branch refspec no longer exists upstream, "
        f"not retrying: {first_err or '<empty>'}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# cockpit-contract release publish -- fires from THIS hook, not GitHub
# Actions, per PM directive (the org's Actions runner is billing-blocked and
# cannot start jobs at all). The publish logic itself
# (`.github/scripts/publish_cockpit_contract.py`, DoE-claude) is untouched
# and already correct -- this seam only decides WHETHER and WHEN to invoke
# it, after a push this hook already performed successfully.
#
# Negative-spec, the one this seam exists to avoid repeating: an earlier
# design attached the same publish step to the `/workday-complete` ceremony,
# which is vendored into every coordinator-installed repo, not just DoE's --
# so example-retrieval-repo's and example-cockpit-repo's daily ceremonies would each have
# tried to publish a tag to DoE's origin and failed forever on any machine
# where DoE is unresolvable (DoE-claude
# cross-repo/archive/2026-07-25-claude-klabauter-em-cockpit-publish-use-a-github-action-not-a-claude-klabauter-directive.md).
# The guard below is intrinsic to the committing repo (a file that only
# DoE-claude tracks), not a repo-name allowlist or a ceremony hook, so it
# cannot reproduce that failure mode in any other repo.
# ---------------------------------------------------------------------------

_COCKPIT_PUBLISH_SCRIPT_RELPATH = Path(".github") / "scripts" / "publish_cockpit_contract.py"
_COCKPIT_SCHEMA_PATH = "coordinator/cockpit-contract/schema"
# git's well-known empty-tree object -- diffing against it lists every path
# in the target tree. Used only as the fallback base for a branch with no
# prior `refs/remotes/origin/<branch>` (first push on a new branch), so
# "schema touched" degrades to "the schema dir exists at the new tip" rather
# than silently skipping the check for lack of a base to diff from.
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _cockpit_publish_script(repo_root: str) -> Path | None:
    """Cheap existence check: does THIS repo track the DoE cockpit-contract
    publish script?

    This single filesystem stat is the entire scoping guard for the seam
    below. It cannot fire in example-retrieval-repo, example-cockpit-repo, or any other
    fleet repo, because none of them track
    `.github/scripts/publish_cockpit_contract.py` -- only DoE-claude does.
    No repo-name allowlist, no machine-local registry lookup: the guard is
    derived from the committing repo's own working tree, so it stays
    correct even if the script is renamed or DoE-claude itself moves.
    Deliberately a `Path.is_file()` stat, not a git call -- cheap enough to
    run unconditionally ahead of the additional git rev-parse this seam
    would otherwise add.
    """
    try:
        candidate = Path(repo_root) / _COCKPIT_PUBLISH_SCRIPT_RELPATH
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _schema_touched(repo_root: str, old_remote_sha: str | None, local_sha: str) -> bool:
    """True if the commit range just pushed touched the cockpit-contract
    schema dir.

    Mirrors the GitHub Actions workflow's own `paths:` filter
    (`coordinator/cockpit-contract/schema/**`,
    `.github/workflows/publish-cockpit-contract-release.yml`) so the hook and
    the (formerly considered) CI trigger agree on what "touched" means.
    Diffs the FULL pushed range (`old_remote_sha..local_sha`), not merely the
    latest commit, so a multi-commit push is handled correctly -- a
    `git diff --name-only HEAD~1..HEAD` shape would miss a schema-touching
    commit buried earlier in a multi-commit push.
    """
    base = old_remote_sha or _EMPTY_TREE_SHA
    out = _run_git(
        repo_root,
        ["diff", "--name-only", f"{base}..{local_sha}", "--", _COCKPIT_SCHEMA_PATH],
    )
    return bool(out)


def _invoke_cockpit_publish(
    repo_root: str,
    script: Path,
    *,
    timeout_secs: float = CONTRACT_PUBLISH_TIMEOUT_SECS,
) -> None:
    """Run the DoE-owned publish script with the repo's own interpreter, cwd
    at the repo root, and let its own gates decide PUBLISH / NOOP / refuse.

    Never raises, and a non-zero exit from the script is NOT treated as a
    hook failure -- see module docstring "auto-push must never block a
    commit" and this seam's own contract point 5 ("never fail the hook"). A
    refusal verdict (REFUSE_DIVERGED / REFUSE_VERSION_GATE) is information
    for a human, surfaced as one clear stderr line naming the exact
    remediation command, never as an exception or a nonzero hook exit.

    `timeout_secs` -- caller-supplied bound, defaulting to
    `CONTRACT_PUBLISH_TIMEOUT_SECS` (the interactive hook path's patience,
    unchanged). The cadence path (`push_outstanding`) passes the per-repo
    remaining slot budget instead, read at call time -- see that module for
    why a second named constant was rejected in favour of this parameter.
    """
    python_exe = _resolve_python_exe() or "python3"
    subprocess = _subprocess()
    try:
        result = subprocess.run(
            [python_exe, str(script)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout_secs,
            **no_console_creationflags(),
        )
    except Exception as exc:
        print(
            f"[coordinator] cockpit-contract publish could not be started "
            f"({type(exc).__name__}: {exc}). Run it yourself: "
            "python3 .github/scripts/publish_cockpit_contract.py "
            "(EXPLAIN_ONLY=1 for a dry run).",
            file=sys.stderr,
        )
        return
    if result.returncode != 0:
        print(
            f"[coordinator] cockpit-contract publish declined or failed "
            f"(exit {result.returncode}). Run it yourself: "
            "python3 .github/scripts/publish_cockpit_contract.py "
            "(EXPLAIN_ONLY=1 for a dry run).",
            file=sys.stderr,
        )


def _maybe_publish_cockpit_contract(
    repo_root: str,
    script: Path,
    old_remote_sha: str | None,
    local_sha: str | None,
    *,
    timeout_secs: float = CONTRACT_PUBLISH_TIMEOUT_SECS,
) -> None:
    """Fire the cockpit-contract publish script after a successful push, if
    (and only if) the pushed range touched the schema dir.

    Called from `push_outstanding` (cadence path, `timeout_secs` bound to
    the remaining slot budget read at call time), only when
    `_cockpit_publish_script`
    already confirmed this repo carries the script. Never raises -- wraps
    its own body so an unexpected failure in this seam (a bad git
    invocation, a permissions error) degrades to a warning rather than
    propagating into the caller, which would risk the "never fail the hook"
    contract on a code path that already successfully pushed the commit.
    """
    try:
        if not local_sha:
            return
        if not _schema_touched(repo_root, old_remote_sha, local_sha):
            return
        _invoke_cockpit_publish(repo_root, script, timeout_secs=timeout_secs)
    except Exception as exc:
        print(
            f"[coordinator] cockpit-contract publish check failed unexpectedly "
            f"({type(exc).__name__}: {exc}). Run it yourself if a schema commit "
            "just landed: python3 .github/scripts/publish_cockpit_contract.py "
            "(EXPLAIN_ONLY=1 for a dry run).",
            file=sys.stderr,
        )


def _engine_source_root_for_currency() -> "Path":
    """The claude-klabauter source tree, taken as the root of the package this module is
    running from — an IDENTITY, never a registry lookup.

    Separate function so the scope test in `_refresh_engine_currency_cache` has
    a seam a test can point at a fixture repo, instead of the test having to
    weaken the scope test itself (which is that function's own subject).
    """
    from pathlib import Path as _Path

    return _Path(__file__).resolve().parents[2]


def _refresh_engine_currency_cache(repo_root: str) -> None:
    """Recompute and persist the publish-lag verdict the forwarder door reads
    (`warm.skew.write_currency_cache`) — best-effort, silent, never raises.

    WHY THE POST-COMMIT PATH OWNS THIS. The door cannot compute it: it is
    forbidden to import `coordinator_core` at all, and the computation costs
    two git subprocesses. This path already spawns git many times and runs on
    exactly the event that invalidates the answer, so the verdict is correct by
    construction rather than by timeout.

    Scoped to the engine's own repo, and the scope test is IDENTITY, not a
    registry lookup: the source tree for this lag is the tree this module is
    running from, so the hook fires the refresh only when `repo_root` IS that
    tree. This hook fires in every fleet repo on the box, and a lag computed
    against example-retrieval-repo's history would be a number about nothing. A different
    repo, a box with no mirror registered, or an execution out of the published
    mirror itself all write nothing — the ordinary state, not a failure, and
    any prior verdict is left untouched rather than replaced with a wrong one.

    Deliberately not logged on failure. Every other best-effort leg in this
    module logs because a missed push strands a peer; a missed cache refresh
    costs one advisory line staying quiet, and a per-commit diagnostic for that
    is the wallpaper this file's own `_ENV_SUPPRESS_FOR_SYNC_PUSH` comment
    rejects.
    """
    try:
        from pathlib import Path as _Path

        from coordinator_core.engine_root import (
            is_published_engine_mirror,
            published_engine_mirror_path,
        )
        from coordinator_core.warm import skew as _skew

        source = _Path(_engine_source_root_for_currency())
        if source != _Path(repo_root).resolve():
            return
        if is_published_engine_mirror(str(source)):
            return
        mirror = published_engine_mirror_path()
        if not mirror:
            return
        _skew.write_currency_cache(_Path(mirror), source)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Async self-detach (GRAVESTONED 2026-08-30, docs/plans/2026-08-30-who-
# pushes-and-when.md C8)
# ---------------------------------------------------------------------------
#
# Review: coordinator:code-reviewer (P3, 2026-08-30) -- this header used to
# describe the os.fork()/Windows subprocess.Popen re-spawn machinery
# (`_detach_and_run`, `spawn_detached_push`) in the present tense. Both are
# gravestoned along with the per-commit respawn they implemented; only
# `_resolve_python_exe()` survives beneath this header now, reused by
# `_invoke_cockpit_publish` to run the DoE-owned publish script with the
# repo's own interpreter -- see that function's own docstring, which
# correctly dates the gravestoning.
# ---------------------------------------------------------------------------

def _resolve_python_exe() -> str | None:
    """Resolve the interpreter used by `_invoke_cockpit_publish` to run the
    DoE-owned publish script with the repo's own interpreter.

    Formerly also shared by the per-commit respawn call sites
    (`_detach_and_run`'s Windows leg and `spawn_detached_push`), both
    gravestoned 2026-08-30 (docs/plans/2026-08-30-who-pushes-and-when.md
    C8) once the post-commit hook stopped invoking `auto_push` at all (C6/
    C7) -- this is now this function's sole caller.
    """
    import shutil

    return sys.executable or shutil.which("python3") or shutil.which("python")

