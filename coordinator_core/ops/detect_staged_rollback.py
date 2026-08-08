"""
coordinator_core.ops.detect_staged_rollback — staged-index exact-blob rollback detector.

Purpose: reads the caller's staged index (``git diff --cached``) and flags the
shape a 2026-07-28 incident produced on this repo — an in-progress index whose
staged blobs, file by file, were byte-identical to an OLDER commit's blob for
that same path, silently reverting landed work had it been committed. This
module never mutates a repo: it is read-and-report only, and is NOT wired into
any commit path (that is a separate, PM-gated decision — see the git-hook
minimization carve-out in claude-klabauter's CLAUDE.md § Runtime conventions
(b)). ``main()`` here is invoked directly by a caller (a future git hook, an
EM session, a CI step) that decides for itself whether/how to gate on the
exit code.

Detection logic:
    For each staged path, resolve its staged (index) blob sha, then walk that
    path's own commit history (bounded by ``HISTORY_DEPTH_LIMIT`` — see that
    constant's docstring for why 40) looking for an OLDER commit whose blob
    for the same path is byte-identical to the staged blob. A match records
    the matched commit and the "rollback depth" — how many commits touching
    that path were skipped backwards to reach it (1 = the immediately prior
    version of the file; deeper = further back).

Threshold design (the whole point of this module — see MIN_ROLLBACK_PATHS /
MIN_ROLLBACK_DEPTH docstrings for the numbers and their justification): a
single file matching an older blob is ordinary (``git revert``, undoing one
bad edit, restoring one file) and must NOT fire alone unless the match is
deep. The signal this module exists to catch is BREADTH (many files at once)
or DEPTH (one file jumping back past several of its own intervening edits) —
never a lone shallow match.

Override (deliberate divergence from a no-override discipline): unlike a
correctness guard, this one has a real and benign false-positive case — a
DELIBERATE mass revert is legitimate work indistinguishable, from git alone,
from the incident this module exists to catch. So this module DOES take a
named override env var (``COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK``),
spelled to match the sibling precommit gates' convention in
``coordinator_core.ops.install_meta_repo_precommit_hook._GATE_REGISTRY``. Do
NOT "fix" this into a hard block by analogy with that module's own gates —
every one of ITS refusal cases is a genuine defect (see that module's own
docstring); this module's is not. A later reader who removes the override on
that analogy would be reintroducing the exact false-positive this module was
built to tolerate.

Not wired: this module registers no git hook, and is not invoked from
``install_meta_repo_precommit_hook.py``'s gate registry or any other commit
path. Wiring it in is a separate PM-gated decision.

Negative-spec:
    - NEVER runs a mutating git command (no ``git reset``, ``git checkout``,
      no staging/unstaging) — read-only over ``git diff --cached`` / ``git
      log`` / ``git rev-parse`` only.
    - Does NOT walk the FULL repo history per path — bounded by
      ``HISTORY_DEPTH_LIMIT``; a path with a real rollback beyond that depth
      is a known blind spot, not a silent success (see that constant's
      docstring).
    - Does NOT special-case renames — invoked with ``--no-renames`` so a
      renamed file surfaces as a plain delete + add, not a moved match. A
      rename false-negative (content moved to a new path) is out of scope:
      this module's own remit is "the same path rolled back", not general
      content provenance.
    - Does NOT itself decide whether to block a commit — it is a read/report
      library plus a CLI; the caller (once wired, if ever) owns the exit-code
      → block decision.
"""

from __future__ import annotations

import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Bound on how far back (in commits touching a given path) this module will
# search for a matching blob. 40 is deep enough to catch every rollback shape
# seen in practice (the 2026-07-28 incident's deepest single-path match was 5
# commits back) with comfortable headroom, while keeping the per-path `git
# log` call cheap and bounded regardless of a path's total lifetime history —
# a repo where one file has thousands of historical revisions must not turn
# one staged-path check into an unbounded walk.
HISTORY_DEPTH_LIMIT = 40

