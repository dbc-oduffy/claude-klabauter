"""
coordinator_core.hooks.auto_push — naked-Python post-commit auto-push helper.

Purpose: DR-059 windows-hostile-bash -> Python port of the DoE-owned bash script
`coordinator/bin/coordinator-auto-push` (223 lines). Pushes the current branch to
origin if it is a `work/*` branch, classifying push failures so retryable classes
(ref-lock, network, gh-transient) get a bounded jittered retry while everything
else fails loud without blocking the commit. Always exits 0 -- auto-push must
never block a commit.

This module is the claude-klabauter half of the auto-push reimplementation; the DoE half
(retiring the bash script + repointing `coordinator-ensure-post-commit-hook` to
exec this module directly, no `bash`/`nohup` layer) routes via cross-repo memo
per DR-047 (DoE owns contract/generator, claude-klabauter owns engine). Do NOT edit DoE's
`coordinator-auto-push` or `coordinator-ensure-post-commit-hook` from here.

Spec backlink: state/handoffs/2026-07-15_164501_auto-push-naked-python-reimpl.md
Doctrine: DR-059 (break-class windows-hostile bash surfaces get reimplemented in
Python, not patched in place); CLAUDE.md § Runtime conventions.

Invocation:
    python3 -m coordinator_core.hooks.auto_push
    python3 coordinator_core/hooks/auto_push.py

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

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as session_liveness
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

Sized against the load norm rather than a fast path: 50-70 concurrent LLMs
plus a dozen sessions sharing this checkout means a genuine push can be slow.
Expiry is classified as a retryable network-class failure, which is what a
stalled SSH leg actually is."""

CONTRACT_PUBLISH_TIMEOUT_SECS = 300
"""Wall-clock bound for the DoE cockpit-contract publish child.

Longest-running of the four (it is a full release publish), and its failure is
already advisory -- it prints a runnable remediation rather than failing the
hook, so expiry routes to that same never-block path."""

# Test seam: skip all backoff sleeps (mirrors COORDINATOR_AUTO_PUSH_NO_SLEEP=1
# in the bash source).
_ENV_NO_SLEEP = "COORDINATOR_AUTO_PUSH_NO_SLEEP"

# Test/dev seam: run synchronously (skip the fork/respawn self-detach) so the
# calling process (and tests) can observe the push outcome directly.
_ENV_SYNC = "COORDINATOR_AUTO_PUSH_SYNC"

# Forensic seam: when the hook runs through a wrapper, sys.executable names the
# wrapper's interpreter rather than the real host interpreter that launched it.
_ENV_HOST_PYTHON = "COORDINATOR_HOST_PYTHON"

# Stand-down seam (opro-01 C-01, state/audits/2026-08-18-opro-01-where-the-
# push-outcome-is-known.md): set by a caller that will publish this commit
# ITSELF, synchronously, and therefore needs to be the only publisher.
#
# Not a disable switch and not a test seam. `git commit` fires this hook and
# the hook detaches; a caller that then runs its own `git push` has two
# publishers racing for one branch tip, and when the detached child wins, the
# caller's push fails on a commit that IS on the remote. That is the
# 2026-07-30 false negative, and it is why `scoped_git_commit` grew a
# remote-confirmation probe to walk its own verdict back.
#
# One publisher, chosen deliberately, makes the caller's own push outcome
# authoritative by construction. The commit is still published -- by the
# caller, in the same invocation, synchronously -- so nothing is deferred and
# no pending record is written. A caller that sets this and does NOT push is
# the one misuse this seam has, which is why the name says SUPPRESS_FOR_
# SYNC_PUSH rather than DISABLE.
_ENV_SUPPRESS_FOR_SYNC_PUSH = "COORDINATOR_AUTO_PUSH_SUPPRESS_FOR_SYNC_PUSH"

#: Generator-provenance declaration: every write in this module (push-
#: failures.log, push-stderr-*.log, coordinator-auto-push-pending.json)
#: lands under resolve_git_common_dir() — inside .git/ — never a tracked
#: repo artifact.
GENERATES: list = []

