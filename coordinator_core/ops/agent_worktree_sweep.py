"""
coordinator_core.ops.agent_worktree_sweep — find and (optionally) reap
agent-isolation git worktrees.

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Background: Claude Code 2.1.x auto-creates per-dispatch git worktrees under
<repo>/.claude/worktrees/agent-<hash>/ for backgrounded Agent dispatches.
These are documented opt-out-only behavior (no per-dispatch flag yet, see
anthropics/claude-code#58597). They persist locked until session deletion,
accumulating across days.

This module:
  1. Enumerates `git worktree list --porcelain` for paths matching
     .claude/worktrees/agent-*.
  2. Per worktree, classifies state vs. the calling repo's active branch:
       - empty-clean   : no commits ahead of the compare ref, no dirty files
       - dirty-benign   : dirty files, but every changed path is inside a
                           known auto-add allowlist (.claude/settings.local.json,
                           .last-cleanup), and no commits ahead
       - commits-clean : commits ahead, no dirty files (salvageable)
       - dirty         : uncommitted changes present outside the allowlist
                           (PM must handle)
  3. With --reap: removes empty-clean and dirty-benign worktrees; cherry-picks
     commits-clean worktrees onto the active branch then removes them; warns
     and leaves dirty worktrees alone.
  4. Emits one line per worktree (JSON or text, per --format) describing the
     worktree, its classified state, the action taken, and a detail string.

Liveness gate (S1b, workday-complete-ceremony-hardening spinoff, F4-adjacent
finding — state/audits/2026-07-23-destructive-git-op-liveness-gate-audit.md):
--reap's destructive branches (`git worktree remove --force`, `git branch -D`)
used to run on point-in-time file-state classification ALONE, with no session-
liveness check at all. A freshly-spawned agent worktree that has not yet
written a file classifies `empty-clean` and is indistinguishable from an
abandoned one — a misfire deletes a *running* agent's entire isolated tree.

KNOWN STRUCTURAL GAP (deliberate, not an oversight — do not "fix" by
inventing a mapping): there is no per-worktree owner-session mapping anywhere
in this codebase. `.claude/worktrees/agent-<hash>/` is created by Claude
Code's own platform for backgrounded Agent dispatches; it is NOT tracked
under this repo's `.git/coordinator-sessions/` session-liveness substrate,
and nothing written into or alongside such a worktree names a session id.
(The one place a real mapping could eventually live — the `locked <reason>`
string `git worktree list --porcelain` reports — is captured on the
``Worktree`` record and surfaced in the JSON output below, but is NOT parsed
for meaning: there is no evidence Claude Code puts a session id there, and
building a safety gate on an unverified external string is worse than not
gating at all. A worktree-creation-time sidecar would close this properly;
that is a platform/dispatch-path change, out of this baton's scope, and
tracked as separate debt.)

Because no per-worktree attribution is possible, the gate answers the
coarser, honestly-answerable question instead: is any OTHER coordinator
session live in this repo right now? A live peer might have an in-flight
background dispatch, so reaping ANYTHING this pass is refused wholesale —
not just the worktree that "looks" risky. Implemented via
``coordinator_core.session.worktree_safety.history_rewrite_verdict`` (the
same ok/refused/unknown predicate the sibling F4 fix uses), subtracting this
session's own id from the live set. FAIL CLOSED: a live peer (``refused``)
or an unresolvable liveness/self-identity (``unknown``) both block the
*entire* reap pass; only ``ok`` (no live peers) lets today's per-worktree
classification run unchanged. Every worktree still gets a line in that case
— action ``reap-skipped``, state left at its classification, detail naming
the peer sessions/reason — so a caller can tell "reap was blocked" apart
from "nothing was eligible" or "reap wasn't requested" (scan-only).

Exit codes (parity-critical, unchanged from the bash oracle):
  0 — completed (with or without findings)
  2 — not a git repo / no `git` on PATH / CLI usage error
  3 — --reap requested but a cherry-pick conflict left a worktree mid-state,
      or a `git worktree remove` invocation was rejected

Negative-spec (unchanged from the bash oracle — faithful port, not a rewrite):
    - Never touches non-agent worktrees (path must contain
      "/.claude/worktrees/agent-").
    - Never deletes branches that don't match the swept worktree's own branch
      (best-effort `git branch -D`, failure ignored — branch may already be
      gone).
    - Never force-pushes.
    - Never invokes destructive git commands (worktree remove, branch -D,
      cherry-pick) without --reap.
    - Under detached HEAD on the calling repo, --reap is silently clamped to
      scan-only (REAP forced off) — classification still runs against the
      detached HEAD SHA as the compare ref, but the commits-clean/dirty
      reap branches never execute (ACTIVE_BRANCH is required for cherry-pick
      target and is guaranteed non-empty whenever the reap branches run).
    - `git worktree prune` runs best-effort at the end ONLY when --reap was
      requested (C16, wsc-tail-slim-down — a surface call must not mutate: a
      read-only advisory scan, e.g. `/workstream-start`'s `--format text` call
      with no `--reap`, must leave the repo's worktree bookkeeping untouched).
      Failure (e.g. Windows ref-lock contention) is ignored. Considered (and
      deliberately left out) for the liveness gate above: prune only forgets
      git's own bookkeeping for ALREADY-GONE worktree directories (admin-ref
      cleanup) — it destroys no content and no live agent's tree, so it is
      not a peer-liveness hazard the way `worktree remove --force` is; the
      REAP gate is a mutation-on-read-path fix, not a liveness-safety fix.
    - Do NOT parse the `locked <reason>` string for a session id or any other
      meaning — see the module docstring's KNOWN STRUCTURAL GAP note. It is
      captured and surfaced, never interpreted.

Submodule-context note (unchanged from the bash oracle): when $PWD/cwd is
inside a submodule, `git rev-parse --show-toplevel` resolves to the
*submodule* root, not its superproject — a submodule and its superproject
keep independent worktree sets. This module surfaces (does not act on) the
superproject path when detected; it never silently sweeps the superproject
from inside the submodule.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.session.worktree_safety import history_rewrite_verdict

# subprocess timeouts (rule: every subprocess.run over looping/external input
# needs a bound — a single hung `git` child must not wedge the whole sweep).
_GIT_TIMEOUT_SECS = 30
_CHERRY_PICK_TIMEOUT_SECS = 60  # may run commit hooks; generous but bounded
_PORCELAIN_TIMEOUT_SECS = 30

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_BENIGN_ALLOWLIST = frozenset({".claude/settings.local.json", ".last-cleanup"})

_AGENT_WORKTREE_MARKER = "/.claude/worktrees/agent-"

_HELP_TEXT = """\
Usage: agent-worktree-sweep.sh [--reap] [--format json|text]

  --reap            Remove empty-clean worktrees; cherry-pick + remove
                    commits-clean worktrees. Without it, scan only.
  --format json     Default. One JSON line per worktree.
  --format text     Human-readable summary.
  --help            Show this help.