# Breadth threshold: fire when at least this many DISTINCT staged paths are
# rollback candidates, regardless of any single path's depth. Justification:
# the 2026-07-28 incident staged nine paths simultaneously, each an exact
# older-commit match — no ordinary single-file operation (a manual revert, a
# `git checkout HEAD~1 -- file`) produces multi-file breadth like this by
# accident. 3 sits comfortably below that incident's 9 while staying above
# the common "I fixed two related files and also reverted a stray edit in a
# third" shape, which is still deliberate single-purpose work, not a
# wholesale tree restore.
MIN_ROLLBACK_PATHS = 3

# Depth threshold: fire when ANY single path's match is at least this many
# commits back, even if it is the only rollback candidate staged. 1 (the
# immediately-prior version of a file) is the ordinary "undo my last edit"
# case and must never fire alone. 2+ means the staged content skips past at
# least one OTHER completed commit to that path, not just the most recent
# one — verified against the incident: `install_meta_repo_precommit_hook.py`
# staged its blob from commit `ba84e095`, five commits behind the path's
# HEAD version (`f204c4a0`), and `resolve_target.py` similarly matched an
# older commit several revisions back. Both clear this threshold by a wide
# margin; an ordinary single-step undo (depth 1) does not.
MIN_ROLLBACK_DEPTH = 2

OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK"

_PROG = "detect-staged-rollback"

# Distinguishes the hash from the subject in a single `git log --format=...`
# field without colliding with `-z`'s own NUL record terminator (see
# `_path_history`'s docstring for the full record shape).
_FIELD_SEP = "\x01"


@dataclass(frozen=True)
class SkippedCommit:
    commit: str
    subject: str


@dataclass(frozen=True)
class RollbackCandidate:
    path: str
    matched_commit: str
    matched_subject: str
    depth: int
    skipped: Tuple[SkippedCommit, ...] = field(default_factory=tuple)


def _run_git(args: Sequence[str], cwd: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )


def _staged_blobs(repo_root: str, env: Optional[dict] = None) -> Dict[str, str]:
    """Path -> full staged (index) blob sha for every staged, non-deleted path.

    One `git diff --cached --raw -z --no-renames --no-abbrev` call for the
    WHOLE staged set — not one call per path — is the perf-critical piece:
    this is O(1) git invocations in the number of staged paths, not O(N).

    Record shape per staged path, NUL-delimited (`-z`):
        ":<oldmode> <newmode> <oldsha> <newsha> <status>\\0<path>\\0"
    concatenated back-to-back with no separator between records (confirmed
    empirically — see this module's test fixtures). Splitting the whole
    output on NUL therefore yields flat (rawline, path) pairs in sequence.

    Deleted paths (status "D", newsha all-zero) are skipped — a deletion has
    no staged content to compare against a historical blob.
    """
    result = _run_git(
        ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev"],
        cwd=repo_root,
        env=env,
    )
    if result.returncode != 0:
        return {}

    tokens = result.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]

    blobs: Dict[str, str] = {}
    i = 0
    while i + 1 < len(tokens):
        rawline = tokens[i]
        path = tokens[i + 1]
        i += 2
        if not rawline.startswith(":"):
            # Defensive: a malformed/unexpected record shape must not crash
            # the scan — skip it rather than mis-parse the next pair.
            continue
        parts = rawline[1:].split(" ")
        if len(parts) != 5:
            continue
        _old_mode, _new_mode, _old_sha, new_sha, status = parts
        if status.startswith("D"):
            continue
        blobs[path] = new_sha
    return blobs


