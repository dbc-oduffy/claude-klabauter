"""
coordinator_core.workstream_complete.directives_commit_tail — the
dirty-tree, commit-tail, push-verification, claim-release, cadence and
final-summary builders for the `workstream-complete-assemble` computed-skill
engine.

Purpose: computes Steps 3.0/3/3.5/3.6/4 (`SKILL.md` census rows
`d-accumulate-session-paths`, `d-run-wsc-tail`, `d-verify-push-landed`,
`d-release-plan-claim`, `d-emit-cadence`,
`d-render-final-summary`) for `coordinator_core.workstream_complete`'s
`brief()` assembly seam (C3) — the terminal leg of the ceremony, run only
after every earlier directive/judgment has resolved. Mirrors
`directives_session_hygiene.py`'s directives-vs-gates split: a mutating
step (a real, on-disk, never-invoked-in-process CLI) becomes a
`directives[]` entry; a step whose "work" is reading disk/git state or
folding already-computed one-liners into a fixed template becomes a plain
read-only function this module exposes directly, per the same "compute a
fact, don't invent a directive" disposition that module's own docstring
documents.

This module is one of seven siblings (directives_lessons_plan.py,
directives_completion.py, directives_memo_lifecycle.py,
directives_review.py, directives_session_hygiene.py, judgments.py) built
under the multi-module-assembler convention `docs/plans/2026-07-26-
workstream-complete-computed-frontage.md` (D-4) sets for this tree:
`__init__.py` is retained as the assembly + CLI seam ONLY, and every
submodule exposes pure, `__init__`-independent builder functions.

Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md,
chunk C2e. Source census: state/plan-sidecars/2026-07-26-workstream-complete-
computed-frontage.census-steps.md, Step 3.0/3/3.5/3.6/4 rows.

Consumes (orchestrates, reimplements none):
    coordinator/bin/wsc-close.py (tail-args subcommand)
        -> build_close_tail_args_directive's directives[].cli. (The
        `archive-session` subcommand is still live but is no longer
        invoked from this module — see the Step 3.5 section below.)
    coordinator/bin/wsc-tail.py -> build_wsc_tail_directive's directives[].cli.
    coordinator/bin/session-claim-cli (release-artifact) ->
        build_release_plan_claim_directive's directives[].cli.
    coordinator/bin/emit-cadence.py -> build_emit_cadence_directive's
        directives[].cli, via the shared
        `coordinator_core.ceremony_common.tail.build_ceremony_close_tail`
        factor (see § Design note below for why only its emit-cadence half
        is taken).
    `git log`/`git branch -r --contains` (read-only) ->
        compute_push_landed_gate's own subprocess calls, mirroring
        `resolve_repo_root`'s existing precedent of a plain read-only
        subprocess rather than an in-process git read-model (out of scope
        here per this package's own module docstring).

Design note — why `build_ceremony_close_tail` contributes only its
emit-cadence half here. The "Third surface" decision
(docs/plans/2026-07-26-workstream-complete-computed-frontage.md, D-1
addendum) directs this module to consume `build_ceremony_close_tail`
for the `d-emit-cadence` tail pair rather than re-deriving it — but
`build_ceremony_close_tail` returns a PAIR (`coordinator-ceremony-hook`
then `emit-cadence`), and workstream-complete, unlike workday-complete and
workweek-complete, has no post-command-hook step anywhere in its own
census or `SKILL.md` (verified: `grep -c ceremony-hook
coordinator/skills/workstream-complete/SKILL.md` -> `0`; the command-
payload-inventory audit's "run project post-ceremony command hook" row
lists `workday-start`/`workday-complete`/`workweek-start`/`workweek-
complete` but never `workstream-complete`). Emitting the hook half here
would be a phantom directive with no source-of-truth step behind it —
exactly the failure class AC2's phantom-verb guard exists to catch. This
chunk resolves the tension by taking ONLY `build_ceremony_close_tail`'s
second (emit-cadence) element — still sharing the family factor's shape
(cli/args/depends_on/already_satisfied) rather than hand-deriving a third
copy, while not inventing a step this ceremony does not have. Flagged
explicitly as a decision this chunk made that the plan body left open,
per this package's own precedent for such flags
(`directives_session_hygiene.py`'s completeness-checklist-gate note).

Negative-spec:
    - Does NOT decide commit subject/prose text, the pinboard note, which
      concurrent-EM disposition to pick for a case-(c) unattributable file,
      or the Step-4 "Work done"/flag-severity classification prose —
      `commit-message-authoring`, `unattributable-file-disposition`,
      `concurrent-peer-attribution`, `session-work-summary`, and
      `flag-severity-classification` are C2f's (`judgments.py`) judgment
      points. This module only turns ALREADY-DECIDED text into directive
      args or a rendered summary string.
    - Does NOT implement the dirty-tree classifier itself
      (`d-classify-dirty-tree`) — per the census, that step is already
      engine-owned inside `ceremony.wsc_tail`'s own `commit_gates` and has
      nothing separate for this assembler to compute or name.
    - Does NOT restate the "never `git add -A`" invariant anywhere in this
      module. That invariant is already enforced inside the op
      (`coordinator_core.bash_guards.block_blanket_git_add`) and DR-090's
      own worked example names restating an already-enforced invariant at
      the call site as the antipattern the conversion exists to remove —
      this module's directives pass an explicit `--stage-paths`/pathspec
      list and nothing broader, full stop.
    - Does NOT invoke any of the named CLIs in-process. Every function
      below only reads disk/git state or returns a `directives[]` entry
      naming an existing CLI; mutation happens only when the apply half
      later executes that entry.
    - Does NOT retry a `wsc-tail` client-side timeout automatically.
      `describe_wsc_tail_outcome`'s `"timeout"` entry names RECONCILE
      (check actual repo state before deciding anything failed) as the
      required next move, never a blind retry — see its own docstring.
    - DOES retry, narrowly: `_chunked_committed_paths`' per-chunk git spawn
      (`_run_git_ok_retrying`, `_GIT_RETRY_ATTEMPTS`) is the ONE exception
      to the no-retry posture above, and it is not a contradiction of it —
      that entry is about not blind-retrying an ALREADY-REPORTED-SUCCESS
      client-side timeout; this one is a bounded retry of a git SPAWN that
      has not yet succeeded, absorbing routine lock contention this
      machine's load norm makes common. Fail-closed is preserved either
      way: exhausting the retry budget still raises
      `PeerAttributionUnavailable`, never degrades to an empty/partial
      result. Do not "simplify" this retry away, and do not widen it into a
      general-purpose retry wrapper reused elsewhere in this module — it is
      scoped to this one call site's known failure mode.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, NamedTuple, Optional, Set, Union

from coordinator_core.ceremony_common.tail import build_ceremony_close_tail
from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _session_liveness
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_memo_lifecycle as _memo_lifecycle

_NO_CONSOLE = no_console_creationflags()


def _directive(
    id_: str,
    cli: str,
    args: list[str],
    depends_on: Any = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on, "already_satisfied": already_satisfied}


# ---------------------------------------------------------------------------
# Step 3.0 tail / Step 3 WSC_PATHS — d-accumulate-session-paths
# ---------------------------------------------------------------------------


def accumulate_session_paths(
    session_authored_paths: Iterable[str],
    deletion_paths: Iterable[str] = (),
    sidecar_paths: Iterable[str] = (),
) -> list[str]:
    """Step 3's `WSC_PATHS` capture, done once: folds the session-authored
    path set Step 2.67 already computed, its `git rm` deletion paths, and
    any preserved Step 2.6b run-report sidecar deletions into one deduped,
    order-preserving pathspec list — the same set drives both `git add`
    and `git commit` per the SKILL's own "capture ONCE, reuse for both"
    discipline (2026-06-24 shared-index absorption incident).

    Pure aggregation, not a `directives[].cli` entry: per the census
    (`d-accumulate-session-paths` row), this is "fully derivable by the
    engine from the same session-authored-file tracking Step 2.67 already
    computes" — there is no separate mutating CLI to name, only a set
    union already known by the time this step runs. Callers splice the
    result into `build_close_tail_args_directive`'s / `build_wsc_tail_
    directive`'s `--stage-paths` argument.
    """
    seen: dict[str, None] = {}
    for path in (*session_authored_paths, *deletion_paths, *sidecar_paths):
        if path:
            seen.setdefault(str(path), None)
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Step 3.0 case-(b) — d-resolve-known-concurrent-paths (C2e producer)
# ---------------------------------------------------------------------------


def _spawn_git(repo_root: "Union[Path, str, None]", args: list[str]) -> "tuple[int, str, str]":
    """The one `subprocess.run` body every read-only git spawn in this
    module funnels through — `timeout=30`/`**_NO_CONSOLE` (the Windows
    no-console-window flag) set in exactly one place rather than once per
    call site. Never raises: a spawn failure (missing git, non-repo,
    `OSError`, timeout) collapses to `(1, "", "spawn failed")`, the same
    shape a bare nonzero-exit git failure already produces — callers that
    need a different failure shape (`_run_git_ok`'s `None`) adapt this
    return value themselves; this helper does not pick a contract for
    them.

    `repo_root` widens to `str`/`None` only because the `GitRunner` callback
    shape `bulk_trailer_session_map` invokes passes its `cwd` as
    `Optional[str]`. A `None` stringifies to a bogus path and the spawn
    fails closed into `(1, "", "spawn failed")` — pre-existing behaviour of
    the call site this consolidated, preserved deliberately rather than
    tightened here, where a new raise would change a caller's contract."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, "", "spawn failed"
    return proc.returncode, proc.stdout, proc.stderr


def _run_git_ok(repo_root: Path, args: list[str]) -> Optional[str]:
    """Runs a read-only git subcommand from `repo_root`; returns stdout on a
    clean (rc==0) run, `None` on any failure (missing git, non-repo, spawn
    error, timeout) — mirrors this module's other subprocess call sites
    (`compute_push_landed_gate`), never raises."""
    rc, stdout, _stderr = _spawn_git(repo_root, args)
    if rc != 0:
        return None
    return stdout


def _peer_subagent_share_paths(repo_root: Path, sid: str) -> set[str]:
    """A peer session's `state/subagent-share/<sid>/` surface, in every
    shape `git status --porcelain` might report it: the directory itself
    (git's default collapsed-untracked-directory line, e.g. `?? state/
    subagent-share/<sid>/`), plus every FILE actually present underneath it
    (the shape `git status -uall` — or a partially-tracked dir — would
    report instead). Emitting both costs nothing (an entry that never
    appears in a given `git status` invocation just never matches anything
    in `classify_session_authored_files`'s exact-membership check) and
    avoids silently missing whichever shape the caller's git config
    produces."""
    rel_dir = f"state/subagent-share/{sid}"
    abs_dir = repo_root / "state" / "subagent-share" / sid
    paths: set[str] = {f"{rel_dir}/"}
    try:
        if abs_dir.is_dir():
            for candidate in abs_dir.rglob("*"):
                if candidate.is_file():
                    paths.add(candidate.relative_to(repo_root).as_posix())
    except OSError:
        pass
    return paths


#: Mirrors `archive_stamp._SESSION_ID_UUID_RE` verbatim (deliberately NOT
#: imported from there — `archive_stamp` pulls in `coordinator_core.ops`,
#: and `coordinator_core.ops` eager-imports every op module, some of which
#: import THIS package at module scope; a top-level import of `archive_stamp`
#: here risks the identical partially-initialized-module cycle
#: `commit_reality.py` already documented and worked around. `archive_stamp`
#: itself replicates this SAME regex rather than importing coverage.py's copy
#: — see its own "Ownership guard" comment — so this is the same small,
#: well-tested mechanism replicated a third time, not reinvented).
_SESSION_ID_UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]+[0-9a-fA-F]$")

#: Header line marker for `_committed_paths_for_sids`'s batched
#: `git log --no-walk --name-only` output — mirrors coverage.py's own
#: `_COMMIT_HEADER_SENTINEL`, a control byte that cannot appear in a
#: legitimate path or SHA.
_COMMIT_HEADER_SENTINEL = "\x02"

#: Chunk size for `_committed_paths_for_sids`'s Spawn-2 batched
#: `git log --no-walk -c --name-only <shas>` call — mirrors coverage.py's
#: `_TRAILER_LOOKUP_CHUNK = 300` idiom (coverage.py:1611, "keeps each
#: spawn's argv comfortably under Windows' ~32K command-line length
#: ceiling"). At ~41 bytes/sha (40-hex + separator), 300 shas is ~12.3KB —
#: well under the ~32767-char Windows `CreateProcess` ceiling with ample
#: headroom for the rest of this call's argv. Review: code-reviewer P1 —
#: an unchunked union of `all_shas` overflows around 790-800 shas, which
#: this machine's stated load norm (50-70 concurrent sessions, ~958
#: commits/24h) reaches in ordinary operation, not as a contrived edge
#: case.
_COMMITTED_PATHS_CHUNK = 300

#: Whether `build_close_tail_args_directive` may emit `--review-slice` flags for
#: a list-shaped `decisions["review"]`.
#:
#: TRUE since the consuming half landed alongside it: `wsc_tail.
#: _run_precommit_tail` branches dict-vs-list and routes a list to `tail_ops.
#: write_review_trail_many`, so `--review-slice` now reaches an op that
#: understands it.
#:
#: The constant is retained rather than inlined because it names the coupling
#: this emission has: without a consuming `wsc_tail.py`, the plumbing below is a
#: TRAP rather than dead code — the directive path already accepts a list
#: (`build_write_trail_directives`, a3d90f24dc16), so a list-shaped review emits
#: `--review-slice`, reaches `bin/wsc-tail.py`, and hands the op a list where
#: `tail_ops.write_review_trail` expects a dict. Flipping it back off, or landing
#: either half without the other, re-opens exactly that.
_COMMIT_TAIL_ACCEPTS_REVIEW_SLICES = True