Operates on the repo containing $PWD. Touches only worktrees whose path
matches .claude/worktrees/agent-* relative to the repo root.
"""


def _run(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = _GIT_TIMEOUT_SECS,
) -> subprocess.CompletedProcess:
    """Run a git subprocess with a bounded timeout and no stdin inheritance.

    stdin=DEVNULL prevents a hung child (e.g. a cherry-pick that triggers an
    interactive commit-hook prompt) from blocking on the sweep's own stdin.
    """
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATIONFLAGS,
    )


def _git_available() -> bool:
    try:
        proc = _run(["git", "--version"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_available: proc = _run([\"git\", \"--version\"], timeout=10) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _repo_root(cwd: Optional[Path] = None) -> Optional[str]:
    try:
        proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _repo_root: proc = _run([\"git\", \"rev-parse\", \"--show-toplevel\"], cwd=cwd) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _superproject_root(repo_root: Path) -> Optional[str]:
    try:
        proc = _run(
            ["git", "-C", str(repo_root), "rev-parse", "--show-superproject-working-tree"]
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _superproject_root: proc = _run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _active_branch(repo_root: Path) -> str:
    try:
        proc = _run(["git", "-C", str(repo_root), "branch", "--show-current"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _active_branch: proc = _run([\"git\", \"-C\", str(repo_root), \"branch\", \"--show-current\"]) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _head_sha(repo_root: Path) -> str:
    try:
        proc = _run(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _head_sha: proc = _run([\"git\", \"-C\", str(repo_root), \"rev-parse\", \"HEAD\"]) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


@dataclass
class Worktree:
    path: str
    branch: str
    locked: bool = False
    #: Raw text after "locked" in `git worktree list --porcelain` (empty
    #: string when locked with no reason, or not locked at all). Captured
    #: and surfaced as-is — see module docstring's KNOWN STRUCTURAL GAP note
    #: for why this is never parsed for meaning.
    lock_reason: str = ""


def _parse_worktree_porcelain(text: str) -> List[Worktree]:
    """Parse `git worktree list --porcelain` stanza output.

    Stanzas look like:
        worktree /path
        HEAD <sha>
        branch refs/heads/<name>
        locked
    separated by a blank line. Mirrors the bash oracle's line-by-line state
    machine exactly (current_path/current_branch/current_locked accumulation,
    flushed on the next "worktree " line or blank line, plus a final flush
    for a trailing stanza with no terminating blank line).
    """
    worktrees: List[Worktree] = []
    current_path = ""
    current_branch = ""
    current_locked = False
    current_lock_reason = ""

    def _flush():
        nonlocal current_path, current_branch, current_locked, current_lock_reason
        if current_path:
            worktrees.append(
                Worktree(current_path, current_branch, current_locked, current_lock_reason)
            )
        current_path = ""
        current_branch = ""
        current_locked = False
        current_lock_reason = ""

    for line in text.splitlines():
        if line.startswith("worktree "):
            _flush()
            current_path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            current_branch = line[len("branch refs/heads/"):]
        elif line.startswith("locked"):
            current_locked = True
            current_lock_reason = line[len("locked"):].strip()
        elif line == "":
            _flush()
    _flush()
    return worktrees


def _list_worktrees(repo_root: Path) -> List[Worktree]:
    proc = _run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        timeout=_PORCELAIN_TIMEOUT_SECS,
    )
    if proc.returncode != 0:
        return []
    return _parse_worktree_porcelain(proc.stdout)


def _is_agent_worktree(path: str) -> bool:
    return _AGENT_WORKTREE_MARKER in path


def _commits_ahead(wt_path: str, compare_ref: str) -> int:
    if not compare_ref:
        return 0
    try:
        proc = _run(["git", "-C", wt_path, "rev-list", "--count", f"{compare_ref}..HEAD"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _commits_ahead: git rev-list --count failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError:
        print(f"skip: _commits_ahead: return int(proc.stdout.strip() or \"0\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _status_porcelain_lines(wt_path: str) -> List[str]:
    try:
        proc = _run(["git", "-C", wt_path, "status", "--porcelain"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _status_porcelain_lines: proc = _run([\"git\", \"-C\", wt_path, \"status\", \"--porcelain\"]) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout
    return [line for line in out.splitlines() if line]


def _porcelain_path(line: str) -> str:
    """Strip the 2-char status code + separator, matching the bash oracle's
    `sub(/^...[ ]?/, "", path)` (3-char prefix incl. one trailing space,
    optional) plus surrounding-quote stripping for paths git double-quotes."""
    path = line
    if len(path) >= 3 and path[2] == " ":
        path = path[3:]
    elif len(path) >= 2:
        path = path[2:]
    if path.startswith('"'):
        path = path[1:]
    if path.endswith('"'):
        path = path[:-1]
    return path


def _is_benign_only(dirty_lines: List[str]) -> bool:
    for line in dirty_lines:
        path = _porcelain_path(line)
        if path not in _BENIGN_ALLOWLIST:
            return False
    return True


@dataclass
class Classification:
    state: str
    commits_ahead: int
    dirty_count: int


def classify_worktree(wt_path: str, compare_ref: str) -> Classification:
    commits_ahead = _commits_ahead(wt_path, compare_ref)
    dirty_lines = _status_porcelain_lines(wt_path)
    dirty_count = len(dirty_lines)
    benign_only = dirty_count > 0 and _is_benign_only(dirty_lines)

    if dirty_count > 0 and benign_only and commits_ahead == 0:
        state = "dirty-benign"
    elif dirty_count > 0:
        state = "dirty"
    elif commits_ahead > 0:
        state = "commits-clean"
    else:
        state = "empty-clean"
    return Classification(state, commits_ahead, dirty_count)


def _json_escape(s: str) -> str:
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\t", "\\t")
    s = s.replace("\n", "\\n")
    return s


def emit_json(
    path: str, branch: str, state: str, action: str, detail: str, lock_reason: str = ""
) -> str:
    return (
        '{"path":"%s","branch":"%s","state":"%s","action":"%s","detail":"%s","lock_reason":"%s"}'
        % (
            _json_escape(path),
            _json_escape(branch),
            _json_escape(state),
            _json_escape(action),
            _json_escape(detail),
            _json_escape(lock_reason),
        )
    )


def emit_text(
    path: str, branch: str, state: str, action: str, detail: str, lock_reason: str = ""
) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    return "[%s] %-14s %-10s %s\n  branch=%s detail=%s lock_reason=%s" % (
        now,
        state,
        action,
        path,
        branch,
        detail,
        lock_reason,
    )


def _emit(
    fmt: str, path: str, branch: str, state: str, action: str, detail: str, lock_reason: str = ""
) -> str:
    if fmt == "text":
        return emit_text(path, branch, state, action, detail, lock_reason)
    return emit_json(path, branch, state, action, detail, lock_reason)


def _remove_worktree(repo_root: Path, wt_path: str) -> bool:
    try:
        proc = _run(["git", "-C", str(repo_root), "worktree", "remove", "--force", wt_path])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _remove_worktree: proc = _run([\"git\", \"-C\", str(repo_root), \"worktree\", \"remove\", \"--for failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _delete_branch_best_effort(repo_root: Path, branch: str) -> Optional[str]:
    """Best-effort `git branch -D <branch>` after a worktree has been removed.

    Returns ``None`` on success, or when ``branch`` is empty (nothing to
    delete — not a failure, matches the "may already be gone" negative-spec).
    Returns a short error string on failure so the caller can fold it into
    the emitted line instead of swallowing it silently — a worktree removed
    but its branch left behind is a real, visible-in-`git branch` residue
    that a caller reading only stdout previously had no way to see. Still
    best-effort: this never raises, and callers never abort the reap on a
    branch-delete failure.
    """
    if not branch:
        return None
    try:
        proc = _run(["git", "-C", str(repo_root), "branch", "-D", branch])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return stderr or f"git branch -D exited {proc.returncode}"
    return None


def _commit_list_reverse(wt_path: str, active_branch: str) -> List[str]:
    try:
        proc = _run(["git", "-C", wt_path, "rev-list", "--reverse", f"{active_branch}..HEAD"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _commit_list_reverse: git rev-list --reverse failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def _cherry_pick_with_env(repo_root: Path, sha: str, wt_path: str) -> bool:
    """Cherry-pick one commit onto repo_root's active branch.

    COORDINATOR_OVERRIDE_BRANCH mirrors the bash oracle's env-var pair —
    the destination branch may be a protected/override-gated branch under
    daily-branch-discipline, and this salvage cherry-pick is an authorized
    exception (agent-worktree-sweep is itself a trusted maintenance tool).
    """
    proc_env = {
        **_os_environ_copy(),
        "COORDINATOR_OVERRIDE_BRANCH": "1",
        "COORDINATOR_OVERRIDE_BRANCH_REASON": f"agent-worktree-sweep cherry-pick from {wt_path}",
    }
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "cherry-pick", "--allow-empty", "-x", sha],
            capture_output=True,
            text=True,
            timeout=_CHERRY_PICK_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
            env=proc_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _cherry_pick_with_env: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _os_environ_copy() -> dict:
    return dict(os.environ)


def _cherry_pick_abort(repo_root: Path) -> None:
    try:
        _run(["git", "-C", str(repo_root), "cherry-pick", "--abort"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _cherry_pick_abort: _run([\"git\", \"-C\", str(repo_root), \"cherry-pick\", \"--abort\"]) failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass


def _worktree_prune(repo_root: Path) -> None:
    try:
        _run(["git", "-C", str(repo_root), "worktree", "prune"])
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _worktree_prune: _run([\"git\", \"-C\", str(repo_root), \"worktree\", \"prune\"]) failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass


def _sweep_one(
    repo_root: Path,
    active_branch: str,
    compare_ref: str,
    reap: bool,
    fmt: str,
    wt: Worktree,
    reap_block_detail: Optional[str] = None,
) -> Tuple[str, int]:
    """Classify + (optionally) reap one agent worktree.

    `compare_ref` (active branch name, or the calling repo's detached-HEAD SHA
    fallback) drives classification; `active_branch` — guaranteed non-empty
    whenever the reap branches below actually execute (main() forces reap off
    under detached HEAD) — is the cherry-pick destination for commits-clean
    salvage. Keeping the two distinct is load-bearing: collapsing them back to
    one parameter reintroduces the bug where a detached-HEAD calling repo
    misclassifies every ahead-of-HEAD worktree as empty-clean (compare_ref
    would silently fall back to '' instead of the detached SHA).

    `reap_block_detail`: when not None, `--reap` was requested but the
    whole-pass peer-liveness gate (see main()) refused it — every worktree
    this pass gets action `reap-skipped` with this string folded into detail,
    regardless of its own classification. This is deliberately a SEPARATE
    action from `scan-only` (reap never requested) and from any per-worktree
    outcome (removed/warned-skip/etc, reap requested and ran) — a caller must
    be able to tell "reap was blocked" apart from "nothing was eligible" or
    "reap wasn't requested" from the action field alone.

    Returns (emitted_line, exit_code_contribution) where exit_code_contribution
    is 0 or 3 (mirrors the bash oracle's EXIT_CODE accumulation — any 3
    anywhere in the sweep wins over 0 for the overall process exit code).
    """
    if not Path(wt.path).is_dir():
        return (
            _emit(
                fmt, wt.path, wt.branch, "missing", "skip",
                "worktree path does not exist", wt.lock_reason,
            ),
            0,
        )

    classification = classify_worktree(wt.path, compare_ref)
    state = classification.state

    if reap_block_detail is not None:
        detail = (
            f"ahead={classification.commits_ahead} dirty={classification.dirty_count} "
            f"— {reap_block_detail}"
        )
        return (
            _emit(fmt, wt.path, wt.branch, state, "reap-skipped", detail, wt.lock_reason),
            0,
        )

    if not reap:
        detail = f"ahead={classification.commits_ahead} dirty={classification.dirty_count}"
        return _emit(fmt, wt.path, wt.branch, state, "scan-only", detail, wt.lock_reason), 0

    if state == "empty-clean":
        if _remove_worktree(repo_root, wt.path):
            branch_err = _delete_branch_best_effort(repo_root, wt.branch)
            detail = "ahead=0 dirty=0"
            if branch_err:
                detail += f"; branch delete failed: {branch_err}"
            return _emit(fmt, wt.path, wt.branch, state, "removed", detail, wt.lock_reason), 0
        return (
            _emit(
                fmt, wt.path, wt.branch, state, "remove-failed",
                "git worktree remove rejected", wt.lock_reason,
            ),
            3,
        )

    if state == "dirty-benign":
        if _remove_worktree(repo_root, wt.path):
            branch_err = _delete_branch_best_effort(repo_root, wt.branch)
            detail = f"ahead=0 dirty={classification.dirty_count} (benign-allowlist only)"
            if branch_err:
                detail += f"; branch delete failed: {branch_err}"
            return _emit(fmt, wt.path, wt.branch, state, "removed", detail, wt.lock_reason), 0
        return (
            _emit(
                fmt, wt.path, wt.branch, state, "remove-failed",
                "git worktree remove --force rejected", wt.lock_reason,
            ),
            3,
        )

    if state == "commits-clean":
        # active_branch is guaranteed non-empty here: reap is forced False by
        # main() under detached HEAD (where active_branch is empty), so this
        # branch only ever executes when active_branch is non-empty. Invariant
        # depends on that guard ordering in main() — do not reorder it.
        commits = _commit_list_reverse(wt.path, active_branch)
        picked = 0
        pick_failed = ""
        for sha in commits:
            if _cherry_pick_with_env(repo_root, sha, wt.path):
                picked += 1
            else:
                pick_failed = sha
                _cherry_pick_abort(repo_root)
                break
        if pick_failed:
            detail = f"picked={picked}/{len(commits)} stopped_at={pick_failed} — worktree retained for PM"
            return _emit(fmt, wt.path, wt.branch, state, "salvage-conflict", detail, wt.lock_reason), 3
        if _remove_worktree(repo_root, wt.path):
            branch_err = _delete_branch_best_effort(repo_root, wt.branch)
            detail = f"cherry-picked={picked} onto={active_branch}"
            if branch_err:
                detail += f"; branch delete failed: {branch_err}"
            return _emit(fmt, wt.path, wt.branch, state, "salvaged-removed", detail, wt.lock_reason), 0
        detail = f"cherry-picked={picked} onto={active_branch} but worktree remove rejected"
        return _emit(fmt, wt.path, wt.branch, state, "salvaged-remove-failed", detail, wt.lock_reason), 3

    # state == "dirty"
    detail = (
        f"ahead={classification.commits_ahead} dirty={classification.dirty_count} "
        "— uncommitted changes; PM must triage"
    )
    return _emit(fmt, wt.path, wt.branch, state, "warned-skip", detail, wt.lock_reason), 0


def _parse_args(argv: List[str]) -> Tuple[bool, str, Optional[int]]:
    """Parse CLI args. Returns (reap, fmt, early_exit_code) — early_exit_code
    is set (and reap/fmt are meaningless) for --help or a usage error."""
    reap = False
    fmt = "json"
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--reap":
            reap = True
            i += 1
        elif arg == "--format":
            if i + 1 >= len(argv):
                print("agent-worktree-sweep: --format requires a value", file=sys.stderr)
                return reap, fmt, 2
            fmt = argv[i + 1]
            i += 2
        elif arg in ("--help", "-h"):
            print(_HELP_TEXT, end="")
            return reap, fmt, 0
        else:
            print(f"unknown arg: {arg}", file=sys.stderr)
            return reap, fmt, 2
    return reap, fmt, None


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]

    reap, fmt, early_exit = _parse_args(argv)
    if early_exit is not None:
        return early_exit

    if not _git_available():
        print("git not found", file=sys.stderr)
        return 2

    repo_root_str = _repo_root()
    if not repo_root_str:
        print("not in a git repo", file=sys.stderr)
        return 2
    repo_root = Path(repo_root_str)

    superproject = _superproject_root(repo_root)
    if superproject:
        print(f"note: {repo_root} is a submodule of {superproject}", file=sys.stderr)
        print(
            f"      this sweep covers the submodule only; agent worktrees in "
            f"{superproject} will NOT be reached — re-run from there for those.",
            file=sys.stderr,
        )

    active_branch = _active_branch(repo_root)
    if not active_branch:
        print("calling repo is in detached HEAD; refuse to reap", file=sys.stderr)
        reap = False

    compare_ref = active_branch or _head_sha(repo_root)

    # Whole-pass peer-liveness gate (S1b): a live PEER coordinator session
    # (this session's own id subtracted) might have an in-flight background
    # Agent dispatch, so a refused/unknown verdict blocks reaping EVERY
    # worktree this pass — see module docstring's KNOWN STRUCTURAL GAP note
    # for why this is whole-pass rather than per-worktree. Only computed when
    # reap is still requested post detached-HEAD clamp above; a plain scan
    # never touches anything, so it never needs a gate.
    reap_block_detail: Optional[str] = None
    if reap:
        verdict = history_rewrite_verdict(cwd=str(repo_root))
        if verdict.outcome != "ok":
            reap_block_detail = f"reap requested but skipped ({verdict.outcome}): {verdict.reason}"
            print(
                f"reap refused: {verdict.reason} — scanning without destructive action",
                file=sys.stderr,
            )

    worktrees = [w for w in _list_worktrees(repo_root) if _is_agent_worktree(w.path)]

    exit_code = 0
    for wt in worktrees:
        line, contribution = _sweep_one(
            repo_root, active_branch, compare_ref, reap, fmt, wt, reap_block_detail
        )
        print(line)
        if contribution:
            exit_code = contribution

    if reap:
        _worktree_prune(repo_root)

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