def _path_history(
    repo_root: str,
    path: str,
    env: Optional[dict] = None,
    limit: int = HISTORY_DEPTH_LIMIT,
) -> List[Tuple[str, str, str]]:
    """Most-recent-first list of (commit_hash, subject, blob_sha) for every
    commit touching `path`, bounded to `limit` commits.

    ONE `git log -n <limit> --format=%H<SEP>%s --raw --no-renames --no-abbrev
    -z -- <path>` call per path returns both the commit metadata AND that
    commit's blob sha for the path in a single subprocess invocation — commit
    metadata and diff content interleaved as
    "<hash><SEP><subject>\\n:<rawline>\\0<path>\\0" repeated, so history[i]
    for i>=1 is always available at the cost of one process spawn per staged
    path (the documented "one git log per staged path" budget), never a
    second per-commit call to fetch a blob or subject separately.
    """
    result = _run_git(
        [
            "log",
            f"-n{limit}",
            f"--format=%H{_FIELD_SEP}%s",
            "--raw",
            "--no-renames",
            "--no-abbrev",
            "-z",
            "--",
            path,
        ],
        cwd=repo_root,
        env=env,
    )
    if result.returncode != 0 or not result.stdout:
        return []

    tokens = result.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]

    history: List[Tuple[str, str, str]] = []
    i = 0
    while i + 3 <= len(tokens):
        header = tokens[i]
        if _FIELD_SEP not in header:
            # Malformed record — bail out on the remainder of this path's
            # history rather than mis-parse; partial history degrades to a
            # smaller search window, never a crash.
            break
        commit_hash, subject = header.split(_FIELD_SEP, 1)
        # `git log`'s own body separator puts a leading "\n" on the raw-diff
        # line that follows a custom --format header (empirically confirmed
        # — see this module's tests): strip it before the ":" status check,
        # or every record is silently misparsed as malformed.
        rawline = tokens[i + 1].lstrip("\n")
        # path = tokens[i + 2] — not needed; we already know the path.
        i += 3
        if not rawline.startswith(":"):
            continue
        parts = rawline[1:].split(" ")
        if len(parts) != 5:
            continue
        _old_mode, _new_mode, _old_sha, new_sha, status = parts
        history.append((commit_hash, subject, new_sha))
    return history