#: Bounded retry budget for a single `_chunked_committed_paths` chunk spawn.
#: `_run_git_ok`/`_spawn_git` themselves carry NO retry of any kind (verified
#: by reading both bodies — `_spawn_git` maps a spawn error straight to
#: `(1, "", "spawn failed")` and `_run_git_ok` maps any nonzero rc straight to
#: `None`, one layer, no loop); the fail-closed conversion (85a36676a) added
#: none either, so a single momentary lock collision now aborts the whole
#: `/workstream-complete` commit tail. This constant is the ONE retry layer
#: for this call site — do not add a second one lower in the stack, and do
#: not remove this one and call the git call "flaky, just fail closed" -
#: fail-closed on a STRUCTURAL failure is correct; failing closed on routine
#: lock contention is not.
#:
#: 3 total attempts (1 original + 2 retries), 0.25s/0.5s backoff — the
#: 0.75s figure is the backoff-SLEEP total only, NOT this layer's total
#: added latency (see `_GIT_RETRY_DEADLINE_SECONDS` below for the number
#: that actually bounds it). `_spawn_git`'s `timeout=30` is PER ATTEMPT,
#: not per call site: a chunk whose `git log` hangs (filesystem
#: contention slow enough to approach the timeout, rather than an
#: instant nonzero-rc rejection — a realistic shape on this machine's
#: documented load norm, `docs/wiki/machine-load-norm.md`: 50-70
#: concurrent LLM sessions, a dozen-plus EMs sharing one checkout) could,
#: absent a deadline, burn up to 3x30s = 90s for a SINGLE chunk before
#: this layer gives up, with `ceil(all_shas/300)` such chunks running
#: sequentially. Review: code-reviewer P2 — the prior comment here
#: claimed "~0.75s worst-case added latency per chunk" and called that
#: "small relative to _spawn_git's existing 30s per-call timeout", which
#: conflated the backoff-sleep total with the retry layer's actual worst
#: case and understated it by two orders of magnitude in the hang case.
#: `_GIT_RETRY_DEADLINE_SECONDS` below is what now bounds the true
#: worst case; do not restate a "small" latency claim here without also
#: naming the deadline.
_GIT_RETRY_ATTEMPTS = 3
_GIT_RETRY_BACKOFF_SECONDS = (0.25, 0.5)

