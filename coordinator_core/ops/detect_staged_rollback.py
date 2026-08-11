"""
coordinator_core.ops.detect_staged_rollback — staged-index exact-blob rollback
detector, PLUS a staged mass-deletion tripwire.

Purpose: reads the caller's staged index (``git diff --cached``) and flags two
distinct shapes:

1. Exact-blob rollback (the module's original purpose) — the shape a
   2026-07-28 incident produced on this repo: an in-progress index whose
   staged blobs, file by file, were byte-identical to an OLDER commit's blob
   for that same path, silently reverting landed work had it been committed.
2. Staged mass deletion (added 2026-08-10, see
   ``state/bug-backlog/2026-08-10-nothing-on-the-commit-path-can-see-a-mas-486778a10476.yaml``)
   — the shape the SAME incident's root cause actually produced: commit
   ``0a3462b72`` staged git's canonical empty tree
   (``4b825dc642cb6eb9a060e54bf8d69288fbee4904``) against a parent holding
   18,506 files. That commit was invisible to check (1) above because
   ``_staged_blobs`` skips status-``D`` entries BY DESIGN (a deletion has no
   staged blob content to compare against history) — a commit that drops
   every tracked file was, and without this check remains, invisible to this
   module. See ``find_mass_deletion`` / ``MASS_DELETION_RATIO_THRESHOLD`` /
   ``MASS_DELETION_ABS_FLOOR`` below for the detection logic and thresholds.

This module never mutates a repo — it is read-and-report only. It IS wired
into a commit path: it is the sole entry in
``coordinator_core.ops.install_claude_klabauter_precommit_hook._GATE_REGISTRY``, the
registry that installs this repo's own ``.git/hooks/pre-commit`` (via the
``coordinator/bin/detect-staged-rollback.py`` trampoline). As of 2026-08-10
this is the ONLY gate that fires on this repo's commit path — every commit
into claude-klabauter runs through ``main()`` here before it can land. A prior
version of this docstring claimed the opposite ("NOT wired into any commit
path"); that claim was stale the moment the installer above shipped, and its
staleness is exactly why check (2) went unnoticed for a full commit — see the
bug-backlog entry cited above.

Detection logic (rollback, check 1):
    For each staged path, resolve its staged (index) blob sha, then walk that
    path's own commit history (bounded by ``HISTORY_DEPTH_LIMIT`` — see that
    constant's docstring for why 40) looking for an OLDER commit whose blob
    for the same path is byte-identical to the staged blob. A match records
    the matched commit and the "rollback depth" — how many commits touching
    that path were skipped backwards to reach it (1 = the immediately prior
    version of the file; deeper = further back).

Threshold design (rollback): a single file matching an older blob is
ordinary (``git revert``, undoing one bad edit, restoring one file) and must
NOT fire alone unless the match is deep. The signal this module exists to
catch is BREADTH (many files at once) or DEPTH (one file jumping back past
several of its own intervening edits) — never a lone shallow match. See
MIN_ROLLBACK_PATHS / MIN_ROLLBACK_DEPTH docstrings for the numbers.

Detection logic (mass deletion, check 2):
    Reads every staged status-``D`` path (the exact set check 1 discards) and
    the tracked-file total at ``HEAD`` (the pre-commit tree). Fires when
    EITHER the absolute deleted-path count crosses ``MASS_DELETION_ABS_FLOOR``
    OR the deleted-path count is at least ``MASS_DELETION_RATIO_THRESHOLD`` of
    the tracked total — an OR, not an AND, because either signal alone is
    sufficient evidence of an implausible deletion: a huge repo losing a
    proportionally-small-but-absolutely-enormous subtree should fire on the
    floor even if the ratio stays low, and a small repo (or subtree) losing
    nearly everything should fire on the ratio even if the absolute count is
    unremarkable. See MASS_DELETION_ABS_FLOOR / MASS_DELETION_RATIO_THRESHOLD
    docstrings for the numbers and how they were derived from this repo's own
    commit history.

Override (deliberate divergence from a no-override discipline, both checks):
unlike a correctness guard, both checks here have a real and benign
false-positive case — a DELIBERATE mass revert or a DELIBERATE large prune is
legitimate work indistinguishable, from git alone, from the incidents these
checks exist to catch. So this module takes two named override env vars,
one per check, each spelled to match the sibling precommit gates' convention
in ``coordinator_core.ops.install_meta_repo_precommit_hook._GATE_REGISTRY``:
``COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK`` (check 1, pre-existing) and
``COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION`` (check 2, new). Do NOT "fix"
either into a hard, unoverridable block by analogy with a correctness guard
— every one of a correctness guard's refusal cases is a genuine defect;
neither of these is, by construction. A later reader who removes either
override on that analogy would be reintroducing the exact false-positive
these checks were built to tolerate. Per the 2026-08-10 bug-backlog ruling,
BOTH overrides are biased toward NOT firing on plausible legitimate work —
this repo runs ~50 concurrent sessions sharing one hook, and a false positive
here blocks every one of them from committing. Missing a marginal real
incident is the deliberately preferred failure mode over blocking legitimate
work; see the threshold docstrings for the historical margin each one keeps.

Negative-spec:
    - NEVER runs a mutating git command (no ``git reset``, ``git checkout``,
      no staging/unstaging) — read-only over ``git diff --cached`` / ``git
      log`` / ``git rev-parse`` / ``git ls-tree`` only.
    - Does NOT walk the FULL repo history per path (check 1) — bounded by
      ``HISTORY_DEPTH_LIMIT``; a path with a real rollback beyond that depth
      is a known blind spot, not a silent success (see that constant's
      docstring).
    - Does NOT special-case renames (check 1) — invoked with ``--no-renames``
      so a renamed file surfaces as a plain delete + add, not a moved match.
      A rename false-negative (content moved to a new path) is out of scope:
      this module's own remit is "the same path rolled back", not general
      content provenance.
    - Does NOT weight staged deletions by path (check 2) — a deletion of
      ``README.md`` and a deletion of a 40MB generated artifact count
      identically as "one path". Byte-weighting is a different, unbuilt
      detector; this one answers "how much of the TREE, by path count, is
      gone", which is what an empty-tree-shaped incident actually produces.
    - Does NOT accumulate deletions across commits (check 2) — each
      invocation measures only the CURRENT staged commit's deleted-path
      count/ratio against the tracked total at that moment; nothing here
      tracks a running total across a SEQUENCE of commits. A paced split
      (e.g. ~900 deletions per commit across 20 commits on a repo whose
      floor is 5127) can keep both the absolute-floor and ratio legs under
      threshold on every single commit while still emptying the tree over
      the sequence — the exact broader-gap shape the carried backlog item
      ``cf-deletion-tripwire-3a91c7`` names. EXECUTED AND CONFIRMED
      2026-08-11, no longer a reasoned-about gap —
      ``docs/research/spike-verdicts/2026-08-11-paced-deletion-evades-the-mass-deletion-tripwire.md``
      ran these predicates unmodified at production thresholds against a
      6000-file repo deleting HALF the remainder per commit: the guard stayed
      silent for 13 consecutive commits and first fired at commit 14, with
      5999 of 6000 files already gone. That spike also establishes a stronger
      claim than "this is a gap": NO single-commit threshold can close it.
      The 0.90 ratio exists because this repo's largest legitimate bulk
      deletion measured 0.505, so any threshold above that is evaded by
      pacing just under it, and any threshold at or below it blocks a
      deletion this repo's own history calls legitimate. A cross-commit
      window is the only shape that can work. This is an accepted, undisclosed
      (until now) gap: closing it needs a bounded rolling-window check (sum
      of staged-D counts across the last N pre-commit invocations), which is
      a distinct, unbuilt detector, not a fix to the per-commit measurement
      this module makes.
    - Does NOT scope the mass-deletion check (or the rollback check) to the
      pathspec a `git commit -- <path>` will actually write (check 2, also
      check 1 via `_staged_blobs`) — both read the FULL staged index via an
      unscoped ``git diff --cached --raw``, not the subset of the index a
      pathspec-scoped commit will turn into tree content (git uses HEAD
      content for unlisted paths in that mode). A developer with unrelated
      deletions staged elsewhere who runs a narrow `git commit -- some/file`
      is evaluated against the WHOLE staged set. This is NOT a fixable
      oversight in this module: a standard git pre-commit hook is invoked
      with no arguments and no environment variable carrying the commit's
      pathspec (confirmed by reading
      ``coordinator_core.ops.install_claude_klabauter_precommit_hook`` and
      ``coordinator/bin/detect-staged-rollback.py`` — both forward/receive
      only ``[repo-root]``, never the invoking `git commit`'s own argv) — a
      pre-commit hook has no git-provided way to see which pathspec the
      commit that triggered it will restrict itself to. Named here as a
      git-hook-API limitation, not silently accepted.
    - Does NOT itself decide whether to block anything beyond its own exit
      code — it is a read/report library plus a CLI; the installed hook
      (``coordinator_core.ops.install_claude_klabauter_precommit_hook``) clamps that
      exit code to 0/1 at the commit boundary (see that module's own
      docstring, "Exit-code clamping").
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

# --- Mass-deletion tripwire (2026-08-10) ---------------------------------
#
# Both constants below were derived from this repo's OWN commit history, not
# picked as round numbers — see
# state/bug-backlog/2026-08-10-nothing-on-the-commit-path-can-see-a-mas-486778a10476.yaml
# for the incident this exists to catch (commit 0a3462b72, which staged
# git's canonical empty tree against an 18,506-file parent — every one of
# those 18,506 paths a status-D entry, deleted_count/tracked_total == 1.0).
#
# A full sweep of every commit in this repo's history (`git log --all
# --name-status`, ~13,200 commits) for its per-commit status-D count, cross-
# referenced against the tracked-file total at that commit's PARENT (the
# pre-commit tree, matching what this module measures at commit time), found
# exactly one commit at ratio 1.0 (the incident itself, 18,506/18,506) and a
# clear runner-up at ratio 0.505 (1,709/3,382 — `e6783a68bd0`, "prune(reclaim):
# drop 1,709 pre-July example-doctrine-mirror-repo files reclaimed by example-doctrine-repo", a deliberate,
# legitimate bulk prune). Every other commit with >=15 deletions in the same
# sweep sits at ratio <= 0.102. There is no commit anywhere between 0.505 and
# 1.0 — the two shapes (largest legitimate prune vs. the incident) are not
# close together, which is what makes a threshold between them defensible
# rather than arbitrary.
MASS_DELETION_RATIO_THRESHOLD = 0.90
"""Fire when staged deletions are >= this fraction of the tracked total at
HEAD. 0.90 sits with ~1.8x headroom above the largest legitimate single-commit
deletion ratio ever observed in this repo (0.505) while leaving 0.10 of
margin below the incident's 1.0 — i.e. up to 10% of the tree could survive a
staged commit and this would still fire, but a commit leaving MORE than 10%
of the tree intact (as every real prune in this repo's history has) does not.
Per the ~50-concurrent-session load constraint, the margin above the
historical legitimate max (0.505) is deliberately generous rather than tight
against it — a future prune materially larger than `e6783a68bd0` (but still a
genuine partial prune, not a total wipe) must not trip this."""

MASS_DELETION_ABS_FLOOR = 5127
"""Fire when the ABSOLUTE staged-deletion count crosses this, regardless of
ratio (catches a huge deletion inside a huge repo that stays proportionally
small). Derived as 3x the largest legitimate single-commit deletion COUNT
ever observed in this repo's history (1,709, the same `e6783a68bd0` commit
the ratio threshold is keyed to) — a 3x safety margin above the historical
worst case, biased toward the same "miss a marginal case over blocking
legitimate work" preference the ratio threshold uses. This floor is a
belt-and-braces catch for a repo shape where the ratio leg could stay under
threshold (e.g. a 200,000-file monorepo losing 10,000 files is only a 5%
ratio, but 10,000 deleted files is not a plausible ordinary edit) — the two
legs are OR'd together (`_mass_deletion_should_fire`), not AND'd, precisely
so neither shape has to also satisfy the other to be caught."""

MASS_DELETION_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION"

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


def _staged_raw_diff(repo_root: str, env: Optional[dict] = None) -> List[str]:
    """Shared ``git diff --cached --raw -z --no-renames --no-abbrev`` spawn,
    tokenized on NUL with the trailing empty token trimmed.

    Both `_staged_blobs` and `_staged_deletions` need the identical staged-
    index raw-diff scan — factored out here so `main()` pays for it once,
    not twice, per commit (real cost on this repo's ~50-concurrent-session
    load norm: every commit on every session was paying this spawn twice).
    Deliberately returns the bare token list, not a parsed dict/list, so
    neither caller's own parsing (and neither caller's return CONTRACT —
    `_staged_blobs`'s dict shape is pinned by `find_rollback_candidates`'s
    tests) has to change to use it.
    """
    result = _run_git(
        ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev"],
        cwd=repo_root,
        env=env,
    )
    if result.returncode != 0:
        return []
    tokens = result.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens = tokens[:-1]
    return tokens


def _staged_blobs(
    repo_root: str,
    env: Optional[dict] = None,
    tokens: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Path -> full staged (index) blob sha for every staged, non-deleted path.

    One `git diff --cached --raw -z --no-renames --no-abbrev` call for the
    WHOLE staged set — not one call per path — is the perf-critical piece:
    this is O(1) git invocations in the number of staged paths, not O(N).
    The spawn itself is shared with `_staged_deletions` via `_staged_raw_diff`;
    pass a pre-fetched `tokens` (as `main()` does) to avoid paying for it a
    second time, or omit it to have this function fetch its own (unchanged
    standalone behavior, and what every existing caller/test still gets).

    Record shape per staged path, NUL-delimited (`-z`):
        ":<oldmode> <newmode> <oldsha> <newsha> <status>\\0<path>\\0"
    concatenated back-to-back with no separator between records (confirmed
    empirically — see this module's test fixtures). Splitting the whole
    output on NUL therefore yields flat (rawline, path) pairs in sequence.

    Deleted paths (status "D", newsha all-zero) are skipped — a deletion has
    no staged content to compare against a historical blob.
    """
    if tokens is None:
        tokens = _staged_raw_diff(repo_root, env=env)

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
    repo_root: str, env: Optional[dict] = None, tokens: Optional[List[str]] = None
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

    `tokens`, if given (as `main()` does), is a pre-fetched `_staged_raw_diff`
    result forwarded straight to `_staged_blobs` to avoid a duplicate spawn;
    omit it to have `_staged_blobs` fetch its own (unchanged standalone
    behavior).
    """
    staged = _staged_blobs(repo_root, env=env, tokens=tokens)
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


@dataclass(frozen=True)
class MassDeletionFinding:
    deleted_count: int
    tracked_total: int
    ratio: float  # 0.0 when tracked_total is 0 (nothing to divide by)
    deleted_paths: Tuple[str, ...] = field(default_factory=tuple)


def _staged_deletions(
    repo_root: str,
    env: Optional[dict] = None,
    tokens: Optional[List[str]] = None,
) -> List[str]:
    """Every staged status-D path — the exact set `_staged_blobs` discards.

    Same single `git diff --cached --raw -z --no-renames --no-abbrev` shape
    (and the same record parsing) as `_staged_blobs`, kept as an independent
    PARSING pass rather than folded into that function: `_staged_blobs` is a
    hot path for the rollback check and its contract (deleted paths absent
    from the returned dict) is depended on by `find_rollback_candidates` and
    pinned by that function's own tests — branching its return shape to also
    carry deletions would be a wider, riskier change for no shared benefit,
    since this function's caller (`find_mass_deletion`) needs nothing else
    from the diff. The SPAWN itself, however, is shared via
    `_staged_raw_diff` — pass a pre-fetched `tokens` (as `main()` does) to
    avoid a second `git diff --cached` process per commit; omit it to have
    this function fetch its own (unchanged standalone behavior).
    """
    if tokens is None:
        tokens = _staged_raw_diff(repo_root, env=env)

    deleted: List[str] = []
    i = 0
    while i + 1 < len(tokens):
        rawline = tokens[i]
        path = tokens[i + 1]
        i += 2
        if not rawline.startswith(":"):
            continue
        parts = rawline[1:].split(" ")
        if len(parts) != 5:
            continue
        _old_mode, _new_mode, _old_sha, _new_sha, status = parts
        if status.startswith("D"):
            deleted.append(path)
    return deleted


def _tracked_file_count(repo_root: str, env: Optional[dict] = None) -> int:
    """Tracked-file total at HEAD — the pre-commit tree the staged deletions
    are being measured against. 0 on any failure (no HEAD yet, e.g. a repo's
    first commit — nothing can be a "mass deletion" of a tree that does not
    exist yet; the ratio leg is skipped in that case and only the absolute
    floor can fire, which is correct: an initial commit cannot delete
    anything that was ever tracked)."""
    result = _run_git(
        ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
        cwd=repo_root,
        env=env,
    )
    if result.returncode != 0:
        return 0
    tokens = result.stdout.split("\0")
    return sum(1 for t in tokens if t)


def find_mass_deletion(
    repo_root: str, env: Optional[dict] = None, tokens: Optional[List[str]] = None
) -> Optional[MassDeletionFinding]:
    """Read-only scan of the staged index for an implausible-share deletion.

    Never mutates the repo. Returns None when nothing is staged for
    deletion — a `MassDeletionFinding` otherwise, regardless of whether it
    crosses either threshold (`_mass_deletion_should_fire` makes that call);
    this function is measurement only, mirroring `find_rollback_candidates`'s
    own separation of "what was found" from "does it fire".

    `tokens`, if given (as `main()` does), is a pre-fetched `_staged_raw_diff`
    result forwarded straight to `_staged_deletions` to avoid a duplicate
    spawn; omit it to have `_staged_deletions` fetch its own (unchanged
    standalone behavior).
    """
    deleted = _staged_deletions(repo_root, env=env, tokens=tokens)
    if not deleted:
        return None
    total = _tracked_file_count(repo_root, env=env)
    ratio = (len(deleted) / total) if total else 0.0
    return MassDeletionFinding(
        deleted_count=len(deleted),
        tracked_total=total,
        ratio=ratio,
        deleted_paths=tuple(deleted),
    )


def _mass_deletion_should_fire(finding: Optional[MassDeletionFinding]) -> bool:
    if finding is None:
        return False
    if finding.deleted_count >= MASS_DELETION_ABS_FLOOR:
        return True
    return finding.tracked_total > 0 and finding.ratio >= MASS_DELETION_RATIO_THRESHOLD


def _mass_deletion_report(finding: MassDeletionFinding, overridden: bool) -> str:
    verb = "would flag" if overridden else "BLOCKED"
    pct = f"{finding.ratio * 100:.1f}%" if finding.tracked_total else "n/a (no HEAD)"
    lines = [
        f"{_PROG}: {verb} — staged commit deletes {finding.deleted_count} of "
        f"{finding.tracked_total} tracked path(s) ({pct}).",
    ]
    sample = sorted(finding.deleted_paths)[:10]
    for p in sample:
        lines.append(f"  D  {p}")
    remaining = finding.deleted_count - len(sample)
    if remaining > 0:
        lines.append(f"  ... and {remaining} more")
    if overridden:
        lines.append(
            f"{_PROG}: {MASS_DELETION_OVERRIDE_ENV} is set — proceeding despite the above "
            "(deliberate mass deletion)."
        )
    else:
        lines.append(
            f"{_PROG}: if this IS a deliberate mass deletion (archival sweep, bulk prune), "
            f"set {MASS_DELETION_OVERRIDE_ENV}=1 and re-run."
        )
    return "\n".join(lines)


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

Read-and-report detector for two staged-index shapes: (1) blobs byte-identical
to an older commit's — i.e. a silent rollback of landed work — and (2) a
staged deletion covering an implausible share of the tracked tree. Never
mutates a repo. repo-root defaults to the current working directory.

exit codes:
  0  clean (no finding, below threshold, or the relevant override is set)
  1  a rollback finding crossed the breadth/depth threshold, and/or a mass-
     deletion finding crossed the ratio/floor threshold
  2  usage error

overrides:
  {rollback_override}  bypasses a rollback finding (deliberate mass revert)
  {mass_override}  bypasses a mass-deletion finding (deliberate bulk prune)
""".format(rollback_override=OVERRIDE_ENV, mass_override=MASS_DELETION_OVERRIDE_ENV)


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

    # ONE `git diff --cached --raw` spawn shared by both checks below — each
    # of `_staged_blobs` and `_staged_deletions` used to run this identical
    # command independently, doubling the per-commit git spawn count for no
    # benefit (real cost at ~50 concurrent sessions, each paying it on every
    # commit). See `_staged_raw_diff`'s docstring.
    raw_diff_tokens = _staged_raw_diff(repo_root, env=env)

    # Both checks always run, independently — a commit can trip either, both,
    # or neither, and each has its own override (see module docstring,
    # "Override"). Neither check short-circuits the other: a rollback finding
    # below its own threshold must not suppress a mass-deletion finding
    # staged alongside it, and vice versa.
    candidates = find_rollback_candidates(repo_root, env=env, tokens=raw_diff_tokens)
    rollback_fires = bool(candidates) and _should_fire(candidates)
    rollback_overridden = env.get(OVERRIDE_ENV, "") not in ("", "0")

    mass_finding = find_mass_deletion(repo_root, env=env, tokens=raw_diff_tokens)
    mass_fires = _mass_deletion_should_fire(mass_finding)
    mass_overridden = env.get(MASS_DELETION_OVERRIDE_ENV, "") not in ("", "0")

    if not rollback_fires and not mass_fires:
        return 0

    blocked = False
    if rollback_fires:
        print(_report(candidates, rollback_overridden), file=sys.stderr)
        blocked = blocked or not rollback_overridden
    if mass_fires:
        assert mass_finding is not None  # _mass_deletion_should_fire(None) is False
        print(_mass_deletion_report(mass_finding, mass_overridden), file=sys.stderr)
        blocked = blocked or not mass_overridden

    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