def _batch_path_history(
    repo_root: str,
    paths: Sequence[str],
    env: Optional[dict] = None,
) -> Dict[str, List[Tuple[str, str, str]]]:
    """ONE `git log --format=%H<SEP>%s --raw --no-renames --no-abbrev -z --
    <path1> <path2> ...` walk resolving every *paths* entry's full commit
    history (most-recent-first, unbounded — no `-n<limit>` here; see below
    for why) in a single subprocess invocation, batching what
    `find_rollback_candidates` used to spend one `_path_history` spawn per
    staged path on.

    Shape: multi-pathspec / object-membership (the safe side of § Anti-scope
    2), NOT range batching — `-- pathA pathB ...` asks "every commit
    touching ANY of these paths" (union/OR semantics), the exact same shape
    `draft_plan_aging._batch_git_commit_epochs` (C14, `bd6d14afc`) already
    landed for its own N+1 fix; adapted here from a single first-touch-wins
    epoch per path to a full per-path history list. No reachability/ancestry
    arithmetic (`A..B`, `--not`) is used anywhere in this query, so § Anti-
    scope 1/2's range-batching trap does not apply.

    Deliberately UNBOUNDED (no `-n<limit>` on the git invocation itself,
    unlike single-path `_path_history`'s `-n<limit>`): applying a git-level
    `-n` to a multi-pathspec query caps the number of commits in the
    INTERLEAVED walk across every requested path combined, not per path — a
    path touched only by old commits could be starved to zero history while
    a frequently-touched sibling path consumes the whole window. Every
    commit touching ANY requested path is read once; the per-path cap
    (`limit`, HISTORY_DEPTH_LIMIT by default) is applied in memory instead,
    once each path's own list reaches that length no further commits are
    appended to it (but the shared walk still proceeds for the other
    paths — same complexity bound as before, `HISTORY_DEPTH_LIMIT` commits
    per requested path, just re-homed from a `git log -n` argument to a
    Python-side counter).

    Returns `{path: [(commit_hash, subject, blob_sha), ...]}` — a *paths*
    entry present in this dict but with an EMPTY list means git found no
    commit touching it at all (never committed, or unreadable-by-git); an
    entry present with a non-empty list is capped at `limit`. On any
    subprocess failure/timeout/non-zero exit, returns `{}` (every requested
    path reads as absent — same fail-open posture as `_batch_git_commit_epochs`
    and as a `_path_history` per-path invocation failure, which already
    returned `[]` for that one path).

    § Anti-scope 25 reconciliation (caller-side, see
    `find_rollback_candidates`): a path ABSENT from this dict, or present
    with an empty list, is read as "no history found for this path" and the
    caller treats it as NOT a rollback candidate — the same outcome a
    single-path `_path_history` failure/empty-result already produced before
    this batching change. Failure direction: this function detects
    rollbacks, so a path wrongly resolved to "no history" SUPPRESSES a
    rollback finding that should have fired, never fabricates one — no
    regression from the pre-batch per-path behavior, which had the identical
    fail-open shape one path at a time. Reference shape for the
    absence-reconciliation pattern (cited, not re-derived):
    `coordinator_core/ops/emit/sections/handoffs.py`'s
    `_resolve_shipped_in_dates` (prefix-match plus a `matched` set).
    """
    if not paths:
        return {}
    try:
        result = _run_git(
            [
                "log",
                f"--format=%H{_FIELD_SEP}%s",
                "--raw",
                "--no-renames",
                "--no-abbrev",
                "-z",
                "--",
                *paths,
            ],
            cwd=repo_root,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0 or not result.stdout:
        return {}

    wanted = set(paths)
    history: Dict[str, List[Tuple[str, str, str]]] = {}

    tokens = result.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]

    # State-machine parse, driven by SHAPE (a rawline token always starts
    # with ":" once its possible leading "\n" cushion is stripped; a header
    # token never does) rather than hash-length/charset heuristics — a
    # single commit touching MULTIPLE requested paths emits multiple
    # rawline/path pairs back-to-back before the next header (empirically
    # confirmed against this repo's own history; see this module's test
    # fixtures), which a fixed-stride triple-grouping (`_path_history`'s
    # single-path assumption) cannot parse.
    current_commit: Optional[str] = None
    current_subject: Optional[str] = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        stripped = token.lstrip("\n")
        if stripped.startswith(":"):
            # A raw-diff rawline/path pair for the CURRENT commit.
            if current_commit is None or i + 1 >= len(tokens):
                i += 1
                continue
            path = tokens[i + 1]
            i += 2
            if path not in wanted:
                continue
            parts = stripped[1:].split(" ")
            if len(parts) != 5:
                continue
            _old_mode, _new_mode, _old_sha, new_sha, _status = parts
            bucket = history.setdefault(path, [])
            if len(bucket) < HISTORY_DEPTH_LIMIT:
                bucket.append((current_commit, current_subject, new_sha))
            continue
        # A commit-header token: "<hash><SEP><subject>".
        if _FIELD_SEP not in stripped:
            # Malformed/unexpected record shape — skip rather than mis-parse.
            i += 1
            continue
        current_commit, current_subject = stripped.split(_FIELD_SEP, 1)
        i += 1

    return history