#: Overall wall-clock budget across `_run_git_ok_retrying`'s attempts,
#: sized against `docs/wiki/machine-load-norm.md` (50-70 concurrent
#: sessions, routine transient `index.lock`/pack-refs contention) rather
#: than an idle box: large enough to absorb a one-off lock collision (the
#: 0.25s/0.5s backoff pair plus a couple of fast-failing spawns fits
#: comfortably inside it), small enough that a slow-spawn hang cannot
#: stack multiple full 30s `_spawn_git` timeouts behind one chunk. Once
#: the deadline has passed, `_run_git_ok_retrying` does not start another
#: attempt — a retry whose remaining budget cannot fit another attempt
#: must not begin one. This does NOT cap an attempt already in flight
#: (Python has no thread-cancellation primitive to abort a running
#: `subprocess.run`, and `_spawn_git`'s own `timeout=30` is what bounds
#: that): true worst case per chunk is therefore
#: `_GIT_RETRY_DEADLINE_SECONDS` (spent on attempts that fail fast/retry)
#: PLUS one final in-flight attempt's own `_spawn_git` timeout (30s) —
#: i.e. ~40s per chunk, not the un-bounded ~90s the retry layer would
#: otherwise risk, and nowhere near the previously-claimed 0.75s.
_GIT_RETRY_DEADLINE_SECONDS = 10.0

#: NOT a stderr-shape classifier that skips retries for "obviously
#: structural" failures — considered and deliberately rejected. Git's
#: lock-contention stderr shape (`index.lock`/`unable to create`/`cannot
#: lock ref`) is not perfectly disjoint from every structural message across
#: git versions/locales, and a classifier that guesses wrong in the
#: skip-retry direction would silently shorten the retry budget for a
#: genuinely transient failure — reintroducing, one layer down, the exact
#: fail-open-shaped risk this retry exists to close. Retrying a confirmed-
#: structural failure instead wastes at most `_GIT_RETRY_ATTEMPTS - 1`
#: cheap, bounded spawns before `PeerAttributionUnavailable` still raises —
#: harmless per this task's own stated weighting ("retrying a structural
#: failure is wasted time but harmless; not retrying a transient one wedges
#: a close"). So every failure, recognized or not, gets the same uniform
#: bounded budget below.


def _run_git_ok_retrying(
    repo_root: Path,
    args: list[str],
    *,
    now_fn: "Any" = time.monotonic,
) -> Optional[str]:
    """`_run_git_ok`, called in a loop under `_GIT_RETRY_ATTEMPTS`'s bounded,
    uniform retry budget — see that constant's own docstring for why this
    layer exists, why it is the only one, and why it does not classify
    failures. Loops over `_run_git_ok` itself (not a second inline rc==0
    check against `_spawn_git`) so a future change to `_run_git_ok`
    (logging, stderr capture, a stdout transform) reaches this path
    automatically rather than needing a second edit.

    `_GIT_RETRY_DEADLINE_SECONDS` bounds the overall wall-clock spent
    STARTING attempts — checked before every attempt after the first, so a
    retry whose remaining budget cannot fit another attempt never starts
    one; see that constant's own docstring for why an attempt already in
    flight is not itself cut short. `now_fn` is injectable (defaults to
    `time.monotonic`) so the deadline is testable without a real wait.

    Returns `None` (same contract as `_run_git_ok`) once the attempt budget
    or the deadline is exhausted, whichever comes first — the caller's
    existing fail-closed `raise PeerAttributionUnavailable` on a `None`
    result is unchanged."""
    deadline = now_fn() + _GIT_RETRY_DEADLINE_SECONDS
    for attempt in range(_GIT_RETRY_ATTEMPTS):
        if attempt > 0 and now_fn() >= deadline:
            break
        result = _run_git_ok(repo_root, args)
        if result is not None:
            return result
        if attempt < _GIT_RETRY_ATTEMPTS - 1:
            time.sleep(_GIT_RETRY_BACKOFF_SECONDS[attempt])
    return None


class PeerAttributionUnavailable(RuntimeError):
    """Raised by `_committed_paths_for_sids` when EITHER backing git spawn
    (the bulk trailer walk, or any chunk of the batched touched-paths walk)
    fails. Deliberately NOT swallowed to an empty-set result the way the
    pre-batching per-sha loop degraded — an empty peer-exclusion set reads
    to every caller as "no peer owns any of these paths", which is exactly
    the worst-outcome class `resolve_known_concurrent_paths` exists to
    prevent (a peer's uncommitted work silently swept into this session's
    commit). Same fail-closed precedent as `session_attribution.
    GitLogFailed` (which this wraps for the trailer-walk case) and
    `coverage.py`'s `_bulk_trailer_lookup`'s "never a partial map" contract
    (which this mirrors for the chunked touched-paths case: ANY chunk
    failure invalidates the whole union rather than returning a partial
    one). Left uncaught, this propagates out of `resolve_known_concurrent_
    paths` through `directives_commit_tail`'s callers in `__init__.py`
    (none of which currently wrap this call in a try/except) — a loud
    failure of the whole ceremony pass is the correct fail-closed outcome
    here, not a quiet empty exclusion set that lets the commit proceed
    wrong.
    """


def _chunked_committed_paths(
    repo_root: Path, all_shas: "list[str]"
) -> "Dict[str, Set[str]]":
    """Spawn 2 of `_committed_paths_for_sids`, chunked at
    `_COMMITTED_PATHS_CHUNK` shas per `git log --no-walk -c --name-only`
    call (never one call per sha — see
    `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`, the
    standing guard this must not re-trip). Returns {sha: touched_paths},
    the union across every chunk. Raises `PeerAttributionUnavailable` on
    the FIRST chunk that STILL fails after `_run_git_ok_retrying`'s bounded
    retry budget is exhausted — matches `coverage.py`'s `_bulk_trailer_
    lookup`'s "never a partial map" posture; a partial union here would
    silently under-report a peer's touched paths for whichever shas fell in
    the failed chunk, which is the exact fail-open outcome this function
    exists to close. The retry layer (see `_GIT_RETRY_ATTEMPTS`'s own
    docstring) absorbs routine transient lock contention from a concurrent
    peer's git operation before this fail-closed raise ever fires — it does
    not weaken the raise itself.
    """
    touched_by_sha: "Dict[str, Set[str]]" = {}
    for i in range(0, len(all_shas), _COMMITTED_PATHS_CHUNK):
        chunk = all_shas[i : i + _COMMITTED_PATHS_CHUNK]
        touched_out = _run_git_ok_retrying(
            repo_root,
            ["log", "--no-walk", "-c", f"--format={_COMMIT_HEADER_SENTINEL}%H", "--name-only", *chunk],
        )
        if touched_out is None:
            raise PeerAttributionUnavailable(
                f"git log --no-walk -c --name-only failed for a {len(chunk)}-sha chunk "
                f"({i}:{i + len(chunk)} of {len(all_shas)} total) while resolving peer-"
                "committed paths — refusing to return a partial/empty union."
            )
        current_sha: Optional[str] = None
        for line in touched_out.splitlines():
            if line.startswith(_COMMIT_HEADER_SENTINEL):
                current_sha = line[len(_COMMIT_HEADER_SENTINEL):].strip()
                touched_by_sha.setdefault(current_sha, set())
            elif current_sha is not None and line.strip():
                touched_by_sha[current_sha].add(line.strip())
    return touched_by_sha


