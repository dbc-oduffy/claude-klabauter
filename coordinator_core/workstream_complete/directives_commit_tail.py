"""
coordinator_core.workstream_complete.directives_commit_tail — the
dirty-tree, commit-tail, push-verification, claim-release, cadence and
final-summary builders for the `workstream-complete-assemble` computed-skill
engine.

Purpose: computes what remains of Steps 3.0/3.6/4 (`SKILL.md` census rows
`d-accumulate-session-paths`, `d-verify-push-landed`,
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

Step 3/3.5, 2026-08-23 kill (`ceremony.wsc_tail`) and 2026-08-25 rebuild
(DR-358, `docs/decisions/DR-358-the-close-ceremony-shape-after-the-kill.md`):
`d-close-tail-args` (`build_close_tail_args_directive`, fronting
`coordinator/bin/wsc-close.py tail-args`) and `d-run-wsc-tail`
(`build_wsc_tail_directive`, fronting the now-deleted `coordinator/bin/
wsc-tail.py` trampoline for the killed `ceremony.wsc_tail` op) were removed
outright by the kill. DR-358 rules that the OPERATOR requirement they
discharged — closing a session without hand-landing the commit — is
rebuilt, not restored: `run_close_commit` below is the rebuilt shape, an
in-process call directly against `commit_pipeline.run_commit_pipeline` at
`push_mode=PUSH_MODE_NEVER` (hard constraints 5/6), with no new CLI or
`directives[]` layer above it — `d-close-tail-args`'s former argument
computation merges into this one call per DR-358's own ruling, rather than
being rebuilt as a separate step. `d-release-plan-claim`
(`build_release_plan_claim_directive`, fronting `session-claim-cli
release-artifact`) remains gone as a standalone directive — DR-358 rules it
discharged by an inline call to the existing native port
`ops/ceremony/tail_ops.py :: cs_release_artifact`, wired into
`run_close_commit`'s route by a later chunk (C5), not rebuilt as a directive
either; see `state/kill-ledger.md` for the original removal record.
`run_close_commit_and_release_claims` (this module, C5) is the wired route:
it wraps `run_close_commit` and releases BOTH claim mechanisms hard
constraint 4 and AC5 separately require — the per-path `session/scope.py ::
release_committed_claims` (hard constraint 4) and the governing-plan
artifact claim via `cs_release_artifact` (AC5) — unconditionally, on both
the success and failure exit of the wrapped commit call, per DR-358's
failure-path ruling. A caller of this rebuilt close route should call
`run_close_commit_and_release_claims`, never `run_close_commit` directly —
the latter releases neither claim (see its own docstring).

Consumes (orchestrates, reimplements none):
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
    - `run_close_commit` does NOT compose `subject`/`prose` text itself
      (`commit-message-authoring` stays `judgments.py`'s C2f call), does NOT
      decide `stage_paths` (that is `accumulate_session_paths`'/
      `resolve_known_concurrent_paths`'s job above), and does NOT release the
      governing-plan claim (`cs_release_artifact` wiring is C5's route, not
      this function's body) — it only turns already-decided values into one
      `run_commit_pipeline` call, at a push mode this module never lets the
      caller override.
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
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Union

from coordinator_core.ceremony_common.tail import build_ceremony_close_tail
from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _session_liveness
from coordinator_core.session import scope as session_scope
from coordinator_core.warm import skew as _skew
from coordinator_core.warm.engine_root import current_engine_clone as _current_engine_clone
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
    union already known by the time this step runs. Previously spliced by
    callers into `build_close_tail_args_directive`'s / `build_wsc_tail_
    directive`'s `--stage-paths` argument (both removed, ceremony.wsc_tail
    kill, 2026-08-23); this function is now unused pending a replacement
    caller.

    Reconciliation with C5's in-process auto-commit safety net
    (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-
    wrote.md § C5, `coordinator_core.ops.ceremony.wsc_tail`'s post-commit
    `session_auto_commit_safety_net` step): this function and that step
    answer two genuinely different questions, not one question computed
    twice, so keeping both is deliberate rather than duplicative.

    This function (and `_peer_subagent_share_paths`/
    `resolve_known_concurrent_paths` above it) run at ASSEMBLE time, before
    any commit — they compute the explicit `--stage-paths` pathspec the WSC
    commit is told to stage, from signals already known before the ceremony
    starts (Step 2.67's session-authored-file tracking, the peer-exclusion
    scan). C5's safety net runs AFTER that commit has already landed, via a
    fresh `coordinator_core.ops.session.safe_commit_offer.compute_offer`
    read taken at that later moment — it exists specifically to catch
    session-claimed paths that became dirty (or were newly touched by a
    dispatched sub-agent) DURING the ceremony's own pre-commit tail steps
    (roadmap-callout render, review-trail write), after this module's
    assemble-time pathspec was already fixed and handed to `wsc-tail`.
    Folding the safety net's live re-scan into THIS function would require
    re-deriving it at assemble time, which cannot see a file that does not
    dirty until partway through the ceremony that follows — the two run at
    different points in the pipeline and neither can subsume the other.
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
    is the pre-existing single-`dict` shape; (2) per-ENTRY, when
    `decisions["review"]` is the additive `list[dict]` shape (partitioned-
    review fix, `workstream_complete.build_write_trail_directives`), which
    calls this once per list entry to decide which entries qualify,
    mirroring `build_write_trail_directives`'s own "an incomplete entry
    contributes nothing, silently" convention. (`build_close_tail_args_
    directive`/`wsc-tail.py`, formerly the other consumer of this shape,
    were removed in the ceremony.wsc_tail kill, 2026-08-23.)

    Returns `False` for any non-`dict` input (a bare falsy `review`, or a
    list entry that is itself not a dict) — this function only ever judges
    ONE candidate dict at a time; a caller holding a `list` must iterate
    and call this per element, never pass the list itself."""
    if not isinstance(review, dict):
        return False
    return all(review.get(k) not in (None, "") for k in _REVIEW_REQUIRED_FIELDS)