def find_rollback_candidates(
    repo_root: str, env: Optional[dict] = None
) -> List[RollbackCandidate]:
    """Read-only scan of the staged index for exact-older-blob matches.

    Never mutates the repo. Returns one `RollbackCandidate` per staged path
    whose blob matches an older commit's blob for that same path — index 0
    of a path's history is that path's OWN current (HEAD) content and is
    never treated as a match target (see module docstring's threshold
    rationale); the NEAREST older match (smallest depth) is recorded, since
    that is the state actually being restored to.

    History is resolved via ONE batched `_batch_path_history` walk over all
    staged paths (not one `_path_history` spawn per path — the N+1 this
    function used to pay) — see that function's docstring for the
    multi-pathspec shape and the § Anti-scope 25 absence-reconciliation it
    guarantees.
    """
    staged = _staged_blobs(repo_root, env=env)
    all_history = _batch_path_history(repo_root, list(staged.keys()), env=env)
    candidates: List[RollbackCandidate] = []
    for path, staged_blob in staged.items():
        history = all_history.get(path, [])
        if len(history) < 2:
            continue
        for depth, (commit_hash, subject, blob) in enumerate(history):
            if depth == 0:
                continue
            if blob == staged_blob:
                skipped = tuple(
                    SkippedCommit(commit=h, subject=s) for (h, s, _b) in history[:depth]
                )
                candidates.append(
                    RollbackCandidate(
                        path=path,
                        matched_commit=commit_hash,
                        matched_subject=subject,
                        depth=depth,
                        skipped=skipped,
                    )
                )
                break
    return candidates


def _should_fire(candidates: Sequence[RollbackCandidate]) -> bool:
    if len(candidates) >= MIN_ROLLBACK_PATHS:
        return True
    return any(c.depth >= MIN_ROLLBACK_DEPTH for c in candidates)


def _report(candidates: Sequence[RollbackCandidate], overridden: bool) -> str:
    lines: List[str] = []
    verb = "would flag" if overridden else "BLOCKED"
    lines.append(
        f"{_PROG}: {verb} — {len(candidates)} staged path(s) exactly match an older commit's blob:"
    )
    for c in sorted(candidates, key=lambda c: c.path):
        lines.append(
            f"  {c.path} -> matches {c.matched_commit[:12]} "
            f"({c.depth} commit(s) touching this path skipped backwards)"
        )
        lines.append(f"    restoring: {c.matched_subject}")
        if c.skipped:
            lines.append("    work at risk if this is committed:")
            for s in c.skipped:
                lines.append(f"      {s.commit[:12]} {s.subject}")
    if overridden:
        lines.append(
            f"{_PROG}: {OVERRIDE_ENV} is set — proceeding despite the above (deliberate mass revert)."
        )
    else:
        lines.append(
            f"{_PROG}: if this IS a deliberate mass revert, set {OVERRIDE_ENV}=1 and re-run."
        )
    return "\n".join(lines)


_USAGE = """\
usage: detect-staged-rollback [repo-root]

Read-and-report detector for a staged index whose blobs are byte-identical to
an older commit's — i.e. a silent rollback of landed work. Never mutates a
repo. repo-root defaults to the current working directory.

exit codes:
  0  clean (no candidates, below threshold, or {override} set)
  1  a staged-rollback finding crossed the breadth/depth threshold
  2  usage error
""".format(override=OVERRIDE_ENV)


def main(argv: Optional[List[str]] = None, env: Optional[dict] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    env = dict(os.environ) if env is None else dict(env)

    # A leading-dash arg is never a repo root. Without this, `--help` was
    # taken as a path and the CLI died on `FileNotFoundError: '--help'` — a
    # traceback where a usage block belongs. Hand-rolled rather than argparse
    # so exit 2 stays THIS module's usage-error code and does not collide with
    # the trampoline's own transport-failure 2 by accident of argparse's
    # convention (see coordinator/bin/detect-staged-rollback.py's docstring,
    # which enumerates both).
    if argv and argv[0] in ("-h", "--help"):
        print(_USAGE, end="")
        return 0
    if argv and argv[0].startswith("-"):
        print(f"detect-staged-rollback: unknown option {argv[0]!r}\n", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        return 2

    repo_root = argv[0] if argv else "."

    candidates = find_rollback_candidates(repo_root, env=env)
    if not candidates or not _should_fire(candidates):
        return 0

    overridden = env.get(OVERRIDE_ENV, "") not in ("", "0")
    print(_report(candidates, overridden), file=sys.stderr)
    return 0 if overridden else 1


if __name__ == "__main__":
    sys.exit(main())