def _committed_paths_for_sids(
    repo_root: Path, sid_to_start: "Dict[str, datetime]"
) -> "Dict[str, Set[str]]":
    """The batched replacement for the former per-sha loop: for EVERY sid in
    `sid_to_start`, returns the set of paths touched by ITS OWN commits since
    ITS OWN session start — computed with exactly TWO `git` spawns TOTAL,
    independent of both peer count and commit count (AC1,
    docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-tail.md).

    Union-window-once idiom (`bulk_trailer_session_map`'s own docstring):
    rather than deriving one `--since=` window per sid, this walks the
    window since the EARLIEST sid's start ONCE, then attributes per sid
    afterward by in-memory set math — the windows overlap almost completely
    between peers, so re-deriving per sid was most of the original waste.

    Spawn 1 — `session_attribution.bulk_trailer_session_map(...,
    include_merges=True)`: one `git log --format=%H%x1f<trailer>` walk over
    `--since=<earliest>`, WITH merge commits included. `include_merges=True`
    is load-bearing here, not cosmetic — the default (`False`, `--no-merges`)
    is what C18 shipped and got reverted for (docs/plans/2026-08-07-n-plus-
    one-git-spawn-class-and-amplification-gate.md): a peer's merge commit's
    touched paths must still be attributed (AC2), and `--no-merges` would
    silently drop them, converting a peer's real contribution into a UNDER-
    exclusion of `resolve_known_concurrent_paths`'s own exclusion set — the
    opposite of that function's own stated correctness bar. Each returned
    trailer value is additionally UUID-shape-validated against
    `_SESSION_ID_UUID_RE` — `bulk_trailer_session_map` itself does not do
    this (it is a plain `git log` trailer scan; the shape guard is this call
    site's own responsibility, same fail-closed posture the former per-sha
    `archive_stamp._commit_session_id` call provided).

    Spawn 2 — one or more (never per-sha) `git log --no-walk -c --name-only
    <shas>` calls, CHUNKED at `_COMMITTED_PATHS_CHUNK` shas per call (see
    `_chunked_committed_paths`) over the UNION of every sha attributed to
    any requested sid, results unioned across chunks.

    A sid with no resolvable start time is simply absent from `sid_to_start`
    (callers filter it out before calling — see `resolve_known_concurrent_
    paths`'s own `sid_to_start` build loop); a sid present here but with
    zero attributed commits maps to an empty set, never omitted from the
    returned dict.

    FAIL-CLOSED, not fail-open (Review: code-reviewer P1/P2 — this
    deliberately reverses the former per-sha loop's fail-open posture): a
    `GitLogFailed` from the bulk trailer walk, or any chunk failure in the
    batched touched-paths walk, raises `PeerAttributionUnavailable` rather
    than degrading to an empty result. An empty peer-exclusion set is
    indistinguishable, to every caller, from "confirmed no peer owns
    anything here" — the exact over-optimistic read `resolve_known_
    concurrent_paths`'s own CORRECTNESS BAR forbids. See `session_
    attribution.GitLogFailed`'s own docstring for the precedent this
    mirrors one layer down.
    """
    result: "Dict[str, Set[str]]" = {sid: set() for sid in sid_to_start}
    if not sid_to_start:
        return result

    from coordinator_core.session_attribution import GitLogFailed, bulk_trailer_session_map

    earliest = min(sid_to_start.values()).astimezone(timezone.utc).isoformat()

    def _run(args: list, cwd: Optional[str]):
        # `args` always arrives as `["git", <subcommand>, ...]` — the
        # `GitRunner` shape `bulk_trailer_session_map` calls with. Re-spliced
        # through `-C <cwd>` (via `_spawn_git`) to match this module's other
        # subprocess call sites (`_run_git_ok`) rather than relying on `cwd=`
        # kwarg placement.
        return _spawn_git(cwd, args[1:])

    try:
        trailer_map = bulk_trailer_session_map(
            f"--since={earliest}", str(repo_root), _run, include_merges=True
        )
    except GitLogFailed as exc:
        raise PeerAttributionUnavailable(
            f"bulk_trailer_session_map failed while resolving peer-committed paths "
            f"for {len(sid_to_start)} sid(s): {exc}"
        ) from exc

    wanted_sids = set(sid_to_start)
    shas_by_sid: "Dict[str, list]" = {}
    for sha, trailer in trailer_map.items():
        if trailer not in wanted_sids:
            continue
        if not _SESSION_ID_UUID_RE.match(trailer):
            continue
        shas_by_sid.setdefault(trailer, []).append(sha)

    all_shas = [sha for shas in shas_by_sid.values() for sha in shas]
    if not all_shas:
        return result

    # `-c` (combined diff) is load-bearing for a MERGE sha: plain `git log
    # --name-only` prints NOTHING for a merge commit by default (verified
    # empirically — a bare `--name-only` walk silently drops every merge's
    # touched paths, which for THIS function would reproduce the exact
    # under-exclusion C18 was reverted for one layer down). `-c` reproduces
    # `git show`'s own default combined-diff behaviour (the former per-sha
    # call this replaces), listing paths that differ from every parent — the
    # same set `git show --name-only` reported for a single sha. A non-merge
    # sha's `-c` output is identical to its plain `--name-only` output.
    #
    # CHUNKED (never one call per sha, never a single unchunked argv) — see
    # `_chunked_committed_paths` and `_COMMITTED_PATHS_CHUNK`.
    touched_by_sha = _chunked_committed_paths(repo_root, all_shas)

    for sid, shas in shas_by_sid.items():
        paths: Set[str] = set()
        for sha in shas:
            paths.update(touched_by_sha.get(sha, set()))
        result[sid] = paths
    return result


def _enumerate_peer_session_ids(
    repo_root: Path, this_session_id: str
) -> "tuple[list[str], bool]":
    """Lists the OTHER session ids with a claim dir under the git common
    dir's `coordinator-sessions/` hub (`_session_core.sessions_dir`, the
    same hub `resolve_session_start_time` and `liveness.live_session_ids`
    read). Returns `(sids, enumeration_reliable)` — `enumeration_reliable`
    is `False` only when the hub could not be walked at all (an `OSError`
    mid-`iterdir`, e.g. permission failure or a TOCTOU dir removal), so the
    caller can tell "confirmed zero peer session dirs" (`True`, `[]` —
    session dirs are created eagerly at session start, so an empty walk
    that COMPLETED means no peer has ever existed) apart from "the walk
    itself broke and I have no idea" (`False`, whatever partial list was
    collected before the failure)."""
    try:
        base = _session_core.sessions_dir(str(repo_root))
    except Exception:
        return [], False
    if not base:
        # Not a git repo (or the hub genuinely cannot be resolved) — a
        # well-defined "no session-claim namespace exists", per
        # `sessions_dir`'s own docstring, not a resolver failure silently
        # read as "no peers". `known_concurrent_paths` is moot in this case
        # anyway: with no git repo there is no `git status --porcelain` to
        # apply it against.
        return [], True
    basep = Path(base)
    if not basep.is_dir():
        return [], True

    sids: list[str] = []
    try:
        for entry in basep.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name == this_session_id or name in _session_liveness._NON_SESSION_DIR_NAMES:
                continue
            sids.append(name)
    except OSError:
        return sids, False
    return sids, True


def _session_live_conservative(repo_root: Path, sid: str) -> bool:
    """`liveness.session_live`, with indeterminate liveness (any exception
    from the liveness read itself — a torn/unreadable meta.json, an OSError
    mid-stat) defaulting to LIVE, never to dead. This is a deliberate
    over-exclusion bias: a peer wrongly treated as live only costs an extra
    exclusion from THIS session's candidate stage-paths set (the EM can
    still hand-add it back after review); a peer wrongly treated as dead
    risks silently sweeping a still-running peer's in-progress work into
    this session's commit — the strictly worse failure per this producer's
    correctness bar."""
    try:
        return _session_liveness.session_live(sid, str(repo_root))
    except Exception:
        return True


def _scan_subagent_share_session_dirs(repo_root: Path, this_session_id: str) -> list[str]:
    """Filesystem-only fallback peer enumeration, independent of the
    `coordinator-sessions/` claim-dir hub entirely: every subdirectory of
    `state/subagent-share/` except `this_session_id`'s own. Used ONLY when
    `_enumerate_peer_session_ids` reports the claim-dir hub itself could not
    be walked (`enumeration_reliable=False`) — see `resolve_known_
    concurrent_paths`'s degrade-safely branch. Every directory found this
    way is treated as a candidate peer UNCONDITIONALLY (no liveness check —
    liveness itself routes through the same broken hub), which is the
    intentionally broader, over-inclusive answer this fallback exists to
    give rather than a confident empty set."""
    paths: list[str] = []
    share_root = repo_root / "state" / "subagent-share"
    try:
        if share_root.is_dir():
            for entry in share_root.iterdir():
                if entry.is_dir() and entry.name != this_session_id:
                    paths.append(entry.name)
    except OSError:
        pass
    return paths