def _review_any_qualifies(review: Any) -> bool:
    """The qualification predicate for "does decisions['review'] back
    anything real" — dict shape delegates straight to `_review_fields_
    present`; list shape is true iff ANY entry qualifies. Anything else
    (falsy, or a shape `validate_review_shape` would already have rejected)
    is `False`.

    Formerly extracted so `build_wsc_tail_directive`'s `--adjudication-
    present` predicate and `build_close_tail_args_directive`'s `--review-*`/
    `--review-slice` predicate decided the SAME fact from the SAME code —
    before this helper existed they were two independent predicates (bare
    truthiness vs. `_review_fields_present`) that could disagree on an
    incomplete-but-recognized shape (a dict missing required fields, a list
    where every entry is incomplete, `[{}]`, `{'workstream': 'x'}`): that
    input composed `--adjudication-present` while composing NO `--review-*`/
    `--review-slice` token at all, so the mismatch surfaced only after a
    live ceremony had already committed, as a `failed_critical`-driven
    exit_code 2 the operator is told not to re-run for. `_raise_on_review_
    truthy_unqualified` (below) caught that class at assemble time, before
    either builder's directive was even constructed. Both builders were
    removed in the ceremony.wsc_tail kill, 2026-08-23."""
    if isinstance(review, list):
        return any(_review_fields_present(entry) for entry in review)
    return _review_fields_present(review)


def _raise_on_review_truthy_unqualified(review: Any) -> None:
    """Raises `ValueError` when `review` is truthy but `_review_any_
    qualifies` is `False` — the disagreement class `_review_any_qualifies`'s
    own docstring names. Formerly called once, from `build_wsc_tail_
    directive` (the `--adjudication-present` producer, removed in the
    ceremony.wsc_tail kill, 2026-08-23), which `__init__.py` always called in
    the same assemble pass as `build_close_tail_args_directive` (also
    removed) — raising here aborted assembly before either directive reached
    `directives[]`, same loud-at-ingestion posture `validate_review_shape`
    already establishes for shape, one layer up for completeness. Leaves the
    post-commit `failed_critical` branch (`tail_ops.write_review_trail`) as
    the defence-in-depth backstop it was designed to be, rather than the
    primary detector for this class."""
    if review and not _review_any_qualifies(review):
        if isinstance(review, list):
            detail = f"every entry of the {len(review)}-item list is missing required field(s)"
        else:
            missing = sorted(k for k in _REVIEW_REQUIRED_FIELDS if review.get(k) in (None, ""))
            detail = f"missing required field(s): {missing!r}"
        raise ValueError(
            f"decisions['review'] is truthy but qualifies for no --review-*/--review-slice "
            f"token ({detail}) — a close asserting a review must supply complete review "
            f"metadata, or omit decisions['review'] entirely."
        )


