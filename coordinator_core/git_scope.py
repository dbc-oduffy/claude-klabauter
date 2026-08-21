"""
coordinator_core.git_scope — the one correct way to run a git probe against a
repository that is NOT the ambient one.

Purpose: `git -C <path>` is not repository scoping. It changes only the child
process's working directory; every repo-scoping git ENVIRONMENT variable still
wins over directory-based discovery. git exports `GIT_DIR` to every hook it
runs, commonly as the relative `"."`, so anything invoked downstream of a hook
inherits it — and a `git -C <sibling-repo>` probe then silently answers about
OUR repo while every log line, message, and emitted field still names the
sibling's path.

The second half of the same defect is exit-code collapse. `git cat-file -e`
exits 128 for every failure mode without exception — absent object, malformed
name, path is not a repo, path does not exist — and `merge-base --is-ancestor`
conflates its own 1 (definitely not an ancestor) with 128 (could not answer) the
moment a caller writes `returncode != 0`. Mapping "could not check" onto
"checked, and the answer is no" turns an unanswerable probe into a confident
false claim about somebody else's repo.

Incident of record (2026-08-03, reported by doe-claude-em): a cross-repo premise
check reported two shas as identically "NOT in their clone". One was genuinely
dangling; the other resolved cleanly on their branch and on origin. A true
finding laundered into a coin flip. `coordinator/bin/cross-repo-memo.py` was the
first fix; this module is that fix extracted so the remaining cross-repo probes
share one implementation rather than each re-deriving it.

Usage contract — a foreign-repo probe is THREE calls, not one:
    1. `foreign_repo_unusable_reason(root)` FIRST. A non-None return means every
       downstream answer is unanswerable; render it as its own outcome, never as
       an absence/negative/failure claim about the target.
    2. `git_predicate(root, [...])` for anything whose exit code is a
       true/false predicate (`rev-parse --verify --quiet`,
       `merge-base --is-ancestor`). Returns a tri-state, never a bool.
    3. `scoped_cat_file_batch(root, tokens)` for value-returning blob reads — one
       process for N `<ref>:<path>` tokens, in place of N `git show` calls.
       `scoped_git_env()` remains the escape hatch for value-returning calls
       this module does not wrap (`log`, `ls-remote`) — pass it as `env=`.

Negative-spec:
  - Do NOT use this for calls against the AMBIENT repo that deliberately want
    the inherited environment (a hook auditing the very commit that spawned it
    legitimately reads GIT_DIR). This module is for the case where the `-C`
    target is a different repository than the one the process was launched in.
  - `foreign_repo_unusable_reason` does not merely check the exit code of
    `rev-parse`; it confirms the RESOLVED git dir lies inside the target's own
    tree. That confinement check is the part that catches a poisoned
    environment — with `GIT_DIR` inherited, `git -C <target> rev-parse` exits 0,
    reports the OTHER repo's git dir, and answers happily about it (verified
    empirically, not assumed).
  - That confinement check therefore requires `repo_path` to be a repository
    ROOT. Callers probing a SUBDIRECTORY of a tree (e.g. a self-locating
    `Path(__file__)`-derived engine dir) must use `scoped_git_env()` alone;
    the git dir of a subdirectory's repo is legitimately outside that
    subdirectory and would be reported unusable. Linked worktrees are the
    other known non-fit: their git dir lives under the main checkout's
    `.git/worktrees/`, outside the worktree's own tree.
  - Nothing here raises. Every failure is a returned reason string or an
    UNKNOWN verdict; a broken probe must never break its caller's ceremony.

Spec backlink: `coordinator/bin/cross-repo-memo.py::_git_premise_probe` (the
reference implementation this generalizes).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "REPO_SCOPING_ENV_VARS",
    "PROBE_YES",
    "PROBE_NO",
    "PROBE_UNKNOWN",
    "scoped_git_env",
    "foreign_repo_unusable_reason",
    "reset_foreign_repo_probe_memo",
    "git_predicate",
    "scoped_cat_file_batch",
    "FOREIGN_REPO_GIT_TIMEOUT_SECONDS",
]

#: The git environment variables that scope git to a repository. Every one of
#: these beats `-C`-based discovery, so all of them are stripped before a
#: foreign-repo probe. `GIT_DIR` is the one that fires in the wild (git exports
#: it to hooks); the rest are here because leaving any of them in place leaves
#: the same hole open through a narrower door.
REPO_SCOPING_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)

#: Tri-state probe verdicts. PROBE_NO and PROBE_UNKNOWN are DIFFERENT claims and
#: must never render as the same sentence: NO asserts the target answered and
#: the answer was negative; UNKNOWN asserts only that this process failed to
#: find out, and says nothing whatever about the target.
PROBE_YES = "yes"
PROBE_NO = "no"
PROBE_UNKNOWN = "unknown"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: The single dial for every probe in this module, and the only one — no caller
#: passes a `timeout=`. Every call here is a local object-database read against
#: an already-resolved path, measured at 26.9ms for `git -C <repo> rev-parse
#: HEAD` on the reference box (DR-344 § 4), so 2.0 is the same clamp value
#: `ipc.py :: CEREMONY_BUDGET_SECS` holds an entire op to. A probe that cannot
#: answer a local object-database question inside a whole ceremony's budget is
#: an UNKNOWN, not something to wait on.
#:
#: Negative-spec: this number may be lowered, never raised (DR-349 § 3). A site
#: that cannot live inside it is a defect report about that site, not a case for
#: widening the dial for every other caller.
FOREIGN_REPO_GIT_TIMEOUT_SECONDS = 2.0

#: Memo for `foreign_repo_unusable_reason`, keyed on
#: `(os.path.realpath(repo_path), os.getpid())`. A path does not stop being a
#: git repository inside one process, and the schema-drift, cockpit-freshness
#: and emit surfaces were each re-probing the same DoE clone from scratch with
#: no cache between them. The pid is part of
#: the key so a `fork`ed child, which inherits this dict wholesale, re-probes
#: rather than trusting its parent's answer.
_UNUSABLE_REASON_MEMO: "dict[tuple[str, int], Optional[str]]" = {}


def scoped_git_env(base: Optional[dict] = None) -> dict:
    """Return *base* (default `os.environ`) minus every repo-scoping git var.

    Pass as `env=` to any `git -C <foreign-repo>` call this module does not
    wrap. Without it, `-C` is not actually scoping anything — see the module
    docstring.
    """
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if k not in REPO_SCOPING_ENV_VARS}


def _first_line(text: str) -> str:
    """First non-empty line of git's stderr, for embedding in a one-line reason."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def foreign_repo_unusable_reason(
    repo_path: "str | Path",
    *,
    timeout: float = FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Return None when *repo_path* is usable as a git repo, else why it is not.

    Run this BEFORE any probe whose result will be reported as a fact about
    *repo_path*, so that "the path is not a git repo", "the path does not
    exist", "git is not installed", and "discovery landed on somebody else's
    repository" are all reported as *could not check* rather than silently
    becoming a negative claim about the target's contents.

    The git-dir confinement comparison is the part that catches a poisoned
    environment. Stripping `GIT_DIR` is necessary but not sufficient: a
    `.git` file pointing elsewhere, a `GIT_CEILING_DIRECTORIES` interaction, or
    an outer repo that contains the target as an untracked subdirectory can all
    resolve `rev-parse` to a git dir the caller did not mean. If the resolved
    git dir is not inside *repo_path*, the probe would answer about the wrong
    repository, and that is reported here rather than trusted.

    Memoised per `(realpath(repo_path), pid)`: the answer is a property of a
    path and a process, and the schema-drift surfaces and the emit path each
    ask it repeatedly about the same sibling clone. Call it wherever the read
    naturally belongs — after the first, it costs no process.

    Negative-spec (the memo): a repo made unusable mid-process — deleted, or
    its `.git` swapped underneath us — keeps answering from the first probe.
    That is the deliberate trade: this is a pre-flight guard against asking the
    WRONG repository, not a liveness watch, and every caller downstream of it
    still handles its own git failure. A test that mutates a fixture repo
    between probes calls `reset_foreign_repo_probe_memo()`.

    Negative-spec (the dial): `timeout` is narrow-only — clamped to
    `FOREIGN_REPO_GIT_TIMEOUT_SECONDS`, so passing a larger value buys nothing
    (DR-349 § 3). No in-tree caller passes it at all.
    """
    root = str(repo_path)
    if not root:
        return "no repository path was supplied"
    try:
        key: "Optional[tuple[str, int]]" = (os.path.realpath(root), os.getpid())
    except (OSError, ValueError):
        key = None
    if key is not None and key in _UNUSABLE_REASON_MEMO:
        return _UNUSABLE_REASON_MEMO[key]
    reason = _probe_foreign_repo(root, min(timeout, FOREIGN_REPO_GIT_TIMEOUT_SECONDS))
    if key is not None:
        _UNUSABLE_REASON_MEMO[key] = reason
    return reason


def reset_foreign_repo_probe_memo() -> None:
    """Drop every memoised `foreign_repo_unusable_reason` answer.

    For tests that build, mutate, or delete a fixture repository between probes
    in one process — the one case the memo's stated trade does not cover.
    """
    _UNUSABLE_REASON_MEMO.clear()


def _probe_foreign_repo(root: str, timeout: float) -> Optional[str]:
    """One `rev-parse --absolute-git-dir`, then a pure-path confinement check.

    The un-memoised body of `foreign_repo_unusable_reason`; that function's
    docstring carries the contract.
    """
    try:
        probe = subprocess.run(
            ["git", "-C", root, "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return f"git rev-parse timed out after {timeout}s resolving {root}"
    except OSError as exc:
        return f"could not run git: {exc}"
    if probe.returncode != 0:
        return _first_line(probe.stderr) or f"git rev-parse exited {probe.returncode}"
    git_dir = probe.stdout.strip()
    if not git_dir:
        return "git reported no git directory for this path"
    try:
        real_git_dir = os.path.realpath(git_dir)
        real_root = os.path.realpath(root)
        inside = os.path.commonpath([real_git_dir, real_root]) == real_root
    except (OSError, ValueError) as exc:
        return f"could not confirm {git_dir} belongs to {root}: {exc}"
    if not inside:
        return (
            f"git resolved {root} to the git dir {git_dir}, which is outside "
            "that tree — a probe here would answer about the wrong repository"
        )
    return None


def git_predicate(
    repo_path: "str | Path",
    args: Sequence[str],
    *,
    timeout: float = FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
) -> "tuple[str, str]":
    """Run one repo-scoped git PREDICATE and map its exit code to a tri-state.

    git's convention for the predicates this is for (`rev-parse --verify
    --quiet`, `merge-base --is-ancestor`, `check-ignore -q`) is 0 = true,
    1 = false, anything else = the question could not be answered at all.
    Collapsing that third code into the second is the defect this function
    exists to prevent.

    Returns `(verdict, reason)`. `reason` is non-empty only for PROBE_UNKNOWN,
    and names git's own words for why the question went unanswered.

    Negative-spec (the dial): `timeout` is narrow-only — clamped to
    `FOREIGN_REPO_GIT_TIMEOUT_SECONDS` (DR-349 § 3). These are local object-database
    predicates; one that needs longer is a defect report about the call site.
    """
    timeout = min(timeout, FOREIGN_REPO_GIT_TIMEOUT_SECONDS)
    try:
        probe = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return PROBE_UNKNOWN, f"git timed out after {timeout}s"
    except OSError as exc:
        return PROBE_UNKNOWN, f"could not run git: {exc}"
    if probe.returncode == 0:
        return PROBE_YES, ""
    if probe.returncode == 1:
        return PROBE_NO, ""
    return PROBE_UNKNOWN, _first_line(probe.stderr) or f"git exited {probe.returncode}"


def scoped_cat_file_batch(
    repo_path: "str | Path",
    tokens: Sequence[str],
) -> "Optional[dict[str, Optional[str]]]":
    """Read N `<ref>:<path>` blobs out of a foreign repo in ONE process.

    The batch primitive `git show <ref>:<path>` lacks. `cat-file --batch` takes
    its tokens on stdin and emits `<oid> <type> <size>\\n<contents>\\n` per
    resolved token and `<token> <reason>\\n` per unresolved one, so N vendored
    files cost one spawn instead of N — the amplification
    `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py` exists to
    catch.

    Returns token -> blob text, with None for a token git reported as
    missing/ambiguous, or None for the WHOLE batch when git could not run at
    all. A caller must tell those two apart: the first is "this one file is
    unresolvable", the second is "no comparison ran", and reporting the second
    as the first is the confident-false-claim defect this module exists to
    prevent.

    Binary-exact by construction: the record framing is byte-counted, so the
    payload is sliced by `<size>` out of raw stdout and decoded afterwards.
    Parsing a text stream instead desynchronises the framing on any blob whose
    bytes do not round-trip through the locale codec.

    Never raises — an OSError, a timeout, or a non-zero exit all fold into None.
    Run `foreign_repo_unusable_reason` first: this call does not confirm the
    repo is the one you meant, it only reads it.

    Not to be confused with `ops/ceremony/git_native.py :: cat_file_batch`, which
    reads N paths at ONE ref out of the AMBIENT repo and deliberately inherits the
    ambient git environment. This one takes whole `<ref>:<path>` tokens (so a
    caller may mix refs) and strips the repo-scoping environment, which is the
    whole reason a foreign-repo read cannot use that one.
    """
    tokens = list(tokens)
    if not tokens:
        return {}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "cat-file", "--batch"],
            input=("\n".join(tokens) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    blobs: "dict[str, Optional[str]]" = {}
    out = result.stdout
    pos = 0
    for token in tokens:
        newline = out.find(b"\n", pos)
        if newline == -1:
            blobs[token] = None
            continue
        header = out[pos:newline].decode("utf-8", errors="replace").split()
        pos = newline + 1
        # `<oid> <type> <size>` is the only resolved shape; anything shorter is a
        # `<token> missing`/`<token> ambiguous` line, which carries no payload to skip.
        if len(header) != 3 or not header[2].isdigit():
            blobs[token] = None
            continue
        size = int(header[2])
        blobs[token] = out[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1  # the trailing newline git appends after each blob
    return blobs