def resolve_known_concurrent_paths(repo_root: Path, this_session_id: str) -> "frozenset[str]":
    """Step 3.0 case-(b)'s peer-exclusion set (`classify_session_authored_
    files`'s `known_concurrent_paths` parameter) — the producer that did not
    exist anywhere in this codebase until this function (see
    `jp-stage-paths-missing`'s own docstring, `judgments.py`, which named
    the gap explicitly). Built from primitives that already exist: the
    `coordinator-sessions/` claim-dir hub (`_session_core.sessions_dir`,
    the same hub `resolve_session_start_time`/`live_session_ids` already
    read), `liveness.session_live` for deciding which OTHER sessions are
    genuinely live right now, and `Session-Id` commit-trailer attribution
    (`archive_stamp._commit_session_id`, the same reader `commit_reality.
    py`'s positive-provenance path already relies on) for a peer's own
    recent commits.

    Covers, at minimum, per live peer session: (1) that peer's own
    `state/subagent-share/<sid>/` surface (both the collapsed-directory and
    expanded-per-file `git status --porcelain` shapes — see
    `_peer_subagent_share_paths`), and (2) every path touched by a commit
    carrying THAT peer's `Session-Id` trailer, landed since that peer's own
    session start (see `_committed_paths_for_sids`).

    CORRECTNESS BAR (this function's whole point — read before changing):
      - `this_session_id` is NEVER treated as a peer. If it is falsy, this
        function returns `frozenset()` immediately rather than attempting
        peer enumeration at all: without a resolved identity for "this
        session", there is no reliable way to exclude our OWN claim dir
        from whatever the enumeration finds, and self-exclusion (silently
        re-introducing the exact under-commit bug `jp-stage-paths-missing`
        exists to catch) is the failure this function must never cause.
        Returning nothing here is safe — it just means this pass gets no
        peer-exclusion help, not that anything gets wrongly excluded.
      - When genuinely ambiguous, this function is biased toward
        OVER-exclusion, never under-exclusion — see `_session_live_
        conservative`'s and `_scan_subagent_share_session_dirs`'s own
        docstrings for the two places that bias is applied. Naming a
        peer's file as "known concurrent" (and thus excluding it from OUR
        session-authored set) costs, at worst, an EM having to hand-add a
        path back after review. Failing to name it risks silently
        committing a live peer's in-progress work.
      - Degrades safely rather than empty-and-confident: when the
        `coordinator-sessions/` hub itself cannot be walked
        (`_enumerate_peer_session_ids` reports `enumeration_reliable=
        False` — a permission error, a TOCTOU dir removal, anything short
        of "the hub is empty"), this function does NOT fall through to
        `frozenset()` — an unreachable resolver silently reading as "no
        peers exist" is precisely the failure class this bar exists to
        rule out. It instead falls back to `_scan_subagent_share_session_
        dirs`, a filesystem-only enumeration of every OTHER `state/
        subagent-share/` directory, treated as a candidate peer
        UNCONDITIONALLY (no liveness gate, since liveness itself routes
        through the same broken hub) — deliberately broader and more
        conservative than the normal live-session-filtered path. A hub
        that resolves and walks cleanly but is simply EMPTY (no peer
        session dirs exist at all) is a different, legitimately-confident
        case and returns `frozenset()` unchanged — session dirs are
        created eagerly at session start, so a clean empty walk really
        does mean "no peers right now", not "I couldn't tell".
      - Fails LOUD, never empty-and-confident, when the commit-attribution
        half specifically cannot be resolved: `_committed_paths_for_sids`
        (via `_committed_paths_for_sids`'s union call below) raises
        `PeerAttributionUnavailable` on a git failure rather than degrading
        to an empty per-peer set, and this function does NOT catch it —
        the exception propagates to the caller uncaught. A peer-exclusion
        set this function could not actually compute must not be reported
        as "confirmed empty"; every current caller in `__init__.py` has no
        try/except around this call, so an unresolvable commit-attribution
        read fails the whole ceremony pass loudly rather than silently
        committing over a live peer's work.
    """
    if not this_session_id:
        return frozenset()

    peer_sids, enumeration_reliable = _enumerate_peer_session_ids(repo_root, this_session_id)

    result: set[str] = set()
    if not enumeration_reliable:
        for sid in _scan_subagent_share_session_dirs(repo_root, this_session_id):
            result.update(_peer_subagent_share_paths(repo_root, sid))
        return frozenset(result)

    live_sids = [sid for sid in peer_sids if _session_live_conservative(repo_root, sid)]
    for sid in live_sids:
        result.update(_peer_subagent_share_paths(repo_root, sid))

    # Union-window-once: resolve every live peer's own start time (no git
    # spawn scaling concern here — resolve_session_start_time is already
    # called once per peer, unchanged from before this fix), then hand the
    # WHOLE set to _committed_paths_for_sids in a single call rather than
    # calling it once per peer — see that function's own docstring for why
    # this is not the same cost.
    sid_to_start: "dict[str, Any]" = {}
    for sid in live_sids:
        start = _memo_lifecycle.resolve_session_start_time(repo_root, sid)
        if start is not None:
            sid_to_start[sid] = start
    for paths in _committed_paths_for_sids(repo_root, sid_to_start).values():
        result.update(paths)

    return frozenset(result)


# ---------------------------------------------------------------------------
# Step 3 mandatory-commit-shape — wsc-close tail-args + wsc-tail
# ---------------------------------------------------------------------------

_REVIEW_REQUIRED_FIELDS = ("sha_range", "reviewer", "scope", "verdict", "diff_loc")

#: The `decisions` keys this module's directive builders read — declared
#: once so a caller (`__init__.py`'s `preflight.decisions_template`
#: composition) can import and union this tuple rather than hand-copying
#: the key list. See AC3
#: (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
#: the arg-builder and the template read this SAME constant.
_KEY_DELETED_PATHS = "deleted_paths"
_KEY_KEPT_ENTRIES = "kept_entries"
_KEY_REVIEW = "review"
_KEY_SUBJECT = "subject"
_KEY_PROSE = "prose"
_KEY_STAGE_PATHS = "stage_paths"
_KEY_GOVERNING_PLAN_SLUG = "governing_plan_slug"

FREE_VALUE_KEYS: tuple[str, ...] = (
    _KEY_DELETED_PATHS,
    _KEY_KEPT_ENTRIES,
    _KEY_REVIEW,
    _KEY_SUBJECT,
    _KEY_PROSE,
    _KEY_STAGE_PATHS,
    _KEY_GOVERNING_PLAN_SLUG,
)


def _review_fields_present(review: Any) -> bool:
    """Whether a SINGLE `review` dict carries all five required fields,
    non-empty. Used two ways: (1) directly, on `decisions["review"]` when it
    is the pre-existing single-`dict` shape (`build_close_tail_args_
    directive`'s scalar `--review-*` branch, byte-identical to today); (2)
    per-ENTRY, when `decisions["review"]` is the additive `list[dict]`
    shape (partitioned-review fix, `workstream_complete.build_write_trail_
    directives`) — `build_close_tail_args_directive`'s list branch calls
    this once per list entry to decide which entries qualify for a
    `--review-slice` token, mirroring `build_write_trail_directives`'s own
    "an incomplete entry contributes nothing, silently" convention rather
    than forwarding a partial slice for `wsc-tail.py` to reject.

    Returns `False` for any non-`dict` input (a bare falsy `review`, or a
    list entry that is itself not a dict) — this function only ever judges
    ONE candidate dict at a time; a caller holding a `list` must iterate
    and call this per element, never pass the list itself."""
    if not isinstance(review, dict):
        return False
    return all(review.get(k) not in (None, "") for k in _REVIEW_REQUIRED_FIELDS)


#: Derived from `_REVIEW_REQUIRED_FIELDS` (never hand-duplicated) — the flat
#: `review_<field>` spellings a caller might plausibly place directly on
#: `decisions` instead of nesting them under `decisions["review"]`. A caller
#: who does this looks plausible at the callsite (2026-07-30 cross-repo memo:
#: `cross-repo/archive/2026-07-30-example-doctrine-repo-em-wsc-review-trail-passthrough-
#: and-memo-attribution.md` — a real session supplied `review_sha_range`,
#: `review_reviewer`, etc. as flat keys, `_review_fields_present({})` was
#: `False`, `--review-*` was never composed, and the tail reported
#: `review_trail.write:no-review-metadata` while still exiting 0) — nothing
#: in this module read those keys, so they were silently dropped. See
#: `_warn_unrecognized_review_keys` for the diagnostic this now emits.
_REVIEW_FLAT_ALIAS_KEYS: tuple[str, ...] = tuple(
    f"review_{field}" for field in (*_REVIEW_REQUIRED_FIELDS, "scope_kind")
)