def _raise_on_review_enum_values(review: Any) -> None:
    """Raises `ValueError` when any composed review entry carries an
    out-of-vocabulary `reviewer`/`scope`/`verdict`/`scope_kind`.

    Completes the assemble-time validator family (`validate_review_shape` for
    shape, `_raise_on_review_truthy_unqualified` for completeness) with the
    fourth axis — value legality. Without it, the four closed vocabularies were
    reachable ONLY by being rejected by them: `wsc-close tail-args --help`
    lists the `--review-*` flag names with no value constraints, and the review
    block is a free-form map carrying no `dispositions[]` the way every other
    `judgment_points[]` entry does. The discovery path was author-a-guess, run
    apply, get rejected.

    Firing here rather than at the writer is the whole point: `d-write-trail`
    runs FIRST in apply order, so a wrong enum turned the pass into `EXIT: 4
    PARTIAL_MUTATION` and forced a reconcile-before-retry. Assembly aborts
    before any directive reaches `directives[]`, so the same mistake is now a
    question asked before anything moved.

    Messages come from `review_trail_write.review_enum_errors` — the writer's
    own authority, not a copy — so the text is byte-identical to the
    apply-time rejection and cannot drift from the vocabularies it names.
    Imported lazily: this module is on the assemble hot path and
    `review_trail_write` pulls the op-registry side effects with it.

    Spec backlink: cross-repo/inbox/2026-08-18-doe-claude-em-review-trail-
    write-enums-undiscoverable-until-apply.md § 1.
    """
    from coordinator_core.ops.review_trail_write import review_enum_errors

    entries = review if isinstance(review, list) else [review]
    errors: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not _review_fields_present(entry):
            # A shape-invalid or incomplete entry is not this validator's
            # class — `validate_review_shape` and `_raise_on_review_truthy_
            # unqualified` own those, and an incomplete entry composes no
            # token at all, so its enum values never reach the writer.
            continue
        entry_errors = review_enum_errors(
            reviewer=str(entry["reviewer"]),
            scope=str(entry["scope"]),
            verdict=str(entry["verdict"]),
            # scope_kind is optional in the dict and defaults at the writer;
            # validate the value that will actually be written, not "".
            scope_kind=str(entry["scope_kind"]) if entry.get("scope_kind") else "diff",
        )
        if entry_errors and isinstance(review, list):
            entry_errors = [f"slice[{index}]: {e}" for e in entry_errors]
        errors += entry_errors
    if errors:
        raise ValueError(
            " | ".join(errors)
            + " — rejected at assemble time; correcting it here costs no partial mutation."
        )


#: Derived from `_REVIEW_REQUIRED_FIELDS` (never hand-duplicated) — the flat
#: `review_<field>` spellings a caller might plausibly place directly on
#: `decisions` instead of nesting them under `decisions["review"]`. A caller
#: who does this looks plausible at the callsite (2026-07-30 cross-repo memo:
#: `cross-repo/archive/2026-07-30-doe-claude-em-wsc-review-trail-passthrough-
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
    missing one" (formerly `build_close_tail_args_directive`'s own docstring,
    removed in the ceremony.wsc_tail kill, 2026-08-23).
    This applies that SAME principle one layer up, at the `decisions` dict
    itself — a legitimately absent `review` dict must not fail loud (the
    review slice is genuinely optional), but a flat `review_*` key sitting
    unconsumed on `decisions` is not "absent", it is a caller's contract
    mismatch that this module can detect and was previously swallowing in
    silence. Diagnostic only, never raises — the ceremony still proceeds
    (the review dict, if genuinely supplied, is unaffected), it just stops
    doing so quietly. Printed to stderr, never stdout: this module's own
    `brief`/`apply` CLI seams (`coordinator_core.workstream_complete.
    __init__`) serialize their JSON result to stdout, and polluting it
    would be worse than the silent drop this closes.
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