MAX_ATTEMPTS = 3
# Classes that are safe to retry; see classify_error() for why each is/isn't.
_RETRYABLE_CLASSES = frozenset({"ref-lock", "network", "gh-transient"})


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
_PAT_DEAD_REF = re.compile(r"src refspec .* does not match any")
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
_PAT_AUTH = re.compile(
    r"(Permission denied|Authentication failed|Host key verification|publickey|"
    r"could not read from remote|access denied)"
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
    # Ahead of _PAT_AUTH deliberately: the synthesized timeout message carries
    # "Could not read from remote repository", and _PAT_AUTH's "could not read
    # from remote" alternative misses it only by capitalization. Anyone fixing
    # _PAT_AUTH to be case-insensitive (real git prints the capitalized form on
    # SSH auth failure, so that fix is owed) would otherwise silently reclassify
    # every push timeout as "auth" and send the operator to check credentials.
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

def _run_git(repo_root: str | None, args: list[str]) -> str | None:
    """Run a git command, returning stripped stdout on success or None on failure."""
    cmd = ["git"]
    if repo_root:
        cmd += ["-C", repo_root]
    cmd += args
    subprocess = _subprocess()
    try:
        result = subprocess.run(
            cmd,
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


def push_once(repo_root: str, branch: str, windows_bash: bool, ssh_remote: bool) -> tuple[bool, str]:
    """Run a single push attempt. Returns (succeeded, stderr_text).

    Happy path on ALL platforms (macOS, Linux, Windows-HTTPS, Windows-SSH) is
    native `git push` -- unconditionally, including Windows+SSH. The
    once-spike-gated PowerShell fallback was deleted per the 2026-08-06
    no-shell-spawns PM ruling; see "Transport history" above for the
    preserved rationale. windows_bash/ssh_remote are accepted for call-site
    and test-seam compatibility but no longer branch transport choice.
    """
    del windows_bash, ssh_remote

    subprocess = _subprocess()
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "push", "origin", branch, "--set-upstream"],
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
        return False, (
            f"fatal: push exceeded {GIT_PUSH_TIMEOUT_SECS}s and was killed "
            f"(Could not read from remote repository: timed out)"
        )
    except Exception as exc:
        return False, str(exc)
    return result.returncode == 0, result.stderr or ""


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
    attempts: int,
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
    line = (
        f"[{timestamp}] PUSH FAILED on {branch} ({route}/{err_class} after "
        f"{attempts}) :: {first_err or '<empty>'} :: stderr={forensic}\n"
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
# Retry policy (2026-05-17, extended 2026-05-27): three retryable classes,
# each with a backoff matched to its recovery timescale:
#   - ref-lock      -- local push-ref contention; resolves in milliseconds ->
#                       sub-second jitter
#   - network       -- client-side connectivity blip; sub-second jitter (a
#                       retry that fast either reconnects or fails again
#                       cheaply)
#   - gh-transient  -- GitHub server-side 5xx / commit_refs / sideband
#                       disconnect; the server needs SECONDS to recover, so
#                       back off 2s, 4s.
# Non-FF, auth, and unknown classes fail loud without retry -- non-FF in
# particular means a concurrent push already landed work we don't have, so
# silent retry would mask the divergence (and auto-rebasing a dirty shared
# tree in a post-commit hook is both unsafe and usually blocked).
# gh-push-protection / gh-size-limit / gh-lfs-quota are deliberately NOT
# retryable: secrets, oversized files, and quota don't self-heal.
# ---------------------------------------------------------------------------

def _backoff_seconds(err_class: str, attempt: int) -> float:
    """Compute the backoff duration for a retryable class at the given attempt.

    gh-transient: seconds-scale (attempt*2 + 0-500ms jitter) -- 2s, then 4s.
    ref-lock/network: jittered sub-second (200-700ms + 100ms/attempt).
    """
    import random

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
    subprocess = _subprocess()
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "merge-base", "--is-ancestor", candidate_sha, ref],
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
    "unrecovered failures" rather than "lines written." The pending-record
    loop this class was introduced to close is `drain_pending_push()`'s own
    concern (AC4-AC7) -- this function is purely the forensic trace for the
    push attempt itself, unconditional repo_root parameter kept only for
    call-site symmetry with `log_failure`/`log_race_resolved`.
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
    Deliberately a `Path.is_file()` stat, not a git call -- see
    `run_push_with_retry`'s docstring for why that ordering matters.
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


def _invoke_cockpit_publish(repo_root: str, script: Path) -> None:
    """Run the DoE-owned publish script with the repo's own interpreter, cwd
    at the repo root, and let its own gates decide PUBLISH / NOOP / refuse.

    Never raises, and a non-zero exit from the script is NOT treated as a
    hook failure -- see module docstring "auto-push must never block a
    commit" and this seam's own contract point 5 ("never fail the hook"). A
    refusal verdict (REFUSE_DIVERGED / REFUSE_VERSION_GATE) is information
    for a human, surfaced as one clear stderr line naming the exact
    remediation command, never as an exception or a nonzero hook exit.
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
            timeout=CONTRACT_PUBLISH_TIMEOUT_SECS,
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
) -> None:
    """Fire the cockpit-contract publish script after a successful push, if
    (and only if) the pushed range touched the schema dir.

    Called only from `run_push_with_retry`'s two success sites, only when
    `_cockpit_publish_script` already confirmed this repo carries the
    script. Never raises -- wraps its own body so an unexpected failure in
    this seam (a bad git invocation, a permissions error) degrades to a
    warning rather than propagating into `run_push_with_retry`, which would
    risk the "never fail the hook" contract on the code path that already
    successfully pushed the commit.
    """
    try:
        if not local_sha:
            return
        if not _schema_touched(repo_root, old_remote_sha, local_sha):
            return
        _invoke_cockpit_publish(repo_root, script)
    except Exception as exc:
        print(
            f"[coordinator] cockpit-contract publish check failed unexpectedly "
            f"({type(exc).__name__}: {exc}). Run it yourself if a schema commit "
            "just landed: python3 .github/scripts/publish_cockpit_contract.py "
            "(EXPLAIN_ONLY=1 for a dry run).",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Durable pending-push record (AC14/AC14a) -- holds a push back on a shared
# branch instead of publishing a trivially-reversible bad commit within
# ~60s of it landing. See `_hold_window`'s docstring for the full contract;
# this section is the record's read/write/staleness primitives it builds
# on. Lives ENTIRELY inside the detached child (called from the head of
# `run_push_with_retry`, never from `branch_gate()` -- see that function's
# and `_hold_window`'s docstrings for why the parent process that `git
# commit` waits on synchronously must never pay this cost).
#
# The hold's trigger is narrower than plain `_shared_branch_live_count(...) >
# 1`: `_hold_window` also calls `_peer_commit_within_window` and skips the
# hold when a foreign-session commit has already landed on the branch within
# the window. Measured 2026-08-20 on work/machine-a/2026-08-18to20 (trailing
# 36h): 2079 commits, median inter-commit gap 20s, a median of 10 peer
# commits land on top of any given commit within the 300s hold, and only 6
# of 2079 commits had zero followers in that window. On a branch that busy
# the retraction this hold pays for doesn't exist -- by the time the window
# expires, peers have already built on the commit and rewinding it would
# rewrite their work. `live_count > 1` inverts under load: sharing is
# exactly what makes retraction impossible, not what threatens it. A quiet
# shared branch (no recent foreign commits) still holds exactly as before --
# see `_peer_commit_within_window` for the predicate and its fail-toward-
# pushing posture.
#
# Do NOT reuse `_backoff_seconds`/`_no_sleep` for the hold's sleep duration
# beyond skipping the sleep itself under the test seam -- `_backoff_seconds`
# is error-recovery timing (ref-lock/network/gh-transient), a different
# concern with a different timescale, and `COORDINATOR_AUTO_PUSH_NO_SLEEP`
# must skip the sleep CALL only, never the hold DECISION (whether a record
# gets written at all) -- silently disabling the decision under the test
# seam would let it silently disable in a misconfigured production
# environment too.
# ---------------------------------------------------------------------------

_PENDING_RECORD_NAME = "coordinator-auto-push-pending.json"
# Default hold window (AC14: "default ~5 min").
_HOLD_WINDOW_SECONDS = 300
# A record is taken over (not merely drained) once its holder is confirmed
# dead OR its hold_until is this far in the past -- the grace margin gives a
# live holder time to actually finish pushing after waking before a peer
# calls it stale out from under it.
_STALE_GRACE_SECONDS = 60


def _pending_record_path(repo_root: str) -> Path:
    """`<git-common-dir>/coordinator-auto-push-pending.json` -- see
    `log_failure`'s docstring for why this is the git COMMON dir (worktree/
    submodule safety), not a literal `<repo_root>/.git` join.
    """
    return resolve_git_common_dir(repo_root) / _PENDING_RECORD_NAME


def _read_pending_record(repo_root: str) -> dict | None:
    """Read the pending-push record, or None if absent/corrupt/unreadable.

    A corrupt or partially-written record (this process crashing mid-write,
    pre-atomic-rename) reads as None -- exactly like "no record" -- rather
    than raising, so a reader always has a safe fallback: treat it as if no
    hold is in effect and let the normal predicate/write path re-establish
    one.
    """
    try:
        text = _pending_record_path(repo_root).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _write_pending_record(
    repo_root: str, branch: str, sha: str | None, hold_until: float, holder_pid: int
) -> bool:
    """Write the pending-push record BEFORE the holder sleeps (AC14).

    Writes to a pid-suffixed temp file in the same directory, then
    `os.replace`s it into place -- atomic on both POSIX and Windows (unlike
    a direct write, which a concurrent reader could observe mid-write as
    invalid JSON). Returns False (never raises) on any OSError -- the
    caller (`_hold_window`) treats a failed write as AC14a precondition (1)
    unmet and pushes immediately rather than sleeping un-recorded, which
    would make an interrupted hold indistinguishable from a silently lost
    push.
    """
    git_dir = resolve_git_common_dir(repo_root)
    try:
        git_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    target = git_dir / _PENDING_RECORD_NAME
    tmp = git_dir / f"{_PENDING_RECORD_NAME}.tmp-{os.getpid()}"
    record = {
        "branch": branch,
        "sha": sha,
        "hold_until": hold_until,
        "holder_pid": holder_pid,
    }
    try:
        tmp.write_text(json.dumps(record), encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _remove_pending_record(repo_root: str) -> None:
    """Remove the pending-push record -- only ever called after a
    successful push (AC14: "removed only after a successful push").
    Missing-file is not an error (idempotent, safe to call speculatively).
    """
    try:
        _pending_record_path(repo_root).unlink()
    except OSError:
        pass


def _clear_pending_record_if_branch(repo_root: str, branch: str) -> None:
    """Remove the pending record iff it names `branch` -- called from every
    success site in `run_push_with_retry` (direct success and the resolved
    non-fast-forward race), never on a failed/exhausted push. A record for
    a DIFFERENT branch is left untouched -- it is another branch's unfinished
    business, not this call's to clear.
    """
    record = _read_pending_record(repo_root)
    if record is not None and record.get("branch") == branch:
        _remove_pending_record(repo_root)


def _holder_alive(pid) -> bool | None:
    """Best-effort holder-liveness check for the record's `holder_pid` --
    None means "could not determine" (e.g. `MissingPsutilError` on a
    psutil-less Windows box), in which case `_record_is_stale` falls back to
    the hold_until+grace check alone rather than raising.
    """
    try:
        return session_core.pid_alive(pid)
    except Exception:
        return None


def _record_is_stale(record: dict, now: float) -> bool:
    """A pending record is stale (dead holder, or hold_until long past) --
    taken over, never trusted (AC14). Checked before either coalescing onto
    an existing record or draining one.
    """
    alive = _holder_alive(record.get("holder_pid"))
    if alive is False:
        return True
    hold_until = record.get("hold_until")
    if isinstance(hold_until, (int, float)) and now > hold_until + _STALE_GRACE_SECONDS:
        return True
    return False


def _shared_branch_live_count(repo_root: str, branch: str) -> int | None:
    """Count live sessions whose `meta.json:branch` equals `branch`.

    ### DECISION (C7, PM-required -- stated here per the plan body since this
    chunk cannot edit the plan doc itself): the >1-live-session predicate is
    **branch-scoped**, not repo-wide. `live_session_ids()` is branch-blind
    (it enumerates every live session in the repo's session hub, not per
    branch); the only per-session branch signal is `meta.json:branch`,
    read today only by `pickup_assemble/holder_evidence.py`. Branch-scoped
    was chosen because this chunk's own title and the incident it fixes are
    both specifically about a SHARED BRANCH -- a repo-wide count would hold
    an auto-push back whenever ANY two sessions are live anywhere in the
    repo, including two sessions on two disjoint branches that can never
    race each other on origin, which is not the hazard AC14 describes.
    Measured on this repo 2026-08-03: sessions routinely run several live
    peers across unrelated `work/*` branches, so a repo-wide predicate would
    hold back the common case, not just the shared-branch one.
    A peer whose `meta.json:branch` is empty/unreadable is NOT counted as
    sharing `branch` (the unknown-branch case degrades toward publishing,
    per the plan body's explicit instruction) -- an undercount here only
    means this branch pushes a little earlier than a perfectly-informed
    predicate would, never that a solo branch is held back forever.

    Returns None (not a count) if the predicate itself could not be
    resolved (sessions root walk raised) -- AC14a precondition (3) unmet;
    the caller pushes immediately rather than guessing.
    """
    try:
        sessions_root = session_core.sessions_dir(repo_root)
        if not sessions_root:
            return 0
        live_ids = session_liveness.live_session_ids(repo_root)
        count = 0
        for sid in live_ids:
            sdir = str(Path(sessions_root) / sid)
            peer_branch = session_core.read_meta_field(sdir, "branch")
            if peer_branch and peer_branch == branch:
                count += 1
        return count
    except Exception:
        return None


def _peer_commit_within_window(repo_root: str, branch: str, now: float) -> bool | None:
    """True if a commit from a session OTHER than this process's own landed on
    `branch` within the last `_HOLD_WINDOW_SECONDS`.

    ### WHAT THIS IS AND IS NOT. This is a base-rate PROXY for "the hold's
    retraction promise will be gone before the hold finishes sleeping" -- it is
    NOT a measurement of that. It cannot be: at the moment `_hold_window` runs,
    the commit being protected IS the branch tip, so it has no followers yet by
    construction, and the followers that would destroy retractability are in the
    future. A descendant walk here would therefore return "no followers" always
    and hold always, which is exactly the behaviour this predicate exists to
    narrow. So it measures recent PEER ACTIVITY on the branch and infers the
    rest from the base rate below.

    Known bias, stated because the name of the thing it proxies is stronger than
    what it observes: peer commits inside the window are counted whether or not
    they are ancestors of the commit being protected. A peer commit at T=0 on an
    otherwise idle branch, ours at T=100, causes the hold to be skipped even
    though ours is genuinely still retractable. The skip is the safe direction
    (publish sooner), and a branch with any peer commit in the last five minutes
    is one where a follower inside the next five is the overwhelming base case --
    but "a quiet shared branch still holds" is true of a branch quiet for the
    whole window, not of one that merely has no followers yet.

    Commits with no Session-Id trailer are not counted as peer commits. Plumbing
    commits drop that trailer, so this under-detects peers and, where it does,
    errs toward holding -- the pre-existing behaviour.

    ### DECISION (measured 2026-08-20, work/machine-a/2026-08-18to20, trailing
    36h): 2079 commits, median inter-commit gap 20s -- a median of 10 peer
    commits land on top of any given commit within the 300s hold window, and
    only 6 of 2079 commits had zero followers in that window. The hold's
    stated purpose (see the section comment above this function) is to avoid
    publishing a trivially-reversible bad commit within ~60s of it landing --
    a retraction window. On a branch this busy that window doesn't exist: by
    the time the hold would wake, peers have already built on the commit it
    was protecting, and rewinding it would rewrite their work too. The
    `_shared_branch_live_count(...) > 1` trigger inverts under load --
    sharing is exactly what makes retraction impossible, not what threatens
    it -- so this predicate narrows the hold toward the case where holding can
    still plausibly deliver on its promise.

    Does exactly ONE bounded `git log --since=...` call -- no per-commit
    spawning; this repo's amplification gate
    (`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`)
    forbids that shape. Reuses the `%x1f`-delimited Session-Id trailer format
    `session_attribution.py` already established
    (`bulk_trailer_session_map`/`trailer_foreign_shas`) rather than inventing
    a new parsing shape -- those functions aren't called directly because
    both append a single revision-arg string after the format flag, and that
    slot can't also carry this call's `--since=...` bound.

    Returns None -- not False -- if the predicate could not be evaluated
    (this process's own session id is unresolvable, or the `git log` call
    fails/times out). `_hold_window` treats None as "push", matching AC14a's
    existing fail-toward-publishing posture for every other precondition
    here: a predicate that holds when it can't prove retraction is already
    lost would reintroduce the exact starvation this function removes.
    """
    own_session_id = session_core.resolve_session_id(repo_root)
    if not own_session_id:
        return None
    since_iso = datetime.fromtimestamp(
        now - _HOLD_WINDOW_SECONDS, tz=timezone.utc
    ).isoformat()
    output = _run_git(
        repo_root,
        [
            "log", "--no-merges",
            f"--since={since_iso}",
            "--format=%H%x1f%(trailers:key=Session-Id,valueonly)",
            branch,
        ],
    )
    if output is None:
        return None
    for line in output.splitlines():
        if "\x1f" not in line:
            continue
        _, trailer = line.split("\x1f", 1)
        trailer = trailer.strip()
        if trailer and trailer != own_session_id:
            return True
    return False


def _hold_window(repo_root: str, branch: str) -> bool:
    """Decide whether `run_push_with_retry` should push NOW or has already
    delegated to an existing holder.

    Returns True when the caller should proceed to attempt the push
    immediately after this call returns -- covering four cases: (a) the
    branch is solo (<=1 live session sharing it), (b) the live-session
    predicate could not be resolved (fail toward publishing -- AC14a
    precondition 3 unmet), (b2) the branch IS shared and a foreign-session
    commit landed within the hold window, which `_peer_commit_within_window`
    treats as a base-rate proxy for the retraction promise being gone before
    the hold could finish sleeping (see that function for what the proxy does
    and does not observe), and (c)
    this call became the new/takeover holder, slept out its window (or
    skipped the sleep under the test seam), and now owns the push.

    Returns False only when a LIVE, non-stale pending record already covers
    this exact branch (AC14a precondition 2 unmet) -- this call exits
    without pushing at all; the incumbent holder publishes the branch tip
    at wake time, a superset of what this call would have pushed (AC14's
    coalescing-token contract), so a second commit inside one window never
    stacks a second sleeper.

    AC14a precondition (1) -- the record was written and read back
    successfully -- is enforced by `_write_pending_record`'s return value:
    a failed write pushes immediately rather than sleeping un-recorded.

    Precondition (4) -- "the platform's detach was verified at install
    time" -- REMAINS a docstring argument, not a runtime boolean, after
    deliberately trying and rejecting three concrete candidates (2026-08-03,
    AC14a chunk):
      - A "was this call reached via a detached child" env marker, set by
        `_detach_and_run`'s fork child / Windows respawn and by
        `spawn_detached_push`'s respawn, checked here. Rejected: every
        existing hold-window test (`test_hold_window_*`,
        `test_no_sleep_env_skips_backoff_but_not_the_hold_decision`, the
        loss-path and coalescing tests) calls `_hold_window` /
        `run_push_with_retry` DIRECTLY, in the test process, with no
        marker set and without going through `_detach_and_run` or
        `spawn_detached_push` at all -- a gate that requires the marker
        would read every one of those calls as precondition-(4)-unmet and
        skip straight to an immediate push, silently deleting the
        write-then-sleep behavior those tests pin. The HARD CONSTRAINT that
        this coverage keep passing UNCHANGED rules this candidate out; the
        alternative (special-casing "am I under pytest" in production code)
        is not a real precondition, it is a test detector wearing one.
      - `os.getsid(0) == os.getpid()` (POSIX session-leader check, true
        after the fork child's own `os.setsid()`). Rejected for the same
        reason: unrelated to whether pytest's own process happens to be a
        session leader on a given CI box/terminal, so it is not a stable
        signal either way, and a flaky gate is worse than a documented one.
      - A literal install-time artifact (e.g. a stamped file written by the
        installer after running the wire-path push-to-a-real-remote check
        `test_wire_path_respawn_actually_pushes_to_a_real_remote` exercises
        in-repo). Rejected because no such artifact exists anywhere in this
        install chain today (`docs/install/agent-install-manifest.json`,
        `scripts/setup.py` -- neither stamps or checks one), and inventing
        one is explicitly out of scope for this chunk.
    Conclusion: this function only ever runs inside the already-detached
    child in PRODUCTION (this call site is the HEAD of
    `run_push_with_retry`, which itself only ever runs post-detach or under
    the explicit `COORDINATOR_AUTO_PUSH_SYNC` test seam), and that detach's
    own import/PYTHONPATH correctness is `_respawn_env`'s contract (fixed
    2026-08-03, prerequisite to this chunk). There is no runtime signal that
    distinguishes a legitimate direct call (test, or a future in-process
    caller under the sync seam) from an illegitimate one without either
    breaking existing coverage or faking a test-only backdoor -- precondition
    (4) stays a structural/install-time argument, not a fourth gated boolean.

    NEVER called from `branch_gate()` -- that runs in the PARENT process
    that `git commit` waits on synchronously; a hold there would block
    every commit for the full window. This lives at the head of
    `run_push_with_retry` instead, so the `--branch` engine path (which
    skips `branch_gate()` entirely per DEC-1) inherits the same behaviour
    for free.
    """
    now = time.time()
    live_count = _shared_branch_live_count(repo_root, branch)
    if live_count is None or live_count <= 1:
        return True

    existing = _read_pending_record(repo_root)
    if (
        existing is not None
        and existing.get("branch") == branch
        and not _record_is_stale(existing, now)
    ):
        return False

    # Deliberately BELOW the live-record check: an incumbent holder's record is
    # AC14's coalescing token, and `run_push_with_retry`'s success path clears
    # it. Testing this predicate first let a hold-skipping call push and unlink
    # a still-sleeping incumbent's record, leaving nothing for drain_pending_push
    # or boot_sweep to take over if that incumbent was then killed.
    #
    # `is not False`, not a truthy check: None (predicate unresolvable) reads
    # the same as True here -- both push immediately. Only an explicit False
    # (no peer commit in the window, proven by a clean git log read) falls
    # through to the hold logic below.
    if _peer_commit_within_window(repo_root, branch, now) is not False:
        return True

    sha = _run_git(repo_root, ["rev-parse", branch])
    hold_until = now + _HOLD_WINDOW_SECONDS
    wrote = _write_pending_record(repo_root, branch, sha, hold_until, os.getpid())
    if not wrote:
        return True

    if not _no_sleep():
        time.sleep(_HOLD_WINDOW_SECONDS)

    return True


def _branch_resolves_locally(repo_root: str, branch: str) -> bool:
    """True if `branch` still names a local ref -- `git rev-parse --verify
    refs/heads/<branch>` succeeds. Used by `drain_pending_push` to detect a
    record left behind by a branch rename (or any other disappearance of
    the local ref) BEFORE attempting the push (AC4), rather than
    discovering it only after `push_once` fails with `dead-ref`.
    """
    return _run_git(repo_root, ["rev-parse", "--verify", f"refs/heads/{branch}"]) is not None


def _drain_dead_ref_record(repo_root: str, record: dict, branch: str) -> None:
    """Resolve a pending record whose `branch` no longer resolves as a
    local ref (AC5-AC7) -- the closed loop the record's own presence would
    otherwise drive forever: once `hold_until` has passed, `due` stays true
    on every later commit, so without this the record would be re-drained
    (and, pre-AC1/AC2, re-failed into push-failures.log) on every commit
    from here on.

    `sha` can itself be `None` -- `wsc_tail._deferred_publisher_backstop`
    writes its record before the commit it backstops has landed, so at
    write time there is genuinely no sha to pin yet (see that function's
    own docstring). That case is branched on FIRST, separately from the
    "sha pinned" cases below (review: coordinator:review-code, Finding 1,
    2026-08-19 -- a `sha=None` record used to fall into the same predicate
    as "no payload was ever queued" and get dropped outright here,
    silently losing the backstop's obligation on the exact compound case
    -- swallowed step-5e spawn AND an intervening branch rename before the
    next drain -- it exists to cover):
      0. `sha is None` -- unknown payload, not "no payload". If a current
         branch resolves, retarget the record onto it (same shape as case
         2 below, just without a specific sha to check ancestry for) and
         push: whatever the current branch's tip is now includes whatever
         this record was backstopping, if anything landed at all, and
         `push_once` is a harmless no-op ("everything up to date") if nothing
         did. Only with NO current branch to retarget onto -- nothing left
         that could hold the payload -- does this drop.
      1. `sha` pinned and already reachable from origin's copy of the
         CURRENT branch (typically the rename's own push, which already
         carried the commit) -- the queued push has already landed by some
         other path. Drop, stderr note only (AC6). Checked before case 2:
         if the commit is already safely on origin, there is nothing left
         to retarget-and-push, even if it also happens to be locally
         reachable from the current branch.
      2. `sha` pinned and reachable from the CURRENT local branch (not yet
         on origin) -- the commits moved with the rename and the queued
         push is still wanted, just misaddressed. Re-target the record onto
         the current branch via `_write_pending_record` and push it (AC5);
         that push's own success clears the just-rewritten record via
         `_clear_pending_record_if_branch`, same as any other successful
         push.
      3. Reachable from nowhere this function can check -- a genuine loss
         risk, not a rename artifact. ONE loud `push-failures.log` row
         naming the orphaned sha, then drop (AC7): retrying can never
         succeed (the ref that named it is gone and no live branch carries
         it), so looping would only convert a real signal into noise.
    """
    sha = record.get("sha")
    current_branch = resolve_branch(repo_root)

    if sha is None:
        if current_branch:
            retargeted = _write_pending_record(
                repo_root,
                current_branch,
                None,
                record.get("hold_until", time.time()),
                record.get("holder_pid", os.getpid()),
            )
            if retargeted:
                print(
                    f"coordinator-auto-push: pending push for {branch} "
                    f"(no sha pinned yet) re-targeted to {current_branch} "
                    "(branch rename) -- pushing.",
                    file=sys.stderr,
                )
                run_push_with_retry(repo_root, current_branch, _skip_hold=True)
                return
            # _write_pending_record failed -- same AC14a precondition-(1)
            # reasoning as case 2's own fallthrough below: never trust a
            # failed write, fall through to drop-with-note rather than
            # silently losing an unknown payload.
        print(
            f"coordinator-auto-push: dropping pending push for {branch} -- "
            "branch no longer resolves locally, no commit was pinned yet, "
            "and no current branch exists to retarget onto; nothing to retry.",
            file=sys.stderr,
        )
        _remove_pending_record(repo_root)
        return

    if current_branch and _is_superseded(repo_root, current_branch, sha):
        print(
            f"coordinator-auto-push: dropping pending push for {branch} -- "
            "branch no longer resolves locally and the commit is already "
            "on origin; nothing to retry.",
            file=sys.stderr,
        )
        _remove_pending_record(repo_root)
        return

    if current_branch and _is_ancestor(repo_root, sha, current_branch):
        retargeted = _write_pending_record(
            repo_root,
            current_branch,
            sha,
            record.get("hold_until", time.time()),
            record.get("holder_pid", os.getpid()),
        )
        if retargeted:
            print(
                f"coordinator-auto-push: pending push for {branch} "
                f"re-targeted to {current_branch} (branch rename) -- "
                "pushing.",
                file=sys.stderr,
            )
            run_push_with_retry(repo_root, current_branch, _skip_hold=True)
            return
        # _write_pending_record failed (AC14a's own precondition-(1)
        # contract: a failed write is never trusted) -- fall through to the
        # orphaned-report path below rather than silently dropping a
        # payload that IS still reachable somewhere, so the loss is at
        # least reported once instead of vanishing unlogged.

    log_failure(
        repo_root,
        branch,
        "drain",
        "dead-ref-orphaned",
        1,
        f"pending push for {branch} orphaned: sha {sha!r} unreachable from "
        "the current local branch or origin; commits may be lost",
        "",
    )
    _remove_pending_record(repo_root)


def drain_pending_push(repo_root: str) -> None:
    """Drain point for the durable pending-push record (AC14).

    Idempotent and best-effort -- safe to call unconditionally on every
    invocation of this module. Wired at the head of `run_push_with_retry`
    (NOT `main()`), which every commit's own post-commit hook already
    reaches via its detached respawn -- so the NEXT commit fires this for
    free, with no new cross-repo surface and no additional host.

    Three independent call sites now reach this function, matching AC14's
    safety argument exactly:
      1. The head of `run_push_with_retry` (below) -- every commit's own
         post-commit hook already respawns a detached child that ends up
         here for ITS OWN branch, so the next commit drains any due/stale
         record for free.
      2. Session start -- `coordinator_core.ops.session.boot_sweep`'s
         handler calls this unconditionally as one of its boot-time
         sweeps, so a session that starts inside (or after) a missed
         hold-window's `hold_until` drains it before doing anything else.
      3. Workday-start push-health -- `coordinator_core.ops.
         workday_drain_pending_push` ("workday.drain_pending_push") is the
         mutating sibling of the pure-read `workday.
         surface_auto_push_failure_stats` op (which stays a zero-write
         idempotent read per its own ratified contract -- see that
         module's docstring). `/workday-start` invokes both: the read for
         the failure-count surface, this op for the drain.
    A missed drain is now recovered at the NEXT of any of these three
    independent points, not merely the next commit -- "delay, never lose"
    holds against session crash/sleep/power-loss as the AC's safety
    argument requires, not just against "no further commits happen."

    A record is only actioned once its hold window has elapsed (`hold_until`
    reached) or its holder is confirmed dead (`_record_is_stale`) -- an
    in-window, live-holder record is left alone; draining it early would
    just race the incumbent's own wake-and-push.

    Before pushing, the record's `branch` is checked for local resolvability
    (`_branch_resolves_locally`, AC4) -- a branch that no longer resolves
    (most commonly: renamed out from under the record by
    `workday-start-step0`'s midnight rename) is handed to
    `_drain_dead_ref_record` instead of `run_push_with_retry`, which would
    otherwise fail every attempt with `dead-ref` and, since `due` stays true
    on every commit once `hold_until` has passed, do so forever (AC5-AC7).

    When the branch DOES resolve, behavior is unchanged from before AC4-AC7:
    pushed synchronously via `run_push_with_retry(..., _skip_hold=True)` --
    bypassing `_hold_window` entirely, since this call IS the drain, not a
    new hold decision -- and that call's own success path removes the
    record (`_clear_pending_record_if_branch`), so a push that fails here
    leaves the record in place for the next drain point to retry, exactly
    the "delay, never lose" contract this record exists to provide.
    """
    try:
        record = _read_pending_record(repo_root)
        if record is None:
            return
        branch = record.get("branch")
        if not isinstance(branch, str) or not branch:
            _remove_pending_record(repo_root)
            return
        now = time.time()
        hold_until = record.get("hold_until")
        due = isinstance(hold_until, (int, float)) and now >= hold_until
        if not due and not _record_is_stale(record, now):
            return
        if not _branch_resolves_locally(repo_root, branch):
            _drain_dead_ref_record(repo_root, record, branch)
            return
        run_push_with_retry(repo_root, branch, _skip_hold=True)
    except Exception:
        pass


def run_push_with_retry(repo_root: str, branch: str, *, _skip_hold: bool = False) -> None:
    """Attempt the push up to MAX_ATTEMPTS times, retrying retryable classes
    with a class-appropriate backoff. Logs a forensic failure entry and
    returns (never raises) if all attempts are exhausted or a non-retryable
    class is hit. Test seam: COORDINATOR_AUTO_PUSH_NO_SLEEP=1 skips backoff
    sleeps so the test suite doesn't pay the seconds-scale gh-transient wait.

    non-fast-forward is handled inline, not via `_RETRYABLE_CLASSES`: each
    rejection is first checked against `_is_superseded` (read-only fetch +
    ancestor test) before deciding whether to retry or give up, rather than
    blindly resending the same push (see the "Retry policy" module comment
    above `_backoff_seconds` for why this class needs a different retry
    shape than ref-lock/network/gh-transient).

    On a SUCCESSFUL push -- either the direct-success path or a
    non-fast-forward race that turns out already resolved -- this also fires
    `_maybe_publish_cockpit_contract`, gated by the cheap `_cockpit_publish_script`
    filesystem check computed once up front (see that function's docstring
    for why the guard must be a stat, not a git call, and why it makes this
    a no-op in every repo but DoE-claude). Never fires on failure or on a
    skipped push -- both of this function's `return` sites for a failed/
    exhausted push are left untouched.

    Head-of-function hold (AC14): unless `_skip_hold` is set (the drain
    path, `drain_pending_push`, which IS the hold's own resolution, not a
    new hold decision), `_hold_window` decides whether to proceed now or
    exit immediately because a live peer record already covers this branch
    -- see that function's docstring for the full contract. This is
    deliberately NOT in `branch_gate()`: that runs in the PARENT process
    `git commit` waits on synchronously, and a multi-minute hold there would
    block every commit for the window; this function only ever runs in the
    already-detached child (or under the `COORDINATOR_AUTO_PUSH_SYNC` test
    seam), so the sleep costs nothing the caller is waiting on. Both
    success sites below clear the pending record via
    `_clear_pending_record_if_branch` -- removed only after a push that
    actually succeeded (AC14), never on failure or a retry-in-progress.
    """
    if not _skip_hold:
        # The free drain point (AC14): every commit's own post-commit hook
        # already respawns a detached child that ends up here for ITS OWN
        # branch -- draining any due/stale record (this branch's own from a
        # crashed prior hold, or another branch's leftover) piggybacks on
        # that spawn at zero extra process cost. `_skip_hold=True` marks
        # the drain's OWN nested call, so this can never recurse: the drain
        # path pushes once and returns, it never drains again.
        #
        # `pending_before` is read BEFORE the drain to detect the case
        # where the record drain just actioned is THIS call's own branch
        # (review: coordinator:review-code, Finding 2, 2026-08-19): when
        # `wsc_tail._deferred_publisher_backstop` writes an already-due
        # record moments before spawning this very push, `drain_pending_push`
        # below reads it back, does a nested `_skip_hold=True` push, and
        # clears it on success -- all before `_hold_window` gets a look.
        # Without this check, `_hold_window` then finds no record for
        # `branch`, mistakes this outer call for a brand-new holder on a
        # shared branch, and writes a fresh record + sleeps out a fully
        # redundant `_HOLD_WINDOW_SECONDS` for a push that already landed.
        pending_before = _read_pending_record(repo_root)
        drain_pending_push(repo_root)
        if (
            pending_before is not None
            and pending_before.get("branch") == branch
            and _read_pending_record(repo_root) is None
        ):
            # The drain just cleared (pushed) or dropped (already-superseded
            # / orphaned) the record for THIS branch -- either way there is
            # nothing left for this call to publish or hold for, so return
            # rather than re-entering `_hold_window` as a phantom fresh
            # holder. A record still present here (failed push, or a live
            # incumbent's own untouched hold) falls through to the normal
            # `_hold_window` decision below, unchanged.
            return
        if not _hold_window(repo_root, branch):
            return

    windows_bash = is_windows_bash()
    # `git remote get-url origin` used to run here to derive `ssh_remote`. NOTHING
    # consumes that flag any more, on any path: the 2026-08-06 no-shell-spawns
    # ruling deleted the PowerShell transport, and both `push_once` and
    # `route_label` now open with `del windows_bash, ssh_remote` -- the latter
    # returning the constant "direct push". So the probe was one git subprocess
    # per push, on the commit hot path, feeding a value that could not reach any
    # output. Removed rather than left as an unread computation; the seams and
    # signatures around it are unchanged, since they are still test surface.
    ssh_remote = False
    route = route_label(windows_bash, ssh_remote)
    local_sha = _run_git(repo_root, ["rev-parse", branch])

    # Cheap filesystem stat, evaluated once, BEFORE any git call this seam
    # would otherwise add -- this is what keeps the common case (a repo with
    # no cockpit-contract publish script at all) at zero extra subprocess
    # cost. Only when this resolves non-None do we pay for the additional
    # `rev-parse` below.
    cockpit_script = _cockpit_publish_script(repo_root)
    old_remote_sha = (
        _run_git(repo_root, ["rev-parse", f"refs/remotes/origin/{branch}"])
        if cockpit_script is not None
        else None
    )

    attempt = 1
    while attempt <= MAX_ATTEMPTS:
        succeeded, stderr_text = push_once(repo_root, branch, windows_bash, ssh_remote)
        if succeeded:
            _clear_pending_record_if_branch(repo_root, branch)
            if cockpit_script is not None:
                _maybe_publish_cockpit_contract(repo_root, cockpit_script, old_remote_sha, local_sha)
            return

        err_class = classify_error(stderr_text)

        if err_class == "dead-ref":
            # Not in _RETRYABLE_CLASSES (AC2) -- a dead local branch ref
            # cannot self-heal by resending the same push, so report once
            # and stop rather than falling through to the generic
            # log_failure() path below (AC3). See log_dead_ref_failure's
            # docstring for why this stays out of push-failures.log.
            log_dead_ref_failure(repo_root, branch, route, attempt, stderr_text)
            return

        if err_class == "non-fast-forward":
            if local_sha and _is_superseded(repo_root, branch, local_sha):
                _clear_pending_record_if_branch(repo_root, branch)
                log_race_resolved(repo_root, branch, route, attempt)
                if cockpit_script is not None:
                    _maybe_publish_cockpit_contract(repo_root, cockpit_script, old_remote_sha, local_sha)
                return

            if attempt < MAX_ATTEMPTS:
                print(
                    f"coordinator-auto-push: race on {branch} (non-fast-forward, "
                    f"attempt {attempt}/{MAX_ATTEMPTS}) -- retrying",
                    file=sys.stderr,
                )
                if not _no_sleep():
                    time.sleep(_backoff_seconds(err_class, attempt))
                attempt += 1
                continue

            # Retries exhausted and still not superseded -- a genuine,
            # unrecoverable-without-rebase divergence. Fail loud, as before.
            first_err = extract_first_err(stderr_text)
            log_failure(repo_root, branch, route, err_class, attempt, first_err, stderr_text)
            return

        if attempt < MAX_ATTEMPTS and err_class in _RETRYABLE_CLASSES:
            if not _no_sleep():
                time.sleep(_backoff_seconds(err_class, attempt))
            attempt += 1
            continue

        first_err = extract_first_err(stderr_text)
        log_failure(repo_root, branch, route, err_class, attempt, first_err, stderr_text)
        return

    # Unreachable in practice (the loop always returns inside), kept for
    # clarity/defense-in-depth matching the bash `while` fallthrough.
    return


# ---------------------------------------------------------------------------
# Async self-detach
# ---------------------------------------------------------------------------
#
# The bash shim previously did `nohup bash "$SCRIPT" … &` for async push (so
# the commit returns immediately on large repos). The new sh shim `exec`s
# python directly with NO shell `&`, so async must live INSIDE this module.
#
# POSIX: os.fork() -- parent returns immediately (commit hook exits), child
# performs the push and exits on its own. Windows has no fork(); instead we
# re-spawn ourselves via subprocess.Popen with DETACHED_PROCESS |
# CREATE_NEW_PROCESS_GROUP and pass --no-async (via env seam) so the child
# runs synchronously while the parent returns immediately.
#
# This is the part of the port most needing live Windows verification -- the
# spike handoff (state/handoffs/2026-07-15_164501_auto-push-naked-python-reimpl.md)
# names it explicitly. Gated behind --async/--no-async / COORDINATOR_AUTO_PUSH_SYNC
# so tests always run synchronously (no forking under pytest).
# ---------------------------------------------------------------------------

def _resolve_python_exe() -> str | None:
    """Resolve the interpreter to respawn with, shared by both respawn-Popen
    call sites (`_detach_and_run`'s Windows leg and `spawn_detached_push`) so
    a future change to the fallback order can't be applied to only one site.
    """
    import shutil

    return sys.executable or shutil.which("python3") or shutil.which("python")


def _claude_klabauter_package_root() -> str:
    """Directory containing the `coordinator_core` package, for the
    respawned child's PYTHONPATH.

    Both respawn call sites launch the child by resolved ABSOLUTE SCRIPT
    PATH, never `-m` (see each call site's comment for why `-m` is unsafe:
    the hook fires with cwd = the committing fleet repo, not this one, and a
    module-relative import resolves against the caller's cwd). Running the
    script directly puts only `coordinator_core/hooks/` on `sys.path[0]`, so
    `coordinator_core` itself is not importable regardless of the child's
    cwd -- this is the root cause of the 2026-08-01 regression
    (ModuleNotFoundError at the top-level `from coordinator_core.git...`
    import, silently swallowed because the detached child's stderr is
    DEVNULL'd). Injecting this repo's root into PYTHONPATH fixes the import
    without touching the `-m` invariant the existing regression tests
    protect.
    """
    return str(Path(__file__).resolve().parents[2])


def _respawn_env() -> dict:
    """Build the respawned child's environment: sync seam + PYTHONPATH.

    Shared by both respawn-Popen call sites (mirrors `_resolve_python_exe`
    and `_windows_detached_flags`'s own sharing rationale) so a future change
    to one can't silently diverge from the other. Prepends
    `_claude_klabauter_package_root()` ahead of any pre-existing `PYTHONPATH` --
    never replaces it -- so a fleet repo that already sets its own
    PYTHONPATH for unrelated reasons keeps that value reachable too.
    """
    env = dict(os.environ)
    env[_ENV_SYNC] = "1"
    package_root = _claude_klabauter_package_root()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        package_root if not existing else f"{package_root}{os.pathsep}{existing}"
    )
    return env


def _open_respawn_stderr_log(repo_root: str, branch: str):
    """Open a durable, append-only sink for the respawned child's stderr.

    The respawned child's stdio is otherwise entirely DEVNULL'd by design
    (see `_disown_stdio`'s docstring for the pipe-hold hazard that mandates
    disowning it) -- which is exactly what let the 2026-08-01
    ModuleNotFoundError regression above run silently for two days: the
    child died at import, its exit code was never observed (the parent
    returns immediately for push-time latency), and its stderr went nowhere
    an operator could see. Redirecting stderr to a plain FILE instead of a
    PIPE or DEVNULL gives the next invocation or an operator a durable trail
    without console noise -- and unlike a PIPE, a FILE is never read back by
    this process, so it can never reintroduce the pipe-hold defect
    `_disown_stdio` guards against (a full OS pipe buffer blocking the
    child). Lives alongside `push-failures.log` in the git COMMON dir (same
    `resolve_git_common_dir` target, so linked worktrees/submodules share one
    log). Returns None (caller falls back to DEVNULL) if the log can't be
    opened -- best-effort only; must never block or fail the respawn.
    """
    try:
        git_dir = resolve_git_common_dir(repo_root)
        git_dir.mkdir(parents=True, exist_ok=True)
        log_path = git_dir / "auto-push-respawn-stderr.log"
        fh = open(log_path, "a", encoding="utf-8", newline="\n")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fh.write(f"--- respawn {stamp} branch={branch} ---\n")
        fh.flush()
        return fh
    except OSError:
        return None


def _windows_detached_flags() -> int:
    """Compose the Windows-only detached-process creation flags, shared by
    both respawn-Popen call sites (see `_resolve_python_exe`'s docstring for
    why this is factored out rather than duplicated).

    negative-spec -- DETACHED_PROCESS MUST NOT be reintroduced here, and
    ORing it with CREATE_NO_WINDOW is NOT a middle ground: Win32 documents
    CREATE_NO_WINDOW as IGNORED whenever DETACHED_PROCESS or
    CREATE_NEW_CONSOLE is also set. This function carried exactly that
    combination and therefore read as console-suppressed while behaving as
    bare DETACHED_PROCESS -- measured as 6 visible `conhost.exe` windows
    across 3 spawns, versus 0 once DETACHED_PROCESS was dropped.

    The mechanism is inheritance, and it is why one flag fixes a whole
    subtree: DETACHED_PROCESS leaves the child with no console, so every
    descendant (`git`, each hook interpreter) allocates its own WINDOWED
    console. CREATE_NO_WINDOW gives the child a WINDOWLESS console, which
    descendants inherit and never need to reallocate.

    Detached lifetime is preserved and was measured, not assumed -- Windows
    does not reap children on parent exit, so the respawned child still
    outlives a hard-killed parent. Ctrl-C isolation stays with
    CREATE_NEW_PROCESS_GROUP.

    Measurement: `state/audits/2026-08-21-detached-process-console-window-storm.md`.
    """
    subprocess = _subprocess()
    flags = 0
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _disown_stdio() -> None:
    """Redirect the fork child's stdin/stdout/stderr to os.devnull.

    Without this, the child inherits the parent `git commit` process's
    stdout/stderr pipes. The engine's `_git()` runs `subprocess.run(...,
    capture_output=True)`, which blocks reading those pipes until EOF -- so a
    detached-in-name-only child that still holds the write end of the pipe
    open keeps `git commit` blocked for its entire push+retry lifetime. Never
    redirect to the forensic log (`.git/push-failures.log`): that file is
    written by `log_failure()`'s structured appends, and raw child stdio
    pointed at it would interleave and corrupt those writes.
    """
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    try:
        for fd in (0, 1, 2):
            os.dup2(devnull_fd, fd)
    finally:
        if devnull_fd > 2:
            os.close(devnull_fd)


def _detach_and_run(repo_root: str, branch: str) -> None:
    """Run the push asynchronously so the calling commit hook returns immediately."""
    if hasattr(os, "fork"):
        pid = os.fork()
        if pid > 0:
            # Parent: return immediately, letting the commit hook exit.
            return
        # Child: detach from the controlling terminal/session where possible,
        # then perform the push and exit without propagating back to the
        # original hook process.
        try:
            os.setsid()
        except (AttributeError, OSError):
            pass  # best-effort detach only (no setsid on Windows / already a session leader) -- push still runs either way
        try:
            _disown_stdio()
        except Exception as exc:
            # If stdio disown itself fails, do NOT let the exception unwind
            # into main()'s top-level except-Exception handler -- the child's
            # stdio hasn't been redirected yet, so that handler would print to
            # stderr and spawn a git subprocess while still holding the
            # parent's pipe open, reintroducing the exact pipe-hold defect
            # this chunk exists to close. Exit immediately instead. (Review:
            # code-reviewer -- Finding 4.)
            #
            # But exiting silently left this guard with zero forensic trail
            # (cross-repo/inbox/2026-07-23-claude-central-em-enum-parity-consumed-and-fork-child-silence-reply.md).
            # Log a row first -- route/err_class/attempts are hardcoded
            # literals, never computed via _run_git/route_label/is_windows_bash,
            # because a subprocess spawn here would itself hold the still-open
            # parent pipe for the push+retry lifetime, the exact defect above.
            # log_failure()'s own OSError fallback prints to sys.stderr (the
            # still-held parent pipe too) -- that's acceptable here because
            # it's a bounded two-line write immediately followed by
            # os._exit(1), not a pipe held open for a whole retry lifetime.
            try:
                try:
                    provenance = _module_provenance()
                except Exception:
                    provenance = "module=<unresolved> interp=<unknown> python=<unknown>"
                log_failure(
                    repo_root,
                    branch,
                    "direct push",
                    "unknown",
                    0,
                    f"internal error in _disown_stdio [{provenance}]: "
                    + f"{type(exc).__name__}: {exc}"[:200],
                    traceback.format_exc(),
                )
            except Exception:
                # Diagnostics must never be the thing that blocks os._exit(1).
                pass
            os._exit(1)
        try:
            run_push_with_retry(repo_root, branch)
        finally:
            os._exit(0)  # never returns -- child terminates here

    # Windows: no fork(). Re-spawn self as a fully detached child process with
    # the sync seam set, then return immediately from the parent.
    env = _respawn_env()
    detached_flags = _windows_detached_flags()
    python_exe = _resolve_python_exe()
    if not python_exe:
        # No interpreter to respawn with -- fall back to a synchronous run
        # rather than silently dropping the push.
        run_push_with_retry(repo_root, branch)
        return
    # Respawn by RESOLVED ABSOLUTE PATH, never `-m`. The hook fires in EVERY
    # fleet repo, whose cwd is the committing repo (NOT claude-klabauter), so
    # `-m coordinator_core.hooks.auto_push` raises ModuleNotFoundError there --
    # and because the respawn is a detached child, that failure is INVISIBLE:
    # the commit succeeds, the push never happens, nothing is logged. That is
    # the exact silent-no-op class this port exists to eliminate, and it is the
    # default path on Windows (async_mode defaults True). Verified empirically
    # 2026-07-20 on win32: `-m` from cwd=X:/example-retrieval-repo ->
    # "No module named 'coordinator_core'"; absolute path -> clean exit 0.
    # This mirrors the sh shim's own exec-by-abspath contract (Artifact A /
    # DoE cutover memo contract point 3) -- keep the two in agreement.
    #
    # 2026-08-01 follow-up regression: the module later grew its own
    # top-level `from coordinator_core.git.git_dir import
    # resolve_git_common_dir`, which the abspath respawn above cannot
    # satisfy on its own -- running THIS file directly puts only
    # `coordinator_core/hooks/` on `sys.path[0]`, so `coordinator_core`
    # itself isn't importable regardless of cwd. `_respawn_env()` fixes this
    # by injecting this repo's root into the child's PYTHONPATH (see its
    # docstring) rather than switching to `-m`, which would reintroduce the
    # cwd-dependence this comment already documents. Verified empirically
    # 2026-08-03: pre-fix, `python <abspath>` from any cwd ->
    # `ModuleNotFoundError: No module named 'coordinator_core'`, exit 1,
    # silently swallowed by the DEVNULL'd stderr below; post-fix, clean
    # exit 0. See `test_spawn_detached_push_child_survives_import_when_spawned_outside_claude_klabauter`.
    stderr_log = _open_respawn_stderr_log(repo_root, branch)
    subprocess = _subprocess()
    try:
        subprocess.Popen(
            [python_exe, os.path.abspath(__file__), "--repo-root", repo_root],
            env=env,
            # Detached child: never inherit the hook's stdin/stdout/stderr. An
            # inherited-but-invalid stdin handle under CREATE_NO_WINDOW hangs
            # _execute_child on nt (same trap coverage.py:122 documents); an
            # un-redirected stdout keeps the parent's pipes open across the
            # push+retry lifetime, blocking any capture_output=True reader of
            # the parent (the pipe-hold defect this chunk exists to close).
            # stderr goes to a durable FILE (see `_open_respawn_stderr_log`)
            # instead of DEVNULL -- a file, unlike a pipe, is never read back
            # by this process, so it carries none of that pipe-hold hazard,
            # while still surfacing an import-time death instead of hiding
            # it. Falls back to DEVNULL if the log couldn't be opened.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log if stderr_log is not None else subprocess.DEVNULL,
            creationflags=detached_flags,
            close_fds=True,
        )
    finally:
        if stderr_log is not None:
            stderr_log.close()


def spawn_detached_push(repo_root: str, branch: str) -> None:
    """Engine-facing detached-push entry: respawn-Popen leg on BOTH platforms.

    Reused by the wsc_tail deferred-push cutover (C3) so the engine never
    holds a synchronous push on its blocking path. Deliberately NOT
    os.fork()-based, unlike `_detach_and_run`'s legacy hook leg: the engine is
    a threaded asyncio process (to_thread pool), and fork-from-threaded is a
    known deadlock class (locks held by other threads are copied held --
    logging, malloc). A fork child would also inherit the engine's own
    JSON-RPC stdout pipe to its client, reintroducing the pipe-hold defect one
    layer up instead of closing it. A fresh interpreter respawn, fully
    disowned from stdin/stdout/stderr, sidesteps both hazards on every
    platform.

    `branch` is forwarded to the respawned child via `--branch` and pushed
    UNCONDITIONALLY -- `main()` skips `resolve_branch()`/`branch_gate()`
    entirely when `--branch` is present. This is what makes the entry point
    "deterministic and branch-gate-independent" per DEC-1
    (`docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md`): the caller
    already decided which branch to push, so re-deriving it from disk at
    child-spawn time would both race the caller's intent and silently
    re-apply the hook's `work/*`-only gate to a caller that never asked for
    one. (Review: code-reviewer -- Finding 1, `branch` argument was
    previously discarded on the primary respawn path.)
    """
    python_exe = _resolve_python_exe()
    if not python_exe:
        run_push_with_retry(repo_root, branch)
        return
    env = _respawn_env()
    popen_kwargs: dict = {}
    if hasattr(os, "fork"):
        # POSIX: start_new_session=True is the fork-free equivalent of
        # os.setsid() -- detaches the child from the parent's session so it
        # survives the parent (the engine's op handler) returning.
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = _windows_detached_flags()
    # Respawn by RESOLVED ABSOLUTE PATH, never `-m` -- mirrors the Windows
    # hook-leg respawn's own contract (see _detach_and_run's comment): a
    # module-relative import can silently fail to resolve depending on the
    # caller's cwd, and a detached child's failure is invisible. This entry
    # point runs on BOTH platforms (unlike `_detach_and_run`'s POSIX fork
    # leg), so it hit the 2026-08-01 top-level `coordinator_core` import
    # regression on macOS/Linux too, not just Windows -- `_respawn_env()`'s
    # PYTHONPATH injection fixes it here for the same reason.
    stderr_log = _open_respawn_stderr_log(repo_root, branch)
    subprocess = _subprocess()
    try:
        subprocess.Popen(
            [python_exe, os.path.abspath(__file__), "--repo-root", repo_root, "--branch", branch],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # Durable file, not DEVNULL -- see `_open_respawn_stderr_log`'s
            # docstring (same rationale as `_detach_and_run`'s Windows leg).
            stderr=stderr_log if stderr_log is not None else subprocess.DEVNULL,
            close_fds=True,
            **popen_kwargs,
        )
    finally:
        if stderr_log is not None:
            stderr_log.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _release_claims_for_head(repo_root: str) -> None:
    """Append the ``R`` (release) events for the commit that just landed.

    WHY THIS LIVES IN AUTO-PUSH, which is otherwise about pushing: the claim
    ledger must be released for EVERY commit route, not only the coordinator
    ops that call ``scope.release_committed_claims`` themselves
    (``scoped_git_commit``, ``consumed_handoff_stamp``,
    ``detached_render_commit``, ``post_commit_tail``). A plain ``git commit``
    -- an operator's, a tool's, any route bypassing those ops -- released
    nothing, so the claim stayed ``T`` forever and
    ``claim_index.commit_set`` kept offering an already-committed path back
    for commit. That is precisely the "you already committed these" answer the
    offer exists NOT to give.

    The post-commit hook is the one seam every route passes through, and this
    module is the only program that hook runs -- so folding the release in here
    costs ZERO additional processes on the commit hot path, where a second
    interpreter start would be break-class under CLAUDE.md's brightline. One
    ``git show`` spawn is added, inside a process that was starting anyway.

    Complementary, never double-releasing: the coordinator commit path sets
    ``_ENV_SUPPRESS_FOR_SYNC_PUSH``, which returns from ``main`` before this is
    reached, and that path already released its own claims. This covers exactly
    the routes that were missing it.

    Fail-open in every arm, like everything else here: a commit must never be
    blocked, and a stale claim is a far smaller harm than a failed commit.

    KNOWN NARROW GAP, measured rather than theorised: ``release_committed_claims``
    releases only paths that are CLEAN in the worktree, and a path git is still
    renormalizing line endings for can read as dirty for a moment right after
    the commit. Observed once here -- of two paths in one commit, one released
    and one did not; calling ``release_committed_claims`` on the straggler
    seconds later released it immediately, so the function was right and the
    timing was not. The failure mode is benign and self-correcting in effect:
    the claim stays ``T``, so the offer re-offers an already-committed path and
    the next commit of it is an empty no-op. Do NOT "fix" this by relaxing the
    clean check -- that check is what stops a release racing an unstaged edit.
    """
    try:
        from coordinator_core.session import core as _session_core
        from coordinator_core.session import scope as _session_scope

        sid = _session_core.resolve_session_id(repo_root)
        if not sid:
            return
        # `--format=` suppresses the header, leaving one path per line.
        out = _run_git(repo_root, ["show", "--name-only", "--format=", "HEAD"])
        if not out:
            return
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        if not paths:
            return
        # Releases only paths that are CLEAN in the worktree, and is
        # structurally incapable of releasing a peer's claim -- see
        # `release_committed_claims`' own docstring for both properties.
        _session_scope.release_committed_claims(sid, paths, cwd=repo_root)
    except Exception:
        # Never block a commit, and never let a diagnostic here become the
        # thing that does.
        pass


def main(argv: list[str] | None = None) -> int:
    """Entrypoint. ALWAYS returns 0 -- auto-push must never block a commit.

    Wraps the whole body in a broad except so no unexpected internal error
    escapes as a non-zero exit code; best-effort logs the error and still
    exits 0.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--async", dest="async_mode", action="store_true", default=None)
    parser.add_argument("--no-async", dest="async_mode", action="store_false")
    parser.add_argument("--repo-root", dest="repo_root", default=None)
    parser.add_argument("--branch", dest="branch", default=None)
    try:
        args, _unknown = parser.parse_known_args(argv)
    except SystemExit:
        # argparse's own error() path (near-unreachable here: all defined
        # flags are boolean/string with no type validation) -- covered by
        # this function's own "must never block a commit" contract.
        return 0

    branch = "<unknown>"
    try:
        if os.environ.get(_ENV_SUPPRESS_FOR_SYNC_PUSH):
            # The committing caller publishes this commit itself, in this same
            # invocation -- see `_ENV_SUPPRESS_FOR_SYNC_PUSH`. Stand down before
            # resolving anything: no detach, no pending record, no drain. Silent
            # by design, since this fires on every commit through the sanctioned
            # path and a per-commit line on the hot path is noise, not signal.
            return 0

        repo_root = args.repo_root or _resolve_repo_root()
        if not repo_root:
            return 0

        if args.branch is None:
            # Only on the genuine post-commit invocation, and BEFORE the branch
            # gate below -- the ledger must be released for a commit on a branch
            # this hook declines to push, exactly as for one it pushes.
            # `spawn_detached_push` respawns this same main() with an explicit
            # --branch; releasing there would repeat the work and add a spawn in
            # the detached child.
            _release_claims_for_head(repo_root)

        if args.branch:
            # Explicit branch from an engine-facing spawn
            # (spawn_detached_push's respawn) -- deterministic and
            # branch-gate-independent per DEC-1: the caller already decided
            # to push this branch, so skip resolve_branch()/branch_gate()
            # entirely rather than silently re-deriving and re-gating it.
            # (Review: code-reviewer -- Finding 1.)
            branch = args.branch
        else:
            branch = resolve_branch(repo_root)
            if not branch:
                return 0

            should_push, skip_message = branch_gate(branch)
            if skip_message:
                print(skip_message, file=sys.stderr)
            if not should_push:
                return 0

        async_mode = args.async_mode
        if async_mode is None:
            async_mode = not bool(os.environ.get(_ENV_SYNC))

        if async_mode:
            _detach_and_run(repo_root, branch)
        else:
            run_push_with_retry(repo_root, branch)
    except Exception as exc:
        # Best-effort: never let an internal error block the commit.
        try:
            provenance = _module_provenance()
        except Exception:
            # Diagnostics must never be the thing that blocks a commit.
            provenance = "module=<unresolved> interp=<unknown> python=<unknown>"
        print(f"coordinator-auto-push: internal error [{provenance}]: {exc}", file=sys.stderr)
        try:
            # Deliberately NOT `_resolve_repo_root()`: this is the cold error
            # path, reached once per failure, so it has no spawn to save --
            # and the walk resolves from the process cwd, whereas this handler
            # must name the repo the hook actually fired for, which the caller
            # may have supplied explicitly.
            repo_root_fallback = args.repo_root or _run_git(
                None, ["rev-parse", "--show-toplevel"]
            )
            if repo_root_fallback:
                log_failure(
                    repo_root_fallback,
                    branch,
                    "direct push",
                    "unknown",
                    0,
                    # Only the unbounded part (the exception message) is capped:
                    # the provenance prefix must survive truncation, since it is
                    # the field that identifies WHICH copy of this module ran.
                    f"internal error in auto_push.py [{provenance}]: "
                    + f"{type(exc).__name__}: {exc}"[:200],
                    traceback.format_exc(),
                )
        except Exception as log_exc:
            print(f"coordinator-auto-push: also failed to record the internal error: {log_exc}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