def _warn_unrecognized_review_keys(decisions: dict[str, Any]) -> None:
    """This module's own Negative-spec already states the flag-shape
    principle one layer down: "a flag either parser rejects is worse than a
    missing one" (see `build_close_tail_args_directive`'s own docstring).
    This applies that SAME principle one layer up, at the `decisions` dict
    itself — a legitimately absent `review` dict must not fail loud (the
    review slice is genuinely optional), but a flat `review_*` key sitting
    unconsumed on `decisions` is not "absent", it is a caller's contract
    mismatch that this module can detect and was previously swallowing in
    silence. Diagnostic only, never raises — the ceremony still proceeds
    (the review dict, if genuinely supplied, is unaffected), it just stops
    doing so quietly. Printed to stderr, never stdout: this module's own
    `brief`/`apply` CLI seams (`coordinator_core.workstream_complete.
    __init__`) serialize their JSON result to stdout, and `wsc-close
    tail-args`' own stdout is a parsed argv/token channel spliced into
    `d-run-wsc-tail` — polluting either would be worse than the silent drop
    this closes.
    """
    present = sorted(k for k in _REVIEW_FLAT_ALIAS_KEYS if k in decisions)
    if not present:
        return
    print(
        "workstream-complete: decisions key(s) "
        f"{present!r} are not part of the contract and are being IGNORED — "
        f"review metadata must be supplied as a nested decisions[{_KEY_REVIEW!r}] "
        f"object with keys {list(_REVIEW_REQUIRED_FIELDS)!r} (optional "
        "'scope_kind'), not flat top-level keys.",
        file=sys.stderr,
    )


def build_close_tail_args_directive(decisions: dict[str, Any]) -> dict[str, Any]:
    """Assembles the OPTIONAL `--deleted-paths` / `--kept-entries` /
    `--review-*` flags via `wsc-close tail-args` — this mirrors the
    bash conditional-array logic the pre-conversion SKILL inlined, and its
    stdout (one argv token per line) is spliced into `d-run-wsc-tail`'s
    own invocation at apply time, per `depends_on`. The two optional-flag
    groups are each all-or-nothing at the wsc-tail layer (a partial
    `--review-*` supply is rejected there, not here) — this builder only
    omits a group whose backing value is absent, never partially supplies
    one.

    Negative-spec — every flag emitted here must be accepted by BOTH
    `wsc-close tail-args` (which parses this argv) and `wsc-tail.py` (which
    parses the spliced stdout). A flag either parser rejects is worse than a
    missing one: `tail-args` exits 2, `d-run-wsc-tail` splices an EMPTY argv,
    and the whole `--review-*` group is silently dropped — the tail then
    commits with no review metadata while the ceremony still reports success.
    `--review-scope-kind` is spelled to match `wsc-tail.py`'s own parser, NOT
    shortened to the `review` dict's `scope_kind` key.

    `reviewer_evidence` is forwarded on BOTH branches — as
    `--review-reviewer-evidence` on the scalar branch, as a slice key on the
    list branch. It is optional here and outside the all-or-nothing five, but
    `review_trail_write._verify_reviewer_evidence` requires it for every
    reviewer except `wsc-auto-adjudication`; that gate is advisory today and
    designed to become enforcing, so a value accepted in `decisions["review"]`
    and dropped in this argv silently defeats the correlation the gate exists
    to establish. Spec backlink: cross-repo/inbox/2026-08-13-example-doctrine-repo-em-wsc-
    tail-ipc-timeout-and-reviewer-evidence-drop.md § 2.

    Applies that same "silent drop is worse than a loud rejection" principle
    one layer up, at the `decisions` dict itself: an unrecognized flat
    `review_*` key on `decisions` (a caller's plausible-looking but wrong
    spelling of the nested `review` object) now gets a one-line stderr
    diagnostic via `_warn_unrecognized_review_keys` rather than a silent
    drop — a genuinely absent `review` dict stays quiet, per that helper's
    own docstring.

    `decisions["review"]` may be a single `dict` (unchanged: the scalar
    `--review-*` flags above, byte-identical to before the partitioned-
    review fix) OR a `list[dict]` (additive — same shape `build_write_
    trail_directives` reads for the standalone directive path): one
    `--review-slice <json>` token per list entry that passes `_review_
    fields_present`, in original list order, via `wsc-tail.py`'s
    repeatable `--review-slice` flag (`docs/plans/...` — see that CLI's
    own module docstring for the JSON shape and per-slice foreign-session
    isolation this now carries through the commit-tail path). An entry
    failing `_review_fields_present` contributes NO token — silently
    dropped, mirroring `build_write_trail_directives`'s "an incomplete
    entry contributes nothing" convention rather than forwarding a partial
    slice for `wsc-tail.py` to reject. The scalar five-flag form and the
    repeatable `--review-slice` form are mutually exclusive at every layer
    downstream (`wsc-close.py tail-args`, `wsc-tail.py`) — `decisions["review"]`
    being a `dict` XOR a `list` at this call site is what keeps that
    exclusivity automatic; this function never emits both.
    """
    _warn_unrecognized_review_keys(decisions)
    args: list[str] = ["tail-args"]
    if decisions.get(_KEY_DELETED_PATHS):
        args += ["--deleted-paths", *[str(p) for p in decisions[_KEY_DELETED_PATHS]]]
    if decisions.get(_KEY_KEPT_ENTRIES):
        args += ["--kept-entries", *[str(p) for p in decisions[_KEY_KEPT_ENTRIES]]]
    review = decisions.get(_KEY_REVIEW) or {}
    if isinstance(review, list) and _COMMIT_TAIL_ACCEPTS_REVIEW_SLICES:
        for entry in review:
            if not _review_fields_present(entry):
                continue
            payload = {k: str(entry[k]) for k in _REVIEW_REQUIRED_FIELDS}
            if entry.get("scope_kind"):
                payload["scope_kind"] = str(entry["scope_kind"])
            if entry.get("reviewer_evidence"):
                payload["reviewer_evidence"] = str(entry["reviewer_evidence"])
            args += ["--review-slice", json.dumps(payload, sort_keys=True)]
    elif _review_fields_present(review):
        args += [
            "--review-sha-range", str(review["sha_range"]),
            "--review-reviewer", str(review["reviewer"]),
            "--review-scope", str(review["scope"]),
            "--review-verdict", str(review["verdict"]),
            "--review-diff-loc", str(review["diff_loc"]),
        ]
        if review.get("scope_kind"):
            args += ["--review-scope-kind", str(review["scope_kind"])]
        if review.get("reviewer_evidence"):
            args += ["--review-reviewer-evidence", str(review["reviewer_evidence"])]
    return _directive("d-close-tail-args", "wsc-close", args)


def build_wsc_tail_directive(sid: str, decisions: dict[str, Any]) -> dict[str, Any]:
    """The single trampoline call that stages `WSC_PATHS`, composes the
    commit message, runs the Step 2.67 deletion-claim + Step 3.0 dirty-tree
    gates, commits, and chains the async post-commit push — everything the
    pre-conversion SKILL walked as a multi-call keystone block, now ONE
    `wsc-tail.py` invocation (`d-run-wsc-tail`). `subject`/`prose` are
    `commit-message-authoring`'s ALREADY-DECIDED output (C2f); `stage_paths`
    is `accumulate_session_paths`'s result. Depends on `d-close-tail-args`,
    and the apply half splices that call's stdout into this one's argv via
    the trailing `"{d-close-tail-args.argv}"` arg token below —
    `apply._resolve_arg_tokens`'s `.argv` field expands that single
    standalone arg into every non-blank, stripped line of `d-close-tail-
    args`' captured stdout (the `--deleted-paths`/`--kept-entries`/
    `--review-*` flags `wsc-close tail-args` composed), appended in order
    after this directive's own build-time-known args
    (sid/subject/prose/stage-paths/governing-plan-slug), same shape the
    pre-existing inline `d-tail` directive in `__init__.py` already used
    plus the one token. `depends_on="d-close-tail-args"` is kept for
    ordering; the token is what actually threads the value.
    """
    args: list[str] = ["--sid", sid]
    if decisions.get(_KEY_SUBJECT):
        args += ["--subject", str(decisions[_KEY_SUBJECT])]
    if decisions.get(_KEY_PROSE):
        args += ["--prose", str(decisions[_KEY_PROSE])]
    stage_paths = decisions.get(_KEY_STAGE_PATHS)
    if stage_paths:
        args += ["--stage-paths", *[str(p) for p in stage_paths]]
    if decisions.get(_KEY_GOVERNING_PLAN_SLUG):
        args += ["--governing-plan-slug", str(decisions[_KEY_GOVERNING_PLAN_SLUG])]
    args.append("{d-close-tail-args.argv}")
    return _directive("d-run-wsc-tail", "wsc-tail", args, depends_on="d-close-tail-args")