#: The optional keys a single `review` dict (scalar shape, or one entry of
#: the list shape) may carry alongside `_REVIEW_REQUIRED_FIELDS` -- mirrors
#: `wsc-tail.py`'s `_REVIEW_SLICE_ALLOWED_KEYS` plus `workstream` (that one
#: forwarded via wsc-tail.py's own `--review-workstream`, not through the
#: `--review-slice` JSON path). Membership only, not completeness: a dict
#: whose keys are a subset of `_REVIEW_ALLOWED_KEYS` is a legal shape even
#: when a required field is absent -- that stays `_review_fields_present`'s
#: and `build_write_trail_directives`'s own "an incomplete entry contributes
#: nothing, silently" convention (state/bug-backlog/2026-08-14-wsc-apply-
#: accepts-an-unconsumed-decision-debea052f8c5.yaml DESIGN NOTE: fix at
#: decisions-ingestion, leave the lower per-layer conventions alone).
_REVIEW_OPTIONAL_KEYS: tuple[str, ...] = ("scope_kind", "reviewer_evidence", "workstream")
_REVIEW_ALLOWED_KEYS: frozenset[str] = frozenset(_REVIEW_REQUIRED_FIELDS) | frozenset(_REVIEW_OPTIONAL_KEYS)

_REVIEW_SHAPE_MESSAGE = (
    "decisions['review'] must be one of: falsy; a dict whose keys are a "
    f"subset of {sorted(_REVIEW_ALLOWED_KEYS)!r}; or a list of such dicts."
)


def _raise_on_unrecognized_review_dict(entry: dict[str, Any]) -> None:
    """Raises when `entry` (the scalar `review` dict, or one `list` entry)
    carries a key outside `_REVIEW_ALLOWED_KEYS`. Silent on a dict whose
    keys are all recognized, even if incomplete -- see `_REVIEW_ALLOWED_KEYS`
    docstring above for why completeness is not this function's concern."""
    unrecognized = sorted(set(entry) - _REVIEW_ALLOWED_KEYS)
    if not unrecognized:
        return
    missing = sorted(k for k in _REVIEW_REQUIRED_FIELDS if entry.get(k) in (None, ""))
    detail = f" Unrecognized key(s): {unrecognized!r}."
    if missing:
        detail += f" Missing required field(s): {missing!r}."
    raise ValueError(f"{_REVIEW_SHAPE_MESSAGE}{detail}")


