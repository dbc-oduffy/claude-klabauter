"""
coordinator_core.workstream_complete.directives_commit_tail — the
peer-attribution, commit-tail, push-outstanding, push-verification and
publish-lag-advisory builders for the `workstream-complete-assemble`
computed-skill engine.

Purpose: this module's live surface is the CLOSE/COMMIT tail itself, not a
`directives[]`-builder family — the close/commit tail proper is
`run_close_commit` / `run_close_commit_and_release_claims` (the two
governing-plan and per-path claim releases wired into the latter) /
`run_push_outstanding_tail`. Everything else here (`resolve_known_
concurrent_paths` and its `_committed_paths_for_sids`/`chunked_show_
numstat_blocks` support, `compute_push_landed_gate`, `compute_publish_lag_
advisory`) is read-only fan-in these callers consume: peer-exclusion pathspec
resolution, push-landed confirmation, and the DR-335 publish-lag advisory.
Mirrors `directives_session_hygiene.py`'s directives-vs-gates split where it
still applies: a step whose "work" is reading disk/git state or folding
already-computed one-liners into a fixed template is a plain read-only
function this module exposes directly, per that module's own "compute a
fact, don't invent a directive" disposition.

Callers MUST use `run_close_commit_and_release_claims`, never bare
`run_close_commit` — the latter releases NEITHER claim mechanism (see its
own docstring): `run_close_commit_and_release_claims` wraps it and
releases both the per-path commit-claim (`session/scope.py ::
release_committed_claims`, hard constraint 4) and the governing-plan
artifact claim (`ops/ceremony/tail_ops.py :: cs_release_artifact`, AC5)
unconditionally, on both the success and failure exit of the wrapped
commit call.

This module is one of seven siblings (directives_lessons_plan.py,
directives_completion.py, directives_memo_lifecycle.py,
directives_review.py, directives_session_hygiene.py, judgments.py) built
under the multi-module-assembler convention `docs/plans/2026-07-26-
workstream-complete-computed-frontage.md` (D-4) sets for this tree:
`__init__.py` is retained as the assembly + CLI seam ONLY, and every
submodule exposes pure, `__init__`-independent functions.

Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md,
chunk C2e. Source census: state/plan-sidecars/2026-07-26-workstream-complete-
computed-frontage.census-steps.md, Step 3.0/3/3.5/3.6/4 rows.

Kill/rebuild provenance (load-bearing — preserve; do not restate as if
current):

`ceremony.wsc_tail` was killed 2026-08-23 (`state/kill-ledger.md` for the
original removal record) and its OPERATOR requirement — closing a session
without hand-landing the commit — was rebuilt, not restored, by DR-358
(`docs/decisions/DR-358-the-close-ceremony-shape-after-the-kill.md`),
2026-08-25:

- `d-close-tail-args` (formerly `build_close_tail_args_directive`,
  fronting `coordinator/bin/archive-session-scope.py tail-args`) and `d-run-wsc-tail`
  (formerly `build_wsc_tail_directive`, fronting the now-deleted
  `coordinator/bin/wsc-tail.py` trampoline for the killed op) were removed
  outright by the kill and are NOT present in this file. DR-358 rules
  `run_close_commit` the rebuilt shape for `d-run-wsc-tail`'s requirement:
  an in-process call directly against `commit_pipeline.run_commit_pipeline`
  at `push_mode=PUSH_MODE_NEVER` (hard constraints 5/6), with no new CLI or
  `directives[]` layer above it — `d-close-tail-args`'s former argument
  computation merges into this one call per DR-358's own ruling, rather
  than being rebuilt as a separate step. C3 (docs/plans/2026-08-29-the-
  push-subsystem-leaves-and-then-the-pipeline-can-go.md) repointed the
  in-process call itself off the killed `commit_pipeline.
  run_commit_pipeline` onto `coordinator_core.git.commit.commit_paths` --
  DR-358's own ruling about the SHAPE (in-process, no new CLI/directive
  layer) is unaffected by which commit primitive sits underneath it.
- `d-release-plan-claim` (formerly `build_release_plan_claim_directive`,
  fronting `session-claim-cli release-artifact`) remains gone as a
  standalone directive — DR-358 rules it discharged by an inline call to
  the existing native port `ops/ceremony/tail_ops.py :: cs_release_
  artifact`, wired into `run_close_commit`'s route by `run_close_commit_
  and_release_claims` (C5), not rebuilt as a directive either.
- `d-write-trail`'s former review-shape/review-enum assemble-time
  validators (`validate_review_shape` and the rest of that cluster) lived
  in this module only because their sole callers — the now-removed
  `build_wsc_tail_directive`/`build_close_tail_args_directive` — lived here
  too. Deleted as dead code once those builders' removal left them with
  zero callers; `d-write-trail` itself lives in `directives_review.py`
  (`build_write_trail_directives`), not here, and was never this module's
  own step.
- `accumulate_session_paths` (formerly `d-accumulate-session-paths`) was
  spliced only into the two removed builders' `--stage-paths` argument;
  deleted as dead code once they were gone and no replacement caller
  appeared.

Negative-spec:
    - Does NOT decide commit subject/prose text, the pinboard note, which
      concurrent-EM disposition to pick for a case-(c) unattributable file,
      or the Step-4 "Work done"/flag-severity classification prose —
      `commit-message-authoring`, `unattributable-file-disposition`,
      `concurrent-peer-attribution`, `session-work-summary`, and
      `flag-severity-classification` are C2f's (`judgments.py`) judgment
      points. This module only turns already-decided values into a
      `commit_paths` call or a rendered summary string.
    - Does NOT implement the dirty-tree classifier itself
      (`d-classify-dirty-tree`) — per the census, that step is already
      engine-owned inside the commit-gate stack (`commit_gates`) and has
      nothing separate for this module to compute or name.
    - Does NOT restate the "never `git add -A`" invariant anywhere in this
      module. That invariant is already enforced inside the op
      (`coordinator_core.bash_guards.block_blanket_git_add`) and DR-090's
      own worked example names restating an already-enforced invariant at
      the call site as the antipattern the conversion exists to remove —
      this module passes an explicit `stage_paths` pathspec and nothing
      broader, full stop.
    - `run_close_commit` does NOT compose `subject`/`prose` text itself
      (`commit-message-authoring` stays `judgments.py`'s C2f call), does NOT
      decide `stage_paths` (that is `resolve_known_concurrent_paths`'s job
      above, folded by the caller), and does NOT release the governing-plan
      claim (`cs_release_artifact` wiring is `run_close_commit_and_release_
      claims`'s route, not this function's body) — it only turns
      already-decided values into one `commit_paths` call; there is no push
      leg here for a mode to govern at all.
    - DOES retry, narrowly: `_chunked_committed_paths`' per-chunk git spawn
      (`_run_git_ok_retrying`, `_GIT_RETRY_ATTEMPTS`) is bounded lock-
      contention absorption for a git SPAWN that has not yet succeeded, not
      a general-purpose retry wrapper — scoped to this one call site's known
      failure mode. Fail-closed is preserved either way: exhausting the
      retry budget still raises `PeerAttributionUnavailable`, never
      degrades to an empty/partial result. Do not "simplify" this retry
      away, and do not widen it into a general-purpose retry wrapper reused
      elsewhere in this module.
    - `run_push_outstanding_tail` never issues `git push` itself, and
      neither does `compute_push_landed_gate` (read-only: `git log`/`git
      branch -r --contains` only) — the SAME split `run_close_commit`'s own
      docstring names ("A caller wanting a push does so as its own
      separate, later step").
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Union

from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _session_liveness
from coordinator_core.session import scope as session_scope
from coordinator_core.warm import skew as _skew
from coordinator_core.warm.engine_root import current_engine_clone as _current_engine_clone
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_memo_lifecycle as _memo_lifecycle

_NO_CONSOLE = no_console_creationflags()


class CommitTailOutcome(NamedTuple):
    """`run_close_commit`'s own return shape (C3, repointed off the killed
    `commit_pipeline.PipelineResult`) -- carries only the fields this
    module's callers (`apply.py :: _run_close_commit_tail`, this module's own
    tests) actually read off a `PipelineResult` today. `pushed`/`push_status`
    are always the "not attempted" values: this route never had a push leg
    (`commit_paths` has no push concept at all), so there is nothing here to
    disambiguate."""

    committed_sha: Optional[str]
    pushed: Optional[bool]
    push_status: str
    commit_failed: bool
    integrity_breach: bool
    sha_unverified: bool
    diagnostics: List[str]


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
            # `--no-optional-locks`: this funnel is read-only by contract (see
            # docstring), so nothing routed through it has cause to take the
            # index lock or rewrite a 4.9MB / 35k-entry index on a tree ~50
            # sessions write concurrently. Same shape `archive_stamp.py` uses.
            ["git", "-C", str(repo_root), "--no-optional-locks", *args],
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


#: Numstat row shape (`added\tdeleted\tpath`), path-group only — mirrors
#: `coordinator_core.workstream_complete`'s own
#: `_REVIEW_SCALE_NUMSTAT_ROW_RE` (not imported: that module imports THIS
#: one, so importing back would be circular; see `_SESSION_ID_UUID_RE`'s
#: own "Ownership guard"-style precedent above for why a third small regex
#: here beats a cross-direction import cycle). Only the path group is
#: consumed on this side — the peer-attribution consumer never needed
#: added/deleted, and still doesn't now that the spawn is shared with the
#: LOC consumer.
_NUMSTAT_ROW_RE = re.compile(r"^(?:-|\d+)\t(?:-|\d+)\t(.+)$")


def chunked_show_numstat_blocks(
    repo_root: Path, all_shas: "list[str]"
) -> "Optional[Dict[str, str]]":
    """THE single chunked `git show --raw --numstat --format=<sentinel>%H
    <shas>` spawn this module's peer-paths consumer, `__init__.py`'s
    session-LOC consumer, and (C6, docs/plans/2026-08-26-the-gate-paths-
    six-spawns-collapse-to-four.md § C6) `__init__.py`'s
    `_added_paths_from_numstat_blocks` all now route through (C1: spawn #2's
    peer-paths walk and spawn #6's session-LOC walk collapse to one `show
    --numstat`, since `--numstat` is a strict superset of `--name-only`'s
    rows; C6: `--raw` composed alongside `--numstat` in the SAME invocation
    — one argv gains a flag, no new spawn — mirroring `ops.session_commits.
    resolve_session_commits`'s own `args.extend(["--raw", "--numstat"])`
    prior art). Every existing consumer's row parser
    (`_NUMSTAT_ROW_RE`/`_REVIEW_SCALE_NUMSTAT_ROW_RE`, both anchored on
    `^(-|\\d+)\\t(-|\\d+)\\t`) skips a `--raw` row rather than mis-parsing
    it — a `--raw` row begins with `:`, which cannot match either regex.
    Chunked at
    `_COMMITTED_PATHS_CHUNK` shas per call, never one call per sha (see
    `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`) — a
    union of two callers' sha sets is LARGER than either alone, so this is
    MORE likely to need chunking, not less.

    Returns `{sha: <raw numstat block text, one row per line, no header>}`
    for every sha whose header line was seen in the output — a sha with a
    header but zero rows still maps to `""`, never omitted. Returns `None`
    (never a partial dict) on the FIRST chunk that still fails after
    `_run_git_ok_retrying`'s bounded retry budget is exhausted — this
    function does not itself decide fail-open vs fail-closed; each of its
    two callers (`_chunked_committed_paths` here,
    `_measure_session_review_scale_inputs` in `__init__.py`) applies its
    OWN existing contract to a `None` result (raise
    `PeerAttributionUnavailable`, or return the all-`None` four-tuple,
    respectively) exactly as it did before this spawn was shared. Issues NO
    spawn at all for an empty `all_shas` (returns `{}`) rather than one
    over an empty argv.

    `git show` on a merge sha defaults to combined-diff numstat rows
    (verified empirically, restated at `_committed_paths_for_sids`'s own
    `-c` paragraph) — identical to the former `git log --no-walk -c
    --name-only`'s merge handling, so this format swap changes WHERE the
    bytes come from, not what either consumer's merge-commit coverage is.
    """
    if not all_shas:
        return {}
    blocks: "Dict[str, str]" = {}
    for i in range(0, len(all_shas), _COMMITTED_PATHS_CHUNK):
        chunk = all_shas[i : i + _COMMITTED_PATHS_CHUNK]
        out = _run_git_ok_retrying(
            repo_root,
            ["show", "--raw", "--numstat", f"--format={_COMMIT_HEADER_SENTINEL}%H", *chunk],
        )
        if out is None:
            return None
        current_sha: Optional[str] = None
        current_lines: "list[str]" = []
        for line in out.splitlines():
            if line.startswith(_COMMIT_HEADER_SENTINEL):
                if current_sha is not None:
                    blocks[current_sha] = "\n".join(current_lines)
                current_sha = line[len(_COMMIT_HEADER_SENTINEL):].strip()
                current_lines = []
            elif current_sha is not None:
                current_lines.append(line)
        if current_sha is not None:
            blocks[current_sha] = "\n".join(current_lines)
    return blocks


def _committed_paths_from_blocks(blocks: "Dict[str, str]") -> "Dict[str, Set[str]]":
    """Projects `chunked_show_numstat_blocks`' `{sha: numstat_text}` down to
    the peer-attribution consumer's own `{sha: touched_paths}` shape — the
    superset-to-subset partition C1 names: `--numstat` carries
    `added<TAB>deleted<TAB>path` per row, and this consumer only ever
    needed the path column (the same rows `--name-only` alone used to hand
    it)."""
    result: "Dict[str, Set[str]]" = {}
    for sha, text in blocks.items():
        paths: "Set[str]" = set()
        for line in text.splitlines():
            match = _NUMSTAT_ROW_RE.match(line)
            if match and match.group(1):
                paths.add(match.group(1))
        result[sha] = paths
    return result


def _chunked_committed_paths(
    repo_root: Path, all_shas: "list[str]"
) -> "Dict[str, Set[str]]":
    """Spawn 2 of `_committed_paths_for_sids`, now routed through the
    shared `chunked_show_numstat_blocks` (see that function's own
    docstring for the spawn/chunking contract this delegates to, and for
    why the format changed from `log --no-walk -c --name-only` to `show
    --numstat`). Returns {sha: touched_paths}, the union across every
    chunk — same return shape as before this consolidation (AC2: frozen).

    Raises `PeerAttributionUnavailable` when `chunked_show_numstat_blocks`
    reports `None` (any chunk still failing after `_run_git_ok_retrying`'s
    bounded retry budget) — matches `coverage.py`'s `_bulk_trailer_
    lookup`'s "never a partial map" posture; a partial union here would
    silently under-report a peer's touched paths for whichever shas fell in
    the failed chunk, which is the exact fail-open outcome this function
    exists to close. The retry layer (see `_GIT_RETRY_ATTEMPTS`'s own
    docstring) absorbs routine transient lock contention from a concurrent
    peer's git operation before this fail-closed raise ever fires — it does
    not weaken the raise itself.
    """
    blocks = chunked_show_numstat_blocks(repo_root, all_shas)
    if blocks is None:
        raise PeerAttributionUnavailable(
            f"git show --numstat failed for one of {len(all_shas)} peer-committed "
            "sha(s) after the bounded retry budget was exhausted — refusing to "
            "return a partial/empty union."
        )
    return _committed_paths_from_blocks(blocks)


def _committed_paths_for_sids(
    repo_root: Path,
    sid_to_start: "Dict[str, datetime]",
    *,
    extra_shas: "Sequence[str]" = (),
    extra_blocks_out: "Optional[Dict[str, str]]" = None,
    trailer_map_out: "Optional[Dict[str, str]]" = None,
    this_session_id: "Optional[str]" = None,
    own_session_numstat_out: "Optional[Dict[str, str]]" = None,
    window_start_out: "Optional[List[str]]" = None,
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

    `trailer_map_out` (C2, docs/plans/2026-08-26-the-gate-paths-six-spawns-
    collapse-to-four.md § C2): optional, mirrors `extra_blocks_out`'s
    out-param idiom. When not `None`, populated in place with the RAW
    `bulk_trailer_session_map` result -- every {sha: trailer-value} the
    bulk walk saw, unfiltered by `sid_to_start`. Cleared (never left stale)
    on every return path, including the ones that never reach the trailer
    walk at all (`not sid_to_start`) -- absence there means "no map was
    built", not "confirmed no matches".

    `this_session_id`/`own_session_numstat_out` (C4, docs/plans/2026-08-26-
    the-gate-paths-six-spawns-collapse-to-four.md § C4): this function
    already scans the bulk trailer map it just walked for `sid_to_start`'s
    peers -- when `this_session_id` is also supplied, that SAME scan is
    reused to find THIS session's own shas too (never a second trailer
    walk), and those shas are folded into the SAME `show --numstat` union
    this function is about to spawn for peer attribution. When not `None`,
    `own_session_numstat_out` is cleared then populated with `{sha:
    numstat_text}` for exactly those own-session shas, oldest-first
    insertion order (mirrors `workstream_complete._session_owned_shas_
    from_map`'s own oldest-first contract) -- lets a caller read this
    session's own shas back out via `own_session_numstat_out.keys()`
    ordering, or simply match them against a `precomputed_session_shas`
    list resolved the same way. Cleared (never left stale) on every return
    path, including the ones that never reach the trailer walk at all (`not
    sid_to_start`) -- absence there means "no walk happened", not
    "confirmed no own-session commits"; a caller must fall back to its own
    spawning path exactly as it already does for `trailer_map_out`.
    """
    result: "Dict[str, Set[str]]" = {sid: set() for sid in sid_to_start}
    extra_shas = list(extra_shas)
    if not sid_to_start:
        if trailer_map_out is not None:
            # No peer window to walk, so the bulk trailer map below is never
            # built -- absent, not empty-and-confident (C2, docs/plans/2026-
            # 08-26-the-gate-paths-six-spawns-collapse-to-four.md § C2): a
            # caller reading this dict must fall back to its own spawn
            # rather than reading "no entries" as "this session committed
            # nothing".
            trailer_map_out.clear()
        if own_session_numstat_out is not None:
            own_session_numstat_out.clear()
        if extra_shas and extra_blocks_out is not None:
            # No peer to attribute, but a session-LOC caller still needs its
            # own shas' numstat blocks -- one spawn for its sake alone (still
            # never per-sha, still through the shared chunked helper).
            blocks = chunked_show_numstat_blocks(repo_root, extra_shas)
            if blocks is None:
                raise PeerAttributionUnavailable(
                    f"git show --numstat failed for one of {len(extra_shas)} "
                    "session-owned sha(s) after the bounded retry budget was "
                    "exhausted while resolving peer-committed paths."
                )
            extra_blocks_out.update({sha: blocks[sha] for sha in extra_shas if sha in blocks})
        elif extra_blocks_out is not None:
            extra_blocks_out.clear()
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

    if window_start_out is not None:
        # The `--since=` bound the map above was built with, handed back so a
        # caller can tell whether that map could have seen ITS OWN whole
        # history. `trailer_map` answers "which sid touched what, inside this
        # window"; it cannot answer "did this window cover session X", and a
        # caller needing completeness (review scope) rather than attribution
        # has no other way to find out. A list rather than a return value, to
        # leave this function's signature and every existing caller untouched
        # -- the same out-param shape `trailer_map_out` already uses.
        window_start_out.append(earliest)

    if trailer_map_out is not None:
        # RAW, unfiltered {sha: trailer-value} -- every sid the bulk walk
        # saw in `--since=<earliest live peer start>`'s window, not just
        # `sid_to_start`'s peers. This is what lets a caller holding this
        # session's OWN id (never a member of `sid_to_start` -- see this
        # function's own peer-only contract) read ITS OWN shas back out
        # without a second trailer-walk spawn (C2, docs/plans/2026-08-26-
        # the-gate-paths-six-spawns-collapse-to-four.md § C2; mirrors the
        # `extra_numstat_out` out-param idiom C1 landed alongside it). Not
        # UUID-shape-filtered here (unlike `shas_by_sid` below) -- a caller
        # reading this map for its own known-good sid does not need that
        # guard re-applied.
        trailer_map_out.clear()
        trailer_map_out.update(trailer_map)

    wanted_sids = set(sid_to_start)
    shas_by_sid: "Dict[str, list]" = {}
    for sha, trailer in trailer_map.items():
        if trailer not in wanted_sids:
            continue
        if not _SESSION_ID_UUID_RE.match(trailer):
            continue
        shas_by_sid.setdefault(trailer, []).append(sha)

    all_shas = [sha for shas in shas_by_sid.values() for sha in shas]
    peer_sha_set = set(all_shas)

    # C4 fold: `own_shas` is the SAME kind of scan `shas_by_sid` above just
    # did, over the SAME already-in-hand `trailer_map`, for one more sid
    # (`this_session_id`) that is never itself a member of `sid_to_start`
    # (peers only -- see this function's own peer-only contract). Reversed
    # to oldest-first to mirror `_session_owned_shas_from_map`'s own
    # contract, though ordering here is cosmetic: `own_session_numstat_out`
    # below is built by iterating `own_shas` directly, not by reading back
    # `blocks`' own (union-order) key order.
    own_shas: "list[str]" = []
    if this_session_id:
        own_shas = [sha for sha, trailer in trailer_map.items() if trailer == this_session_id]
        own_shas.reverse()

    extra_only = [sha for sha in extra_shas if sha not in peer_sha_set]
    own_only = [
        sha for sha in own_shas if sha not in peer_sha_set and sha not in extra_shas
    ]
    union_shas = all_shas + extra_only + own_only
    if not union_shas:
        if extra_blocks_out is not None:
            extra_blocks_out.clear()
        if own_session_numstat_out is not None:
            own_session_numstat_out.clear()
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
    # `chunked_show_numstat_blocks` and `_COMMITTED_PATHS_CHUNK`. `union_shas`
    # (peer shas plus any caller-supplied `extra_shas`, deduped) is spawned
    # ONCE here — C1's collapse of spawn #2 (this walk) and spawn #6
    # (`__init__.py`'s session-LOC walk, when it supplies `extra_shas`) into
    # one `show --numstat` over their union, partitioned back out below.
    blocks = chunked_show_numstat_blocks(repo_root, union_shas)
    if blocks is None:
        raise PeerAttributionUnavailable(
            f"git show --numstat failed for one of {len(union_shas)} sha(s) "
            "(peer-committed plus any session-owned extras) after the bounded "
            "retry budget was exhausted while resolving peer-committed paths."
        )
    if extra_blocks_out is not None:
        extra_blocks_out.update({sha: blocks[sha] for sha in extra_shas if sha in blocks})
    if own_session_numstat_out is not None:
        own_session_numstat_out.clear()
        own_session_numstat_out.update({sha: blocks[sha] for sha in own_shas if sha in blocks})
    touched_by_sha = _committed_paths_from_blocks(
        {sha: text for sha, text in blocks.items() if sha in peer_sha_set}
    )

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


def resolve_known_concurrent_paths(
    repo_root: Path,
    this_session_id: str,
    *,
    extra_shas: "Sequence[str]" = (),
    extra_numstat_out: "Optional[Dict[str, str]]" = None,
    trailer_map_out: "Optional[Dict[str, str]]" = None,
    own_session_numstat_out: "Optional[Dict[str, str]]" = None,
    window_start_out: "Optional[List[str]]" = None,
) -> "frozenset[str]":
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

    `extra_shas`/`extra_numstat_out` (C1, spawn-collapse dispatch brief
    `.../C1.md`): an optional caller-supplied sha list (never peer shas —
    the session's OWN owned shas, per `__init__.py`'s `_measure_session_
    review_scale_inputs`) folded into the SAME `show --numstat` union this
    function already spawns for peer attribution, with `extra_numstat_out`
    (when not `None`) populated in place with `{sha: numstat_text}` for
    just those extra shas. Optional and additive: an empty/absent
    `extra_shas` reproduces this function's pre-C1 behaviour exactly, one
    spawn for peer attribution alone.

    `trailer_map_out` (C2, docs/plans/2026-08-26-the-gate-paths-six-spawns-
    collapse-to-four.md § C2): optional out-param, same idiom as
    `extra_numstat_out` -- see `_committed_paths_for_sids`'s own docstring
    for the shape it is filled with. Threads straight through to that
    function; cleared (never left stale) on every early-return path this
    function itself takes before ever reaching `_committed_paths_for_sids`
    (a falsy `this_session_id`, or the degrade-safely branch). Lets
    `workstream_complete.__init__._session_owned_shas` read THIS session's
    own attributed shas back out of the SAME bulk trailer walk this
    function already spawns for peer attribution, instead of re-walking the
    DAG a second time for exactly the same window.

    `own_session_numstat_out` (C4, docs/plans/2026-08-26-the-gate-paths-
    six-spawns-collapse-to-four.md § C4): optional out-param, threaded
    straight through to `_committed_paths_for_sids` (see that function's
    own docstring for the shape). Folds THIS session's own shas -- found by
    the SAME `trailer_map_out` scan, at zero extra spawns -- into the SAME
    `show --numstat` union already spawned for peer attribution, closing
    the circular dependency C2 could not: C2's reorder means a caller no
    longer knows its own shas BEFORE this call runs, so it can no longer
    hand them in as `extra_shas`/`extra_numstat_out` (C1's shape) the way
    a caller with advance knowledge still can. Cleared (never left stale)
    on every early-return path this function itself takes before ever
    reaching `_committed_paths_for_sids` (a falsy `this_session_id`, or the
    degrade-safely branch) -- absence there means "no walk happened", not
    "confirmed no own-session commits"; a caller must fall back to its own
    spawning path exactly as it already does for `trailer_map_out`.
    """
    extra_shas = list(extra_shas)

    def _resolve_extras_standalone() -> None:
        # Reached only on a code path that returns before ever calling
        # `_committed_paths_for_sids` (falsy `this_session_id`, or the
        # degrade-safely branch below) -- a caller's `extra_shas` (C1's
        # session-LOC union half) still needs its own numstat blocks even
        # when this function has no peer-attribution work of its own to do.
        if extra_numstat_out is None:
            return
        if not extra_shas:
            extra_numstat_out.clear()
            return
        blocks = chunked_show_numstat_blocks(repo_root, extra_shas)
        if blocks is None:
            raise PeerAttributionUnavailable(
                f"git show --numstat failed for one of {len(extra_shas)} "
                "session-owned sha(s) after the bounded retry budget was "
                "exhausted while resolving peer-committed paths."
            )
        extra_numstat_out.update({sha: blocks[sha] for sha in extra_shas if sha in blocks})

    if not this_session_id:
        if trailer_map_out is not None:
            trailer_map_out.clear()
        if own_session_numstat_out is not None:
            own_session_numstat_out.clear()
        _resolve_extras_standalone()
        return frozenset()

    peer_sids, enumeration_reliable = _enumerate_peer_session_ids(repo_root, this_session_id)

    result: set[str] = set()
    if not enumeration_reliable:
        for sid in _scan_subagent_share_session_dirs(repo_root, this_session_id):
            result.update(_peer_subagent_share_paths(repo_root, sid))
        if trailer_map_out is not None:
            trailer_map_out.clear()
        if own_session_numstat_out is not None:
            own_session_numstat_out.clear()
        _resolve_extras_standalone()
        return frozenset(result)

    live_sids = [sid for sid in peer_sids if _session_live_conservative(repo_root, sid)]
    for sid in live_sids:
        result.update(_peer_subagent_share_paths(repo_root, sid))

    # Union-window-once: resolve every live peer's own start time (no git
    # spawn scaling concern here — resolve_session_start_time is already
    # called once per peer, unchanged from before this fix), then hand the
    # WHOLE set to _committed_paths_for_sids in a single call rather than
    # calling it once per peer — see that function's own docstring for why
    # this is not the same cost. `extra_shas`/`extra_numstat_out` (C1) ride
    # along on the SAME call so a caller needing both this exclusion set
    # AND its own session-owned shas' numstat blocks (`__init__.py`'s
    # `_measure_session_review_scale_inputs`) gets them from the union of
    # ONE `show --numstat` spawn rather than two.
    sid_to_start: "dict[str, Any]" = {}
    for sid in live_sids:
        start = _memo_lifecycle.resolve_session_start_time(repo_root, sid)
        if start is not None:
            sid_to_start[sid] = start
    for paths in _committed_paths_for_sids(
        repo_root,
        sid_to_start,
        extra_shas=extra_shas,
        extra_blocks_out=extra_numstat_out,
        trailer_map_out=trailer_map_out,
        this_session_id=this_session_id,
        own_session_numstat_out=own_session_numstat_out,
        window_start_out=window_start_out,
    ).values():
        result.update(paths)

    return frozenset(result)


# ---------------------------------------------------------------------------
# Step 3 decisions-key surface
# ---------------------------------------------------------------------------

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
_KEY_DELIVERABLE_ID = "deliverable_id"
_KEY_ATTRIBUTED_SESSION_ID = "attributed_session_id"

#: This chunk's own decisions-key vocabulary (no prior key named this).
_KEY_HANDOFF_DISPOSITIONS = "handoff_dispositions"

FREE_VALUE_KEYS: tuple[str, ...] = (
    _KEY_DELETED_PATHS,
    _KEY_KEPT_ENTRIES,
    _KEY_REVIEW,
    _KEY_SUBJECT,
    _KEY_PROSE,
    _KEY_STAGE_PATHS,
    _KEY_GOVERNING_PLAN_SLUG,
    # Review: coordinator:code-reviewer (Finding 1, 2026-08-30) -- widening
    # the undeclared-key guard past `directives_*.py` to also scan `apply.py`
    # surfaced these two as pre-existing, genuinely undeclared reads in
    # `_resolve_close_commit_kwargs` (apply.py). Both are caller-supplied
    # `decisions[...]` facts this module's own arg-builder already consumes;
    # declaring them here is the same fix the `handoff_dispositions` incident
    # above already made for this exact blind spot.
    _KEY_DELIVERABLE_ID,
    _KEY_ATTRIBUTED_SESSION_ID,
    # `handoff_dispositions` is caller-supplied by design (see its own note
    # below: the delivery sha is "resolved by the caller ... never derived
    # here"), which makes declaring it here the whole difference between a
    # key a caller can find and one it cannot. Landed without this entry,
    # `resolve_ship_stamp_candidates` short-circuited on an always-absent key
    # and the ship-stamp was inert in production while reporting a clean
    # "ran, found nothing" -- the green-stamp-on-an-empty-set failure its own
    # plan's Anti-scope names. This module's comment above already states the
    # rule that catches it: "a key read here but absent from this tuple is a
    # key no caller can discover from the template."
    _KEY_HANDOFF_DISPOSITIONS,
)


# ---------------------------------------------------------------------------
# Step 3/3.5 rebuild — d-run-wsc-tail's commit, at the DR-358-ruled shape
# ---------------------------------------------------------------------------


def run_close_commit(
    worktree_root: "Union[Path, str]",
    *,
    session_id: str,
    subject: str,
    prose: str = "",
    deleted_paths: "Sequence[str]" = (),
    kept_entries: "Sequence[str]" = (),
    trailers: str = "",
    stage_paths: "Sequence[str]" = (),
    caller_paths: "Optional[Set[str]]" = None,
    on_committed: "Optional[Any]" = None,
    deliverable_id: Optional[str] = None,
    attributed_session_id: Optional[str] = None,
) -> Any:
    """The rebuilt `d-run-wsc-tail` — restores the operator's ability to
    close a session without hand-landing the commit, at the shape DR-358
    rules and `state/audits/2026-08-25-close-ceremony-floor-probe.md`
    measures (AC3's shape budget), called directly IN-PROCESS, no new CLI or
    `directives[]` layer above it — `d-close-tail-args`'s former argument
    computation merges into this one call, per DR-358's own ruling).

    C3 (docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-
    pipeline-can-go.md): repointed off the killed `commit_pipeline.
    run_commit_pipeline` onto `coordinator_core.git.commit.commit_paths`,
    the sanctioned zero-spawn commit shape. There is no `push_mode`
    parameter to preserve here — this route never owned a push leg (hard
    constraints 5/6 bound the old call to `PUSH_MODE_NEVER` unconditionally,
    so no push was ever attempted from here); `commit_paths` has no push
    concept at all, so the constraint is now structural rather than an
    argument value. A caller wanting a push does so as its own separate,
    later step (`run_push_outstanding_tail`); this function never pushes.

    Every keyword argument here is a caller-supplied, already-decided value:
    `subject`/`prose`/`deleted_paths`/`kept_entries`/`trailers` compose the
    message via `coordinator_core.ops.ceremony.commit_message.
    compose_message` (the same Step-2.67 message shape `run_commit_pipeline`
    used), `stage_paths` (`accumulate_session_paths`/
    `resolve_known_concurrent_paths` above) becomes `commit_paths`'s pathspec.
    This function decides none of the message content itself — see this
    module's own Negative-spec. AC8 (the `Closes:` composed-by-hand message
    text surviving verbatim) is a property of NOT mutating `prose` anywhere
    between the caller and `compose_message` — this function's whole job is
    to not be the place that breaks that.

    `caller_paths`/`on_committed`/`attributed_session_id` are accepted for
    call-shape compatibility with every existing caller but are no longer
    threaded anywhere: `commit_paths` has no tolerant pre-stage step to scope
    (`caller_paths` gated `explicit_stage`'s swept-path tolerance, which does
    not exist on this path — a `stage_paths` entry absent from disk and not
    also listed in `deleted_paths` is a genuine caller error now, surfaced as
    `CommitRefused` rather than silently skipped), no `on_committed` hook
    fires mid-call, and `commit_paths` carries no `Session-Id:` trailer
    concept to attribute.

    Governing-plan claim release (`d-release-plan-claim`'s DR-358 ruling,
    `ops/ceremony/tail_ops.py :: cs_release_artifact`) is NOT called here,
    nor is hard constraint 4's per-path `release_committed_claims` — both
    are wired by `run_close_commit_and_release_claims` (this module, C5),
    the wrapper that calls this function and releases both claims at both
    its success and failure exits; this function's own return is a
    `CommitTailOutcome`, this module's own `PipelineResult`-shaped stand-in
    (`committed_sha`/`pushed`/`push_status`/`commit_failed`/
    `integrity_breach`/`sha_unverified`/`diagnostics`). A caller closing a
    session should call `run_close_commit_and_release_claims`, not this
    function directly.
    """
    from coordinator_core.git.commit import CommitRefused, FilterUnsupported, commit_paths
    from coordinator_core.git.commit import hash_worktree_blobs_via_spawn
    from coordinator_core.ops.ceremony.commit_message import compose_message
    from coordinator_core.ops.ceremony.push import PUSH_STATUS_NOT_ATTEMPTED
    from functools import partial

    root = Path(worktree_root)
    message = compose_message(
        subject=subject,
        prose=prose,
        deleted_paths=deleted_paths,
        kept_entries=kept_entries,
        trailers=trailers,
    )
    # `commit_paths` cannot read a path it cannot find; a `stage_paths` entry
    # already absent on disk and not also declared via `deleted_paths` is
    # what `explicit_stage`'s swept-path tolerance used to paper over --
    # dropped here rather than silently included, since a missing path in
    # `paths` raises `CommitRefused` (an OSError on read), not a no-op skip.
    deleted_set = set(deleted_paths)
    present_paths = [
        p for p in stage_paths if p in deleted_set or (root / p).exists()
    ]
    if not present_paths and not deleted_paths:
        # Mirrors `run_commit_pipeline`'s own empty-`commit_paths` short-
        # circuit (step 2 of its docstring sequence): nothing to stage is a
        # benign no-op, not a refusal -- `commit_paths` itself raises
        # `CommitRefused` on an empty pathspec (it never defaults to "commit
        # the whole index"), so that case is intercepted here before the call
        # rather than reported as a failure.
        return CommitTailOutcome(
            committed_sha=None,
            pushed=None,
            push_status=PUSH_STATUS_NOT_ATTEMPTED,
            commit_failed=False,
            integrity_breach=False,
            sha_unverified=False,
            diagnostics=[],
        )
    try:
        outcome = commit_paths(
            root,
            present_paths,
            message,
            deleted_paths=list(deleted_paths),
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=root),
        )
    except (CommitRefused, FilterUnsupported) as exc:
        return CommitTailOutcome(
            committed_sha=None,
            pushed=None,
            push_status=PUSH_STATUS_NOT_ATTEMPTED,
            commit_failed=True,
            integrity_breach=False,
            sha_unverified=False,
            diagnostics=[str(exc)],
        )
    if on_committed is not None:
        on_committed(outcome.sha)
    return CommitTailOutcome(
        committed_sha=outcome.sha,
        pushed=None,
        push_status=PUSH_STATUS_NOT_ATTEMPTED,
        commit_failed=False,
        integrity_breach=False,
        sha_unverified=False,
        diagnostics=[],
    )


def _release_committed_path_claims(
    worktree_root: "Union[Path, str]", session_id: str, stage_paths: "Sequence[str]"
) -> None:
    """Hard constraint 4's per-route wiring of `session/scope.py ::
    release_committed_claims` — the PATH-claim mechanism (`touched.txt` `R`
    events), never wired automatically by `commit_paths`/`git_native.py`
    (84 hand-wired call sites repo-wide, zero there — hard constraint 4's own
    count). Mirrors `post_commit_tail.py ::
    _commit_and_push_origin_stub_close`'s own call shape exactly (same
    `session_scope.release_committed_claims(sid, paths, cwd=...)` call,
    wrapped the same best-effort way): a release failure must never surface
    as this route's own failure — the commit (or its absence) is the durable
    outcome; a retained stale path-claim is the safe residue, same fail-safe
    direction that call site's own comment documents. Skips entirely when
    `session_id` is falsy — releasing under an unknown sid would be a guess
    at authorship, the one thing this mechanism refuses to do (same posture
    as the precedent it copies)."""
    if not session_id:
        return
    try:
        session_scope.release_committed_claims(
            session_id, list(stage_paths), cwd=str(worktree_root)
        )
    except Exception:
        pass


def _release_governing_plan_claim(
    worktree_root: "Union[Path, str]", governing_plan_slug: Optional[str]
) -> None:
    """AC5 / DR-358's `d-release-plan-claim` ruling: releases the
    governing-plan ARTIFACT claim (a different mechanism entirely from
    `_release_committed_path_claims` above — see this function's own module
    docstring addendum) via the existing native port `ops/ceremony/
    tail_ops.py :: cs_release_artifact(common_dir, "plan",
    governing_plan_slug)` — the same call shape `session/claims.py`'s own
    lifecycle-comment names for the (now-deleted) `wsc_tail.py` Step 6 call
    site this rebuilds. No new directive is added (DR-358 rules this
    in-process call sufficient — a `directives[]` entry would add dispatch/
    argv overhead for a call that is itself in-process and best-effort).

    Skips entirely when `governing_plan_slug` is falsy (no governing plan
    was ever resolved for this session — nothing to release). Otherwise
    best-effort throughout: `cs_release_artifact` itself never raises (a
    missing claim dir, a not-the-holder result, and any `OSError` are all
    clean no-ops per its own docstring), but resolving `common_dir` from
    `worktree_root` can (a non-repo path, a permission error) — caught here
    so a claim-release failure never surfaces as this route's own failure,
    the same fail-safe direction `_release_committed_path_claims` takes for
    the sibling mechanism."""
    if not governing_plan_slug:
        return
    try:
        from coordinator_core.lifecycle import git_common_dir
        from coordinator_core.ops.ceremony.tail_ops import cs_release_artifact

        common_dir = git_common_dir(Path(worktree_root))
        cs_release_artifact(Path(common_dir), "plan", governing_plan_slug)
    except Exception:
        pass


def run_close_commit_and_release_claims(
    worktree_root: "Union[Path, str]",
    *,
    session_id: str,
    subject: str,
    prose: str = "",
    deleted_paths: "Sequence[str]" = (),
    kept_entries: "Sequence[str]" = (),
    trailers: str = "",
    stage_paths: "Sequence[str]" = (),
    caller_paths: "Optional[Set[str]]" = None,
    on_committed: "Optional[Any]" = None,
    deliverable_id: Optional[str] = None,
    attributed_session_id: Optional[str] = None,
    governing_plan_slug: Optional[str] = None,
) -> Any:
    """C5's route: `run_close_commit` wrapped with BOTH claim-release
    mechanisms hard constraint 4 and AC5 each require — two SEPARATE claim
    classes, not one mechanism doing double duty (staff-eng review, finding
    1; see this module's docstring for the full account):

      (a) hard constraint 4 — the per-PATH commit-claim mechanism
          (`_release_committed_path_claims`, `session/scope.py ::
          release_committed_claims`), released for `stage_paths` (the same
          pathspec this call just asked `run_close_commit` to stage/commit).
      (b) AC5 — the governing-plan ARTIFACT claim
          (`_release_governing_plan_claim`, `ops/ceremony/tail_ops.py ::
          cs_release_artifact`), released for `governing_plan_slug` when one
          is supplied.

    Every caller of this rebuilt close route should call THIS function, not
    `run_close_commit` directly — a caller reaching `run_close_commit` on
    its own bypasses both releases silently (exactly the invisible-loss
    failure mode `docs/plans/2026-08-11-claim-release-and-the-gate-that-
    cannot-clear.md` names, and precisely why nothing else in this codebase
    catches the omission — see hard constraint 4's own text).

    Both releases fire in a `finally`, per DR-358's failure-path ruling
    (`d-release-plan-claim` section): "release fires unconditionally when
    the ceremony's close sequence completes its close-time work, whether the
    commit step itself succeeded or failed" — a claim held by a session that
    failed to commit is exactly as abandoned as one held by a session that
    committed cleanly. This holds whether `run_close_commit` returns a
    failed `PipelineResult` OR raises outright: the `finally` runs either
    way, and a raise still propagates to this function's own caller
    unchanged (this function adds no new exception class and swallows
    nothing from `run_close_commit` itself — only the two release calls
    themselves are individually best-effort, per their own docstrings).

    Returns `run_close_commit`'s own `PipelineResult`, unmodified — this
    function decides nothing about commit shape, message text, or push mode;
    see `run_close_commit`'s own docstring and this module's Negative-spec
    for what it does not do.
    """
    try:
        return run_close_commit(
            worktree_root,
            session_id=session_id,
            subject=subject,
            prose=prose,
            deleted_paths=deleted_paths,
            kept_entries=kept_entries,
            trailers=trailers,
            stage_paths=stage_paths,
            caller_paths=caller_paths,
            on_committed=on_committed,
            deliverable_id=deliverable_id,
            attributed_session_id=attributed_session_id,
        )
    finally:
        _release_committed_path_claims(worktree_root, session_id, stage_paths)
        _release_governing_plan_claim(worktree_root, governing_plan_slug)


# ---------------------------------------------------------------------------
# C2 (docs/plans/2026-08-30-the-close-ships-the-baton-it-closed.md) —
# ship-stamp the session's own delivered batons off the claim ledger, folded
# into the SAME close commit `run_close_commit_and_release_claims` already
# makes. NO CORPUS SCAN: candidates come from the claim ledger
# (`session.claims.list_claims_by_session_checked`, the same "handoff-claims"
# subdir `claim_state._sessions_dir(common_dir)` names), never a
# glob/rglob over `state/handoffs/`/`archive/handoffs/` — see the plan's
# Anti-scope ("Do not search for the batons") and C3's own budget-guard AC.
#
# `decisions["handoff_dispositions"]` (this chunk's own vocabulary — no
# prior key existed for it) is `{basename: {"disposition": str, "shipped_in":
# Optional[str]}}`. Only `disposition == "shipped"` with a non-empty
# `shipped_in` (the already-landed DELIVERY commit sha, resolved by the
# caller from its own commits/session facts — never derived here, never the
# close's own commit sha) qualifies for the stamp; `closed`/`abandoned`/
# `continued` are terminal-without-delivery and are excluded the same way a
# claim absent from this map is excluded (the positive-membership rule: a
# held claim qualifies only because THIS close's own decisions say so, never
# because it is merely held).
# ---------------------------------------------------------------------------

#: `_KEY_HANDOFF_DISPOSITIONS` is defined beside FREE_VALUE_KEYS above --
#: it has to be, since that tuple names it, and a key declared after the
#: tuple that reads it is the NameError this module already paid for once.
_HANDOFF_DISPOSITION_SHIPPED = "shipped"


class ShipStampOutcome(NamedTuple):
    """`apply_ship_stamps`'s own return shape — the "ran and found nothing"
    vs "never ran" distinction the plan's Anti-scope demands a reader for
    (the retired design's `empty_consumed_set` flag had none). `attempted`
    is the candidate count BEFORE any per-candidate failure, so `attempted >
    0` with `stamped_paths == ()` reads as "ran, everything failed/skipped",
    distinct from `attempted == 0` ("nothing held qualified")."""

    stamped_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    attempted: int
    diagnostics: tuple[str, ...]


#: The "ran, found nothing" sentinel value -- a module-level constant so the
#: two callers needing it (`apply.py`'s own no-candidates branch, any future
#: one) cannot drift on field values by hand-constructing this shape
#: separately. Review: coordinator:code-reviewer (Finding 3, 2026-08-30).
EMPTY_SHIP_STAMP_OUTCOME = ShipStampOutcome(
    stamped_paths=(), skipped_paths=(), attempted=0, diagnostics=()
)


def _held_handoff_basenames(worktree_root: "Union[Path, str]", session_id: str) -> "list[str]":
    """This session's own held handoff-claim basenames — reuses
    `session.claims.list_claims_by_session_checked` (the SAME claim-record
    store `claim_state._sessions_dir` names) rather than re-deriving a
    second ledger reader, filtered to the `"handoff-claims"` class only (that
    accessor also reports `plan`/`memo` claims, neither relevant here)."""
    from coordinator_core.session.claims import list_claims_by_session_checked

    matches, _errors = list_claims_by_session_checked(session_id, cwd=str(worktree_root))
    return [basename for class_, basename in matches if class_ == "handoff-claims"]


def resolve_ship_stamp_candidates(
    worktree_root: "Union[Path, str]",
    session_id: Optional[str],
    decisions: "dict[str, Any]",
) -> "list[tuple[str, str]]":
    """Returns `[(handoff_relpath, delivery_sha), ...]` for every held claim
    this close's own `decisions["handoff_dispositions"]` records as
    closed-with-delivery in THIS session (see this section's own module
    comment for the positive-membership rule). Restricted to handoffs still
    ACTIVE on disk (`state/handoffs/<basename>` present) — a basename no
    longer there is already under `archive/handoffs/` (consumed, per the PM's
    folder-fact ruling) and is not this close's to stamp; `handoff.stamp`
    itself refuses a path under `archive/handoffs/` too (bonus enforcement,
    named in the chunk body, not relied on alone here).

    Returns `[]` (never raises) for a falsy `session_id` or an empty/absent
    `decisions["handoff_dispositions"]` — both are "nothing to do", not an
    error. Review: coordinator:code-reviewer (Finding 4, 2026-08-30) — these
    two early-outs collapse to the same `[]`, so a caller cannot distinguish
    "no session_id was ever resolved" from "session_id present but nothing to
    ship" from this return value alone. Currently latent, not live: this
    module's only caller (`apply.py :: _run_close_commit_tail`, via
    `_resolve_close_commit_kwargs`) already refuses to reach this call at all
    when `sid` is falsy, so the first branch never fires in practice. Left
    unsplit deliberately — nothing today reads the distinction, and adding a
    signal for a caller that doesn't guard on `sid` is speculative until one
    exists. A future caller that skips that guard gets an ambiguous `[]`
    rather than a distinguishing signal; that caller should either add its
    own `sid` guard first (matching this module's only current caller) or
    split this function's return shape to disambiguate at that point.
    error."""
    if not session_id:
        return []
    dispositions = decisions.get(_KEY_HANDOFF_DISPOSITIONS) or {}
    if not dispositions:
        return []
    held = set(_held_handoff_basenames(worktree_root, session_id))
    root = Path(worktree_root)
    candidates: "list[tuple[str, str]]" = []
    for basename, entry in dispositions.items():
        if basename not in held:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("disposition") != _HANDOFF_DISPOSITION_SHIPPED:
            continue
        sha = entry.get("shipped_in")
        if not sha:
            continue
        active_path = root / "state" / "handoffs" / basename
        if not active_path.is_file():
            continue
        candidates.append((f"state/handoffs/{basename}", str(sha)))
    return candidates


def apply_ship_stamps(
    worktree_root: "Union[Path, str]", candidates: "list[tuple[str, str]]"
) -> "tuple[ShipStampOutcome, dict[str, str]]":
    """Writes all three fields the plan names, through the two named
    validating writers only — never hand-edited frontmatter (constraint a):

      1. `shipped_in` + `shipped_in_kind` (DR-096 lockstep) via
         `archive_stamp.stamp_shipped_in(kind="ship-commit", sha=...)` — the
         canonical caller-supplied-sha case; this function never resolves a
         sha itself, matching `handoff_stamp.py`'s own negative-spec.
      2. `deployment_state -> shipped` (pickup_ready -> false) via
         `handoff.transition`'s `ship` verb — the SAME op
         `archive_stamp._call_handoff_transition` uses for every other
         transition, called the identical way here. NEVER
         `handoff.ship_and_archive` (git-mv + its own archival commit — see
         this module's own top-of-section comment and the chunk body's
         explicit exclusion).

    Returns `(outcome, backups)` — `backups` is `{relpath: original_text}`,
    captured BEFORE either write, but ONLY for paths that reach full
    `stamped_paths` membership (both writes succeeded) — `revert_ship_stamps`
    uses it to restore on a failed/refused fold-in commit
    (WRITE-LANDS-THEN-COMMIT-FAILS: the stamp is not durable until the
    commit that carries it is known to have succeeded). A candidate whose
    backup read fails is skipped outright (never written blind). A
    candidate where `handoff.stamp` succeeds but the `ship` verb then fails
    is reverted IMMEDIATELY, right here — never left at the partial
    `handoff.stamp`-only state, which would be a shipped_in write with no
    commit ever queued to carry it and no later revert pass positioned to
    catch it (that half-state is excluded from `stamped_paths`, so a
    caller's own commit-outcome revert never sees it)."""
    from coordinator_core.archive_stamp import _resolve_repo_root_for, stamp_shipped_in
    from coordinator_core.ops.handoff_transition import _handler as _transition_handler

    root = Path(worktree_root)
    stamped: "list[str]" = []
    diagnostics: "list[str]" = []
    backups: "dict[str, str]" = {}
    for relpath, sha in candidates:
        abspath = root / relpath
        try:
            original_text = abspath.read_text(encoding="utf-8")
        except OSError as exc:
            diagnostics.append(f"{relpath}: could not read for backup, skipped: {exc}")
            continue

        outcome = stamp_shipped_in(str(abspath), kind="ship-commit", sha=sha)
        if outcome.exit_code != 0:
            diagnostics.append(f"{relpath}: handoff.stamp failed: {outcome.error}")
            continue

        worktree, repo_root = _resolve_repo_root_for(abspath)
        if worktree is None or repo_root is None:
            diagnostics.append(f"{relpath}: could not resolve git worktree for ship verb")
            revert_ship_stamps(root, [relpath], {relpath: original_text})
            continue
        try:
            ship_result = asyncio.run(
                _transition_handler(
                    {"handoff_path": str(abspath), "verb": "ship"}, repo_root=repo_root
                )
            )
        except Exception as exc:  # noqa: BLE001 - fold, never crash the close
            diagnostics.append(f"{relpath}: handoff.transition ship raised: {exc}")
            revert_ship_stamps(root, [relpath], {relpath: original_text})
            continue
        if int(ship_result.get("exit_code", 1)) != 0:
            diagnostics.append(f"{relpath}: handoff.transition ship failed: {ship_result.get('error')}")
            revert_ship_stamps(root, [relpath], {relpath: original_text})
            continue

        stamped.append(relpath)
        backups[relpath] = original_text

    stamped_set = set(stamped)
    outcome_obj = ShipStampOutcome(
        stamped_paths=tuple(stamped),
        skipped_paths=tuple(relpath for relpath, _sha in candidates if relpath not in stamped_set),
        attempted=len(candidates),
        diagnostics=tuple(diagnostics),
    )
    return outcome_obj, backups


def revert_ship_stamps(
    worktree_root: "Union[Path, str]", relpaths: "Sequence[str]", backups: "dict[str, str]"
) -> None:
    """WRITE-LANDS-THEN-COMMIT-FAILS: best-effort restore of the exact prior
    bytes captured by `apply_ship_stamps` before either write, for every
    `relpath` in `relpaths` that has a backup. Never composes new content
    (not a second hand-edit of frontmatter — constraint (a) is about
    authoring new field values, not restoring bytes this same call already
    read) and never raises: a restore failure leaves the stamp standing,
    which the caller must not treat as durable either way once the commit it
    was meant to ride has failed."""
    root = Path(worktree_root)
    for relpath in relpaths:
        original = backups.get(relpath)
        if original is None:
            continue
        try:
            (root / relpath).write_text(original, encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# C1 (state/dispatch-briefs/2026-08-31-a-close-disposes-the-baton-it-closed/
# C1.md) — dispose a NON-shipped baton the same close pass ship-stamps a
# shipped one. `resolve_ship_stamp_candidates` above ONLY ever selects
# disposition=="shipped"; closed/abandoned/continued fell through to
# nothing, leaving the baton non-terminal and its ledger claim held after a
# close that told the caller otherwise. SAME claim-ledger source
# (`_held_handoff_basenames` — the SAME `list_claims_by_session_checked`
# reader; NO corpus scan), SAME positive-membership rule (a held claim
# qualifies only because THIS close's own `decisions["handoff_dispositions"]`
# says so, never because it is merely held), SAME
# `state/handoffs/<basename>` active-on-disk restriction as the ship-stamp
# resolver above.
# ---------------------------------------------------------------------------

_HANDOFF_DISPOSITION_CLOSED = "closed"
_HANDOFF_DISPOSITION_ABANDONED = "abandoned"
_HANDOFF_DISPOSITION_CONTINUED = "continued"

#: `abandoned` maps onto the SAME `handoff.transition` `close` verb `closed`
#: already uses, with the SAME caller-supplied `closed_reason` requirement —
#: this is DR-084's own documented replacement, not a silent vocabulary
#: collapse: `_supersede`'s own docstring states plainly that
#: "deployment_state:abandoned has RETIRED — the old consumed+abandoned
#: expression ... is gone[; the replacement is] closed+closed_reason, [a]
#: human/session-only decision" (handoff_transition.py, `_supersede`
#: docstring). There is no separate `deployment_state:abandoned` writer
#: anywhere in this codebase to route `abandoned` onto instead — DR-084 left
#: it nowhere else to go, so writing `deployment_state:closed` for an
#: `abandoned` disposition IS honouring the caller's own disposition, not
#: overriding it.
_CLOSE_VERB_DISPOSITIONS = frozenset(
    {_HANDOFF_DISPOSITION_CLOSED, _HANDOFF_DISPOSITION_ABANDONED}
)

_HANDOFF_DISPOSAL_VALUES = frozenset(
    {_HANDOFF_DISPOSITION_CLOSED, _HANDOFF_DISPOSITION_ABANDONED, _HANDOFF_DISPOSITION_CONTINUED}
)


class CloseStampOutcome(NamedTuple):
    """`apply_close_stamps`'s own return shape — mirrors `ShipStampOutcome`'s
    "ran and found nothing" vs "never ran" distinction for the SIBLING
    disposal path (closed/abandoned/continued) `resolve_ship_stamp_
    candidates`/`apply_ship_stamps` never cover. `attempted` is the
    candidate count BEFORE any per-candidate failure, so `attempted > 0`
    with `disposed_paths == ()` reads as "ran, everything failed/skipped",
    distinct from `attempted == 0` ("nothing held qualified")."""

    disposed_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    attempted: int
    diagnostics: tuple[str, ...]


#: The "ran, found nothing" sentinel — mirrors `EMPTY_SHIP_STAMP_OUTCOME`'s
#: own rationale (a single module-level constant so callers cannot drift on
#: field values by hand-constructing this shape separately).
EMPTY_CLOSE_STAMP_OUTCOME = CloseStampOutcome(
    disposed_paths=(), skipped_paths=(), attempted=0, diagnostics=()
)


def resolve_close_stamp_candidates(
    worktree_root: "Union[Path, str]",
    session_id: Optional[str],
    decisions: "dict[str, Any]",
) -> "list[tuple[str, str, Optional[str]]]":
    """Returns `[(handoff_relpath, disposition, closed_reason), ...]` for
    every held claim this close's own `decisions["handoff_dispositions"]`
    records as `closed`/`abandoned`/`continued` in THIS session — the
    sibling of `resolve_ship_stamp_candidates` for every disposition that
    resolver's positive `disposition == "shipped"` selection excludes.

    `closed_reason` is read alongside `disposition` from the SAME
    caller-supplied entry (never defaulted — a `closed`/`abandoned` entry
    with no `closed_reason` is a caller error `apply_close_stamps` surfaces
    as a diagnostic and skips, never a gap this resolver fills by
    guessing). `continued` entries carry `closed_reason=None`
    unconditionally — a disposition that is ALREADY terminal by definition
    needs no reason and none is read for it.

    Returns `[]` (never raises) for a falsy `session_id` or an empty/absent
    `decisions["handoff_dispositions"]` — same two "nothing to do"
    early-outs as `resolve_ship_stamp_candidates`."""
    if not session_id:
        return []
    dispositions = decisions.get(_KEY_HANDOFF_DISPOSITIONS) or {}
    if not dispositions:
        return []
    held = set(_held_handoff_basenames(worktree_root, session_id))
    root = Path(worktree_root)
    candidates: "list[tuple[str, str, Optional[str]]]" = []
    for basename, entry in dispositions.items():
        if basename not in held:
            continue
        if not isinstance(entry, dict):
            continue
        disposition = entry.get("disposition")
        if disposition not in _HANDOFF_DISPOSAL_VALUES:
            continue
        active_path = root / "state" / "handoffs" / basename
        if not active_path.is_file():
            continue
        closed_reason = (
            entry.get("closed_reason") if disposition in _CLOSE_VERB_DISPOSITIONS else None
        )
        candidates.append((f"state/handoffs/{basename}", disposition, closed_reason))
    return candidates


def _release_handoff_claim(worktree_root: "Union[Path, str]", basename: str) -> None:
    """Releases this session's own `handoff-claims/<basename>` ledger entry —
    self-release only (`session.claims.release_artifact` resolves holder
    identity from `cwd`/`worktree_root` itself, same mechanism
    `_held_handoff_basenames` already reads this session's claims through).
    Best-effort, never raises: mirrors `_release_committed_path_claims`'s
    own fail-safe posture — a release failure must never surface as this
    disposal's own failure, since the terminal write it follows (per this
    section's ordering guarantee) is already durable on disk; the residue
    is a stale claim on an already-terminal record, not a live one wrongly
    freed."""
    try:
        from coordinator_core.session.claims import release_artifact

        release_artifact("handoff", basename, cwd=str(worktree_root))
    except Exception:
        pass


def apply_close_stamps(
    worktree_root: "Union[Path, str]",
    candidates: "list[tuple[str, str, Optional[str]]]",
) -> "tuple[CloseStampOutcome, dict[str, str]]":
    """Disposes each `resolve_close_stamp_candidates` candidate:

      1. `closed`/`abandoned` — stamps `deployment_state: closed` (plus the
         caller-supplied `closed_reason`) via `handoff.transition`'s `close`
         verb, the SAME op `apply_ship_stamps` uses for its own `ship`
         verb. Refuses (diagnostic, skip, no write, no release) a
         `closed_reason` that is missing or not one of `_CLOSED_REASONS`
         (`cancelled | displaced | stale`) — a fabricated reason is exactly
         what the chunk body's item (a) forbids; the caller's own entry is
         the sole source.
      2. `continued` — ALREADY terminal by definition (DR-084: `continued`
         is the automated-writer terminal `handoff.transition supersede`
         stamps) and gets no re-stamp. This function only ASSERTS the
         on-disk `deployment_state` already reads `continued` before
         disposing it (`extract_frontmatter_scalar`, the same lightweight
         fence-scoped reader `handoff_transition.py` itself uses for an
         analogous read-only check) — a mismatch is a diagnostic + skip
         (no release), never a silent guess or an overwrite.

    ORDERING IS LOAD-BEARING (chunk body item (c)): for every candidate the
    terminal stamp (or the `continued` assertion) is checked/applied FIRST,
    the ledger claim released SECOND — never the reverse. The window this
    ordering closes is exactly what `pickup_assemble`'s held-claim read
    (`_resolve_held_handoff_for_session`, the same reader
    `baton_assemble`'s own docstring names) would otherwise misread as
    live, inheritable work rather than a disposed baton: a claim released
    while the record is still non-terminal reads, to that reader, as
    "nothing holds this any more AND it is still open" — precisely the
    corruption this chunk exists to close. A crash between the two steps
    for a given candidate leaves the SAFE half standing: terminal-on-disk,
    claim still held — the record itself no longer reads as live work
    either way, and a later pass can still find and release the stale
    claim.

    Returns `(outcome, backups)` — `backups` is `{relpath: original_text}`,
    captured BEFORE a `closed`/`abandoned` write (mirrors `apply_ship_
    stamps`'s own backup contract) so a caller whose own fold-in commit
    later fails/is refused can restore via `revert_close_stamps`. A
    `continued` candidate that only asserted (no write) never appears in
    `backups` — there is nothing to revert for it. Ledger-claim release is
    best-effort per candidate (`_release_handoff_claim`) and is never
    itself reverted — a release failure leaves a stale claim on an
    already-terminal record (safe residue, per the ordering note above); a
    release success is not undone just because a later commit fails, since
    the terminal write is what `revert_close_stamps` restores instead."""
    from coordinator_core.ops._fm_util import extract_frontmatter_scalar
    from coordinator_core.ops.handoff_transition import _CLOSED_REASONS
    from coordinator_core.ops.handoff_transition import _handler as _transition_handler

    root = Path(worktree_root)
    disposed: "list[str]" = []
    diagnostics: "list[str]" = []
    backups: "dict[str, str]" = {}

    for relpath, disposition, closed_reason in candidates:
        abspath = root / relpath
        basename = Path(relpath).name

        if disposition == _HANDOFF_DISPOSITION_CONTINUED:
            try:
                text = abspath.read_text(encoding="utf-8")
            except OSError as exc:
                diagnostics.append(f"{relpath}: could not read for continued assertion: {exc}")
                continue
            on_disk = extract_frontmatter_scalar(text, "deployment_state")
            if on_disk != _HANDOFF_DISPOSITION_CONTINUED:
                diagnostics.append(
                    f"{relpath}: disposition 'continued' asserted deployment_state:"
                    f"continued but found {on_disk!r} — refusing to stamp or release"
                )
                continue
            disposed.append(relpath)
            _release_handoff_claim(worktree_root, basename)
            continue

        # closed | abandoned
        if closed_reason not in _CLOSED_REASONS:
            diagnostics.append(
                f"{relpath}: disposition {disposition!r} requires a closed_reason in "
                f"{sorted(_CLOSED_REASONS)} (got {closed_reason!r}) — refusing to "
                "fabricate one; skipped, no write"
            )
            continue

        try:
            original_text = abspath.read_text(encoding="utf-8")
        except OSError as exc:
            diagnostics.append(f"{relpath}: could not read for backup, skipped: {exc}")
            continue

        try:
            close_result = asyncio.run(
                _transition_handler(
                    {"handoff_path": str(abspath), "verb": "close", "reason": closed_reason},
                    repo_root=root,
                )
            )
        except Exception as exc:  # noqa: BLE001 - fold, never crash the close
            diagnostics.append(f"{relpath}: handoff.transition close raised: {exc}")
            continue
        if int(close_result.get("exit_code", 1)) != 0:
            diagnostics.append(f"{relpath}: handoff.transition close failed: {close_result.get('error')}")
            continue

        disposed.append(relpath)
        backups[relpath] = original_text
        _release_handoff_claim(worktree_root, basename)

    disposed_set = set(disposed)
    outcome_obj = CloseStampOutcome(
        disposed_paths=tuple(disposed),
        skipped_paths=tuple(
            relpath for relpath, _disposition, _reason in candidates if relpath not in disposed_set
        ),
        attempted=len(candidates),
        diagnostics=tuple(diagnostics),
    )
    return outcome_obj, backups


def revert_close_stamps(
    worktree_root: "Union[Path, str]", relpaths: "Sequence[str]", backups: "dict[str, str]"
) -> None:
    """WRITE-LANDS-THEN-COMMIT-FAILS: best-effort restore of the exact prior
    bytes captured by `apply_close_stamps` before its `closed`/`abandoned`
    write, for every `relpath` in `relpaths` that has a backup — mirrors
    `revert_ship_stamps` exactly (same rationale, same never-raises
    contract). A `continued` candidate has no backup entry (nothing was
    written for it) and is silently skipped here, same as any other
    `relpath` absent from `backups`. Never touches ledger-claim state —
    `apply_close_stamps`'s own docstring names why a release is not
    reverted by this function."""
    root = Path(worktree_root)
    for relpath in relpaths:
        original = backups.get(relpath)
        if original is None:
            continue
        try:
            (root / relpath).write_text(original, encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Step 3.35 — d-push-outstanding — wires push_outstanding() (C4b, 2026-08-25,
# docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md)
# ---------------------------------------------------------------------------


def run_push_outstanding_tail(worktree_root: "Union[Path, str]") -> Dict[str, Any]:
    """The "separate, later step" `run_close_commit`'s own docstring names
    ("A caller wanting a push does so as its own separate, later step; this
    function never pushes") -- `run_close_commit` calls `commit_paths` (C3),
    which has no push leg at all (hard constraints 5/6's old
    `push_mode=PUSH_MODE_NEVER` binding is now structural, not an argument
    value), and nothing else in this module's Step 3 push-confirmation tree
    issues `git push` either (`compute_push_landed_gate`'s own docstring:
    "Never issues `git push` itself"). This function is that missing
    producer, wired to `coordinator_core.ops.push_outstanding.
    push_outstanding` -- the SAME primitive the C4-registered `push.
    outstanding` op exposes to the four DoE-owned cadence surfaces (see that
    module's own docstring); this call reaches it in-process, since both
    caller and callee live in this repo.

    Lazily imports (mirrors `run_close_commit`'s own lazy import of
    `commit_pipeline` immediately above -- this module is on the assemble
    hot path and `commit_pipeline`/`push_outstanding` pull in the op-registry
    side effects of the full commit-gate stack with them).

    Best-effort: any exception raised while resolving or pushing is folded
    into the returned dict as `push_status: "push-failed"` rather than
    propagating -- a push failure must never crash the close sequence past
    the point where claim release has already run (`run_close_commit_and_
    release_claims`'s own `finally`), the same fail-safe direction
    `_release_committed_path_claims`/`_release_governing_plan_claim` take
    for the sibling best-effort releases in this module.

    Returns a flattened dict -- `push_status` (the canonical `commit_
    pipeline.derive_push_status` vocabulary), `acted`, `skipped`, `failed`,
    `unconfirmed` -- the same shape the `push.outstanding` op handler
    returns to ITS callers, so a report reader sees one vocabulary
    regardless of which caller reached this primitive.
    """
    from coordinator_core.ops.ceremony.push import derive_push_status
    from coordinator_core.ops.push_outstanding import push_outstanding

    try:
        outcome = push_outstanding(worktree_root)
    except Exception as exc:  # noqa: BLE001 - best-effort, never crash the close sequence
        return {
            "push_status": "push-failed",
            "acted": [],
            "skipped": [],
            "failed": [f"push_outstanding raised: {exc}"],
            "unconfirmed": [],
        }

    return {
        "push_status": derive_push_status(outcome),
        "acted": list(outcome.acted),
        "skipped": list(outcome.skipped),
        "failed": list(outcome.failed),
        "unconfirmed": list(outcome.unconfirmed),
    }


# ---------------------------------------------------------------------------
# Step 3 push-confirmation branch tree — d-verify-push-landed (read-only)
# ---------------------------------------------------------------------------


class PushLandedGate(NamedTuple):
    pushed: Optional[bool]
    deferred: bool
    unpushed_shas: tuple[str, ...]
    summary_line: str
    declined: bool = False
    #: The publish obligation is a named future checkpoint's, not this
    #: pass's. Never interchangeable with `declined` (policy refused, so
    #: nothing ever publishes) or `deferred` (a detached child may already
    #: be mid-flight) — see `compute_push_landed_gate`'s own docstring.
    cadence_pending: bool = False


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
    - `push_status == "cadence-pending"`: the commit was made under the
      cadence regime (`run_close_commit`'s `push_mode=PUSH_MODE_NEVER`) and
      its publish obligation belongs to a NAMED future checkpoint's own
      `push_outstanding()` call. Like `declined`, nothing is in flight, so
      the `deferred` arm's "check again shortly" guidance is wrong here
      too; unlike `declined`, something WILL publish it, so this is not a
      branch-policy refusal either. Querying `origin/<branch>` would find
      the commit unpushed and report a failure for what is the normal,
      correct cadence outcome — so this short-circuits without querying.
      Producer note: `commit_pipeline` itself never emits this value (see
      its canonical-vocabulary comment) — `derive_push_status` reports
      `"not-attempted"`, which is silent on whether anything will ever
      publish. Only a surface that KNOWS a checkpoint follows may promote
      it, which is why this arm keys on the richer member rather than on
      `"not-attempted"`: treating every `"not-attempted"` as cadence-
      pending would silently pass a commit nothing is going to publish.

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

    if push_status == "cadence-pending":
        return PushLandedGate(
            pushed=None,
            deferred=False,
            unpushed_shas=(),
            summary_line=(
                "Pushed to remote: cadence-pending "
                "(publish deferred to the next cadence checkpoint — nothing in flight)"
            ),
            cadence_pending=True,
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
# DR-335 — close-out publish-lag advisory (read-only)
# ---------------------------------------------------------------------------


def compute_publish_lag_advisory(repo_root: Path) -> Optional[str]:
    """DR-335, call site (b): "reported as done while inert" — at close-out,
    if this session's own work left engine-touching commits unpublished,
    say so. Reuses `coordinator_core.warm.skew.publish_lag` /
    `publish_lag_message` verbatim (same threshold, same two-git-call
    bound, same register) rather than re-deriving the computation — this
    function is placement only, matching `compute_push_landed_gate`'s own
    read-only, git-log-only shape immediately above.

    Returns `None` whenever the lag helper cannot establish a signal (no
    stamp, unresolvable sha, below `PUBLISH_LAG_THRESHOLD_MINUTES`, or an
    unexpected exception anywhere in the chain) — never raises, per
    DR-335's negative spec that an ordinary between-rounds gap is expected
    behaviour, not a defect this gate reports on.
    """
    try:
        lag = _skew.publish_lag(_current_engine_clone(), Path(repo_root))
    except Exception:
        return None
    if lag is None:
        return None
    return _skew.publish_lag_message(lag, site="close-out")


# ---------------------------------------------------------------------------
# Step 3.5 — d-release-plan-claim — REMOVED (ceremony.wsc_tail kill,
# 2026-08-23): the governing-plan claim-release directive depended
# exclusively on `d-run-wsc-tail` landing (`depends_on`/`{d-run-wsc-tail.
# landed}` ordering token) to know the commit it was guarding actually
# happened. With that producer gone, this directive has no way to
# establish its own precondition without inventing a replacement signal —
# not done here, per the kill's own delete-the-step, don't-redesign rule.
# `/workstream-complete` no longer auto-releases a governing-plan
# execution-lock claim; `session-claim-cli release-artifact` itself is
# untouched and remains callable directly.
# (Session archival — formerly `d-archive-session-claim` here — moved to
# session END, not workstream close; see `__init__.py`'s call-site comment
# at the Step 3/3.5/3.6 assembly point. `archive-session-scope.py archive-session` and
# `coordinator_core/session/scope.py`'s `archive()` remain live for that
# SessionEnd-hook caller; only this module's builder was removed.)


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
    ("publish_lag", "Publish lag"),
    ("flag_to_pm", "Flag to PM"),
)


def render_final_summary(
    work_done: str,
    pushed: str,
    completeness_checklist: str = "",
    auto_memory_drain: str = "",
    deferral_harvest: str = "",
    post_summary_reconcile: str = "",
    publish_lag: str = "",
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
    rebuilds the same fixed block one default at a time.

    `publish_lag` is DR-335's close-out advisory: pass
    `compute_publish_lag_advisory(repo_root)`'s return value straight
    through — `None`/empty stays silent (the ordinary case), a non-empty
    string is `compute_publish_lag_advisory`'s already-formatted,
    already-threshold-gated message, never re-derived here."""
    lines = [_SUMMARY_HEAD.format(work_done=work_done, pushed=pushed)]
    values = {
        "completeness_checklist": completeness_checklist,
        "auto_memory_drain": auto_memory_drain,
        "deferral_harvest": deferral_harvest,
        "post_summary_reconcile": post_summary_reconcile,
        "publish_lag": publish_lag,
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