class WscTailOutcome(NamedTuple):
    status: str
    halted: bool
    next_move: str


#: The 5-way exit ladder `d-run-wsc-tail` (`wsc-tail.py`) resolves to at
#: RUN TIME — not something this read-only assembler can pre-resolve, but
#: the branch tree itself is fully known and deterministic per exit code
#: (SKILL.md § Exit ladder), so it is data here rather than re-narrated
#: prose at every call site. Key `"timeout"` is not a `wsc-tail.py` process
#: exit code (it never returns one on a client-side timeout — the process
#: is still running server-side); it is the caller's OWN `cc_invoke`
#: 10s-cap TimeoutExpired condition, included here because the SKILL
#: documents it as the ladder's 5th rung and a caller dispatching this
#: directive needs the same lookup for both cases.
WSC_TAIL_EXIT_LADDER: dict[Any, WscTailOutcome] = {
    0: WscTailOutcome(
        status="success",
        halted=False,
        next_move="Proceed to d-release-plan-claim / d-emit-cadence.",
    ),
    2: WscTailOutcome(
        status="commit-landed-tail-item-needs-attention",
        halted=False,
        next_move=(
            "The commit already landed (e.g. an origin-stub close soft-failed). Read "
            "the diagnostics/tail_results printed to stderr and address the named item "
            "directly. Do NOT re-run d-run-wsc-tail — the tree is already clean."
        ),
    ),
    1: WscTailOutcome(
        status="hard-failure",
        halted=True,
        next_move=(
            "The op refused (commit_failed, or a bare error string) or the trampoline "
            "itself failed. No commit landed. Diagnose before re-running."
        ),
    ),
    3: WscTailOutcome(
        status="transport-failure",
        halted=True,
        next_move="Surface to the PM — this is an install/seam problem, not a ceremony-content one.",
    ),
    "timeout": WscTailOutcome(
        status="client-timeout-reconcile",
        halted=False,
        next_move=(
            "Do NOT blind-retry. Check `git log -1` / `git status` and the op's tail "
            "artifacts (completion entry, swept memos) for evidence the commit already "
            "landed server-side before deciding anything failed or re-running."
        ),
    ),
}


def describe_wsc_tail_outcome(exit_code: Any) -> WscTailOutcome:
    """Looks up `WSC_TAIL_EXIT_LADDER` for a `wsc-tail.py` process exit
    code (`0`/`1`/`2`/`3`) or the literal string `"timeout"` for a
    client-side `cc_invoke` timeout. An unrecognized code degrades to the
    same disposition as `wsc-tail.py`'s own `main()` fallback for an
    out-of-band `exit_code` (treated as a hard failure, not silently
    ignored) — see `wsc-tail.py`'s own `if exit_code != 0` branch.
    """
    outcome = WSC_TAIL_EXIT_LADDER.get(exit_code)
    if outcome is not None:
        return outcome
    return WscTailOutcome(
        status="unrecognized-exit-code",
        halted=True,
        next_move=f"wsc-tail.py returned unrecognized exit_code={exit_code!r} — diagnose before re-running.",
    )


# ---------------------------------------------------------------------------
# Step 3 push-confirmation branch tree — d-verify-push-landed (read-only)
# ---------------------------------------------------------------------------


class PushLandedGate(NamedTuple):
    pushed: Optional[bool]
    deferred: bool
    unpushed_shas: tuple[str, ...]
    summary_line: str
    declined: bool = False


def compute_push_landed_gate(
    repo_root: Path,
    branch: str,
    push_status: Optional[str] = None,
) -> PushLandedGate:
    """Step 3's push-confirmation branch tree, read-only throughout.

    `push_status` is `wsc-tail.py`'s own reported `push_status` field
    (`"deferred"` under the 2026-07-23 async-push contract, `"declined"`
    when branch policy deliberately withheld the push, `None`/absent under
    `COORDINATOR_WSC_SYNC_PUSH=1` synchronous mode). Two short-circuits,
    each non-failing but NOT interchangeable — one waits, one never will:

    - `push_status == "deferred"`: an unpushed commit in the first seconds
      after `d-run-wsc-tail` returns is EXPECTED, not a failure — the
      detached push child may still be in flight — so this short-circuits
      to a non-failing gate (`deferred=True`) without querying
      `origin/<branch>` at all; callers that need certainty RE-CHECK after
      a short interval or consult `.git/push-failures.log` (this module
      does not read that log — a log-tail read belongs to whichever surface
      renders Step 4's "Pushed to remote" line, not this gate).
    - `push_status == "declined"`: branch policy decided, in this pass,
      that no push would be attempted at all — nothing is in flight and
      nothing ever will be, so re-checking later is pointless. This
      short-circuits to a non-failing gate (`declined=True`, `deferred=
      False`) without querying `origin/<branch>` either, but a caller must
      NOT apply the `deferred` arm's "check again shortly" guidance here —
      that is precisely the collapse this separate field exists to
      prevent.

    Never issues `git push` itself — per the SKILL, only the detached push
    (or sync mode) owns that; this function only reads `git log`/`git
    branch -r --contains`.
    """
    if push_status == "deferred":
        return PushLandedGate(
            pushed=None,
            deferred=True,
            unpushed_shas=(),
            summary_line="Pushed to remote: deferred (async post-commit push in flight)",
        )

    if push_status == "declined":
        return PushLandedGate(
            pushed=None,
            deferred=False,
            unpushed_shas=(),
            summary_line="Pushed to remote: declined (branch policy — push intentionally not attempted)",
            declined=True,
        )

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"origin/{branch}..HEAD", "--format=%H"],
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return PushLandedGate(
            pushed=None,
            deferred=False,
            unpushed_shas=(),
            summary_line="Pushed to remote: unknown (could not query origin)",
        )

    if proc.returncode != 0:
        return PushLandedGate(
            pushed=None,
            deferred=False,
            unpushed_shas=(),
            summary_line="Pushed to remote: unknown (could not query origin)",
        )

    unpushed = tuple(sha for sha in proc.stdout.strip().splitlines() if sha)
    if not unpushed:
        return PushLandedGate(
            pushed=True,
            deferred=False,
            unpushed_shas=(),
            summary_line=f"Pushed to remote: yes — {branch}",
        )
    return PushLandedGate(
        pushed=False,
        deferred=False,
        unpushed_shas=unpushed,
        summary_line=f"Pushed to remote: no — {len(unpushed)} unpushed commit(s) on {branch}",
    )


# ---------------------------------------------------------------------------
# Step 3.5 — d-release-plan-claim
# ---------------------------------------------------------------------------
# (Session archival — formerly `d-archive-session-claim` here — moved to
# session END, not workstream close; see `__init__.py`'s call-site comment
# at the Step 3/3.5/3.6 assembly point. `wsc-close.py archive-session` and
# `coordinator_core/session/scope.py`'s `archive()` remain live for that
# SessionEnd-hook caller; only this module's builder was removed.)