def validate_review_shape(review: Any) -> None:
    """The shared POSITIVE shape validator for `decisions['review']` --
    called from `workstream_complete.build_write_trail_directives` (formerly
    also from `build_close_tail_args_directive`, removed in the
    ceremony.wsc_tail kill, 2026-08-23) so they could not diverge. Two
    independent silent-dropping sites for one key was the
    structural defect under state/bug-backlog/2026-08-14-wsc-apply-accepts-
    an-unconsumed-decision-debea052f8c5.yaml (corroborated cross-repo:
    cross-repo/inbox/2026-08-14-example-cockpit-repo-em-review-decisions-dict-
    drops-silently-and-mints-a-not-reviewed-waiver.md) -- a dict nested one
    key deeper than the accepted shapes (e.g. `{'slices': [...], ...}`)
    passed both sites without a single flag or record produced, and the
    ceremony still exited 0.

    RAISES `ValueError` on anything outside the three legal shapes: falsy;
    a dict whose keys are a subset of `_REVIEW_ALLOWED_KEYS`; or a list of
    such dicts. Validates KEY-SET membership only, never per-field
    completeness -- an incomplete-but-recognized dict/entry is left to the
    lower layers' own pre-existing "contributes nothing, silently"
    convention (`_review_fields_present`, `build_write_trail_directives`),
    untouched per that backlog entry's DESIGN NOTE. This is a POSITIVE
    validator (accepts by shape), distinct from `_warn_unrecognized_review_
    keys` above, which is a diagnostic-only NEGATIVE check on a different
    object -- flat `review_*` keys sitting on `decisions` itself, not on
    `decisions['review']`.
    """
    if not review:
        return
    if isinstance(review, dict):
        _raise_on_unrecognized_review_dict(review)
        return
    if isinstance(review, list):
        for entry in review:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{_REVIEW_SHAPE_MESSAGE} Got a list entry of type "
                    f"{type(entry).__name__}, not a dict."
                )
            _raise_on_unrecognized_review_dict(entry)
        return
    raise ValueError(f"{_REVIEW_SHAPE_MESSAGE} Got {type(review).__name__}.")


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
    measures (AC3's shape budget: `run_commit_pipeline(...,
    push_mode=PUSH_MODE_NEVER)`, called directly IN-PROCESS, no new CLI or
    `directives[]` layer above it — `d-close-tail-args`'s former argument
    computation merges into this one call, per DR-358's own ruling).

    `push_mode` is NEVER a parameter here — hard constraints 5/6 bind
    absolutely: this always calls `run_commit_pipeline` (never
    `git_native.commit_scoped` directly, which gets staging safety and
    silently skips `commit_gates.carry_gate` and every other gate) at
    `push_mode=PUSH_MODE_NEVER` (never omitted, which now defaults to
    `PUSH_MODE_NONE` since 2026-08-26, not `PUSH_MODE_SYNC` — and never
    `PUSH_MODE_SYNC` itself, a synchronous network push, and never
    `"deferred"` — `_PUSH_MODES_
    SUPPRESSING_POST_COMMIT_HOOK == frozenset({SYNC, NEVER})` leaves the
    post-commit hook's push armed under `"deferred"`). A caller wanting a
    push does so as its own separate, later step; this function never
    pushes.

    Every keyword argument here is a caller-supplied, already-decided value
    passed straight through to `run_commit_pipeline` — `subject`/`prose`
    (commit-message-authoring, `judgments.py`'s C2f), `stage_paths`
    (`accumulate_session_paths`/`resolve_known_concurrent_paths` above),
    `deleted_paths`/`kept_entries`/`trailers` (the Step-2.67 message blocks,
    `commit_pipeline.compose_message`'s own contract). This function decides
    none of them and composes no message text itself — see this module's own
    Negative-spec. AC8 (the `Closes:` composed-by-hand message text
    surviving verbatim) is a property of NOT mutating `prose` anywhere
    between the caller and `run_commit_pipeline`'s own `compose_message`
    call — this function's whole job is to not be the place that breaks
    that.

    Governing-plan claim release (`d-release-plan-claim`'s DR-358 ruling,
    `ops/ceremony/tail_ops.py :: cs_release_artifact`) is NOT called here,
    nor is hard constraint 4's per-path `release_committed_claims` — both
    are wired by `run_close_commit_and_release_claims` (this module, C5),
    the wrapper that calls this function and releases both claims at both
    its success and failure exits; this function's own return is
    `run_commit_pipeline`'s `PipelineResult`, unmodified. A caller closing a
    session should call `run_close_commit_and_release_claims`, not this
    function directly.

    Imports `run_commit_pipeline`/`PUSH_MODE_NEVER` lazily (mirrors
    `_raise_on_review_enum_values`'s own lazy import above) — this module is
    on the assemble hot path and `commit_pipeline` pulls in the op-registry
    side effects of the full commit-gate stack with it.
    """
    from coordinator_core.ops.ceremony.commit_pipeline import (
        PUSH_MODE_NEVER,
        run_commit_pipeline,
    )

    return run_commit_pipeline(
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
        push_mode=PUSH_MODE_NEVER,
        deliverable_id=deliverable_id,
        attributed_session_id=attributed_session_id,
    )


def _release_committed_path_claims(
    worktree_root: "Union[Path, str]", session_id: str, stage_paths: "Sequence[str]"
) -> None:
    """Hard constraint 4's per-route wiring of `session/scope.py ::
    release_committed_claims` — the PATH-claim mechanism (`touched.txt` `R`
    events), never wired automatically by `run_commit_pipeline`/`git_native.py`
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
# Step 3.35 — d-push-outstanding — wires push_outstanding() (C4b, 2026-08-25,
# docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md)
# ---------------------------------------------------------------------------


def run_push_outstanding_tail(worktree_root: "Union[Path, str]") -> Dict[str, Any]:
    """The "separate, later step" `run_close_commit`'s own docstring names
    ("A caller wanting a push does so as its own separate, later step; this
    function never pushes") -- `run_close_commit` always calls
    `run_commit_pipeline` at `push_mode=PUSH_MODE_NEVER` (hard constraints
    5/6), and nothing else in this module's Step 3 push-confirmation tree
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
    from coordinator_core.ops.ceremony.commit_pipeline import derive_push_status
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
# at the Step 3/3.5/3.6 assembly point. `wsc-close.py archive-session` and
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