def build_release_plan_claim_directive(governing_plan_slug: Optional[str]) -> Optional[dict[str, Any]]:
    """Releases the governing-plan execution-lock claim ONLY when a
    governing plan was in scope (the Step 2.4 governing-plan predicate),
    and only AFTER the Step 3 commit has landed — `depends_on="d-run-wsc-
    tail"` encodes that ordering as a directive-id dependency, which
    `ceremony_common.apply_halt._directive_gate_open` deliberately does
    NOT gate on (see that function's docstring); producer readiness is
    instead enforced here via the trailing `{d-run-wsc-tail.landed}`
    ordering-only arg token (`apply._resolve_arg_tokens`), which resolves
    to the empty string — behaviorally identical to the omitted optional
    `baton_repo_root` positional `session-claim-cli release-artifact`
    already accepts (`claims.release_artifact`'s `if baton_repo_root:`
    truthiness check treats `""` the same as absent) — when `d-run-wsc-
    tail` landed this pass, and fails the directive loud into
    `report["failed"]` otherwise, so a blocked-at-judgment or failed
    commit tail can never let the plan claim release out from under a
    commit that never happened. `release_artifact`'s frontmatter-revert
    precondition is vacuous for the plan claim class (full atomic-mkdir
    lock, no frontmatter execution-stamp; SKILL.md's own ORDERING-CONTRACT
    annotation) and `session-claim-cli release-artifact` is itself
    holder-identity-checked and safe to call unconditionally once the
    predicate is true. Returns `None` (no directive) when no governing
    plan was identified — a plan-less session acquired no claim and
    releases nothing."""
    if not governing_plan_slug:
        return None
    return _directive(
        "d-release-plan-claim",
        "session-claim-cli",
        ["release-artifact", "plan", str(governing_plan_slug), "{d-run-wsc-tail.landed}"],
        depends_on="d-run-wsc-tail",
    )


# ---------------------------------------------------------------------------
# Step 3.6 — d-emit-cadence
# ---------------------------------------------------------------------------


def build_emit_cadence_directive() -> dict[str, Any]:
    """Fires the per-repo emission cadence trigger, best-effort per AC5 —
    a no-seam machine / transport hiccup or gate-off must NOT wedge the
    ceremony. The fail-open is a real mechanism, not a mirrored bash
    idiom: `build_ceremony_close_tail`'s emit-cadence entry carries
    `best_effort: True` (docs/plans/2026-08-08-a-best-effort-directive-
    cannot-fail-a-ce.md, AC8), and the ceremony runner's `_execute_
    directives` routes a non-zero exit from a `best_effort` directive into
    `report["degraded"]` instead of `report["failed"]`, so it can never
    move the ceremony's exit code — the retired pre-conversion bash `||
    echo ... skipped` had no successor until that plan landed.

    Depends on `d-run-wsc-tail` per the SKILL's own ordering ("this fires after Step 3's commit/push,
    not earlier") — previously depended on `d-archive-session-claim`, but
    that directive was removed from this assembly (2026-07-28: session
    archival moved to session END, not workstream close; see `__init__.py`'s
    call-site comment). Re-pointed onto `d-run-wsc-tail`, the directive
    `d-archive-session-claim` itself used to depend on, so the ordering
    guarantee (fires after the Step 3 commit lands) is preserved unchanged.
    Shares the family-scoped `build_ceremony_close_tail` factor (§ module
    docstring's Design note) rather than hand-deriving the directive shape a
    third time — takes only its `emit-cadence` element, since this ceremony
    has no post-command-hook step to pair it with.

    `depends_on="d-run-wsc-tail"` is a directive-id dependency, which
    `ceremony_common.apply_halt._directive_gate_open` deliberately does
    NOT gate on (see that function's docstring — gating it there would
    route a failed producer into an ungatable `HALTED_AT_JUDGMENT`, never
    a halt any disposition can clear). `emit-cadence.py`'s own `main()`
    takes no parameters and ignores its argv entirely, so appending the
    ordering-only `{d-run-wsc-tail.landed}` arg token
    (`apply._resolve_arg_tokens`) is inert to the CLI's own behavior while
    still giving `apply()` a producer-readiness check to enforce: the
    token resolves to the empty string when `d-run-wsc-tail` landed this
    pass, and fails `d-emit-cadence` loud into `report["failed"]`
    otherwise — closing the gap where cadence emission for a commit that
    never happened (the tail blocked at a judgment point, or failed)
    would otherwise dispatch unconditionally."""
    hook_and_cadence = build_ceremony_close_tail(
        post_command_hook_id="d-unused-no-ceremony-hook-step",
        emit_cadence_id="d-emit-cadence",
        ceremony_name="workstream-complete",
    )
    cadence = hook_and_cadence[1]
    if cadence["best_effort"] is not True:
        # Review: code-reviewer — a bare `assert` here is stripped under
        # `python -O`/PYTHONOPTIMIZE, degrading this fail-loud check to a
        # silent revert to failed-not-degraded semantics; an explicit raise
        # survives an optimized run (precedent: claude_klabauter_root.py's shim-spec
        # check).
        raise RuntimeError(
            "build_ceremony_close_tail's emit-cadence entry must carry best_effort: True "
            "(docs/plans/2026-08-08-a-best-effort-directive-cannot-fail-a-ce.md, AC8) — "
            "re-derive it there, do not re-set it here."
        )
    cadence["depends_on"] = "d-run-wsc-tail"
    cadence["args"] = ["{d-run-wsc-tail.landed}"]
    return cadence


# ---------------------------------------------------------------------------
# Step 4 — d-render-final-summary (read-only fan-in, no CLI)
# ---------------------------------------------------------------------------

_SUMMARY_HEAD = """## Session Complete

**Work done:** {work_done}
**Pushed:** {pushed}"""

#: Exception lines, in print order. Each renders only when its caller-supplied
#: value is non-empty — an all-clean ceremony emits the two head fields alone.
#: `auto_memory_drain` is the one field with no other record on disk (the
#: memory store carries no git history), so its caller is responsible for
#: passing the disposition list whenever the drain gate ever printed residue.
_EXCEPTION_LABELS: tuple[tuple[str, str], ...] = (
    ("completeness_checklist", "Completeness checklist"),
    ("auto_memory_drain", "Auto-memory drain"),
    ("deferral_harvest", "Deferral harvest"),
    ("post_summary_reconcile", "Post-summary reconcile"),
    ("flag_to_pm", "Flag to PM"),
)


def render_final_summary(
    work_done: str,
    pushed: str,
    completeness_checklist: str = "",
    auto_memory_drain: str = "",
    deferral_harvest: str = "",
    post_summary_reconcile: str = "",
    flag_to_pm: str = "",
) -> str:
    """Step 4's report-by-exception summary: two always-printed fields plus
    any exception line whose value is non-empty. Fan-in aggregation only —
    per the census, "no new content decided here" — `work_done` itself is
    `session-work-summary`'s ALREADY-DECIDED sentence (C2f), not authored by
    this function. Pure string formatting, no CLI, no `directives[]` entry:
    mirrors `directives_session_hygiene.py`'s
    `compute_completeness_checklist_gate` precedent for a step whose only
    work is rendering a fixed template from caller-supplied facts.

    Negative-spec: the former fixed 8-field block also printed
    `Lessons captured`, `Work archived`, `Docs updated` and
    `Orientation refreshed`. They were dropped, not accidentally lost — each
    was a count or file list of work the ceremony's own commit already
    records, carrying no PM decision, and a block of all-clean status lines
    spends the EM->PM word budget that `hooks/em_report_altitude.py` then
    measures as a verbosity violation. Do not restore them; do not convert
    an empty exception value into an explicit "none"/"clean" line, which
    rebuilds the same fixed block one default at a time."""
    lines = [_SUMMARY_HEAD.format(work_done=work_done, pushed=pushed)]
    values = {
        "completeness_checklist": completeness_checklist,
        "auto_memory_drain": auto_memory_drain,
        "deferral_harvest": deferral_harvest,
        "post_summary_reconcile": post_summary_reconcile,
        "flag_to_pm": flag_to_pm,
    }
    for key, label in _EXCEPTION_LABELS:
        value = values[key]
        if value:
            lines.append(f"**{label}:** {value}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - this module has no standalone CLI
    print(
        "directives_commit_tail.py is a pure builder module, not a CLI — "
        "import it from coordinator_core.workstream_complete instead.",
        file=sys.stderr,
    )
    sys.exit(2)
