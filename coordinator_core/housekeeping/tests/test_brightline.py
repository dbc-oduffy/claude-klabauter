"""
coordinator_core.housekeeping.tests.test_brightline — C7: the whole-cycle
brightline gate, gate-clearing case, process time and spawn count.

Cite (BINDING): docs/plans/2026-08-29-the-housekeeping-cycle-stops-
committing.md, prime_exit_criterion + § C7 task body; DR-344 (the 500ms
brightline this plan's own 200ms cycle criterion sits inside with 2.5x
headroom); `coordinator_core/benchmarks/tests/test_archival_commit_process_
budget.py` (the landed, independently-measured budget guard for the one
git spawn `archive_and_commit`'s path still issues, whose
`GIT_SPAWN_COUNT_TOTAL_RATCHET` this file imports rather than hand-copies).

This is the PRIME exit criterion as a test, on the GATE-CLEARING case — the
easy idle cycle (nothing to close, nothing to archive) proves nothing about
the job this cycle exists to do and is deliberately not what this file
measures. Fixture: `coordinator_core.housekeeping.tests.corpus_fixture.
build_corpus`, the real corpus's own shape (~250 live records, ~1,470
archived records both month-nested and at the archive root, >=17
`awaiting_gate` records of which exactly one is wired to clear) — a budget
measured against a toy fixture would go green and mean nothing, per that
module's own docstring.

Three axes asserted, never duration alone (this chunk's own brief):
  - process time (`time.process_time`, parent CPU only) <= 200ms
  - git spawn count <= `GIT_SPAWN_COUNT_TOTAL_RATCHET` (imported from the
    landed benchmark, never a copied number)
  - read counts: the live corpus read exactly once per record per cycle
    (`result["live_read_count"] == corpus_fixture.TOTAL_LIVE`), and the
    archive candidate index's `lookup()` never resolves to a wholesale
    re-read of the archive (this fixture's one clearing gate's blocker is a
    LIVE terminal record, so every `awaiting_gate` record's resolution
    costs zero archive file reads — proven directly, not assumed, via a
    counting wrapper over `ArchiveIndex.lookup`).

Windows tick-quantisation (this chunk's own brief): `time.process_time()`
quantises at ~15.625ms, so a single-shot reading near the 200ms budget is
~13 quanta and too coarse alone to trust. This file runs `N_OUTER`
INDEPENDENT full-shape repetitions — each its own fresh `build_corpus` +
git-init + baseline commit, because the gate-clearing/archival cycle is not
idempotent (a second run against the same corpus has nothing left to clear
or archive) — and reads the MEDIAN across them, with the rep count recorded
in the assertion message. `N_OUTER` is 3, not the 5 several sibling budget
files use: each rep here pays a full ~1,720-file corpus build plus a real
git commit, and this file is `cadence`-marked (not part of the fast tier)
specifically because that cost is real and should not be paid on every run.

Each per-leg budget from C3 (`corpus.LEG_BUDGET_MS`), C4 (revalidate's own
budget, `test_archive_index.py`), C5 (`resolve.LEG_BUDGET_MS`), and C6
(`gate_clear.LOCK_BUDGET_MS`) is asserted INDEPENDENTLY in its own chunk's
test module and stays asserted there — this file does not re-measure any
leg in isolation, and does not touch those modules' own test files. What
this file adds is the one thing no per-leg test can prove alone: the
ASSEMBLED cycle, all legs paid in the same call, on the same real-shaped
fixture, gate genuinely clearing.

Negative-spec: this file does not test the idle/no-op cycle (a separate,
easier case this criterion is not about), does not re-test `archive_and_
commit`'s own mechanics (rollback, CAS, mode preservation —
`ops/fleet/tests/`'s job), and does not re-derive
`GIT_SPAWN_COUNT_TOTAL_RATCHET` — a copied number here is exactly the
regression this chunk's own brief warns against ("a copied number ...
every hand-copied budget in this workstream's history has gone stale
within a day").
"""

from __future__ import annotations

import asyncio
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest

from coordinator_core.benchmarks.tests.test_archival_commit_process_budget import (
    GIT_SPAWN_COUNT_TOTAL_RATCHET,
)
from coordinator_core.git.argv_batch import _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS
from coordinator_core.housekeeping import archive_index as archive_index_mod
from coordinator_core.housekeeping import cycle
from coordinator_core.housekeeping.tests.corpus_fixture import (
    CorpusFixture,
    LIVE_STATE_COUNTS,
    MEMO_TERMINAL_COUNT,
    TERMINAL_STATES,
    TOTAL_LIVE,
    build_corpus,
    build_memo_overflow_corpus,
)
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

#: The prime exit criterion's own number (plan frontmatter + § C7 body) —
#: DR-344's 500ms brightline is the PM-ratified bar; this is the tighter,
#: EM-restated cycle budget that sits inside it with 2.5x headroom.
CYCLE_PROCESS_TIME_BUDGET_MS = 200.0

#: See module docstring's quantisation section for why 3, not 5.
N_OUTER = 3

#: Comfortably above the ~4 already-terminal live records this fixture's
#: own LIVE_STATE_COUNTS distribution produces (closed=1, continued=1,
#: shipped=2) — cap is never the binding constraint here; the point is that
#: whatever qualifies gets archived, not that the cap is exercised.
CAP = 50


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "brightline-test@example.invalid")
    _git(root, "config", "user.name", "brightline-test")
    return root


def _build_and_commit_fixture(
    root: Path, seed: int, **build_corpus_kwargs: Any
) -> Tuple[Path, CorpusFixture]:
    """Fixture SETUP, excluded from every measured figure below: a fresh
    real-shaped corpus (`build_corpus`) committed as the repo's baseline,
    exactly mirroring `test_cycle.py`'s own `repo` fixture shape but at the
    plan's own real scale rather than a 5-record toy.

    Returns `(repo, fixture)` -- the `CorpusFixture` manifest (2026-08-30,
    the actioned-memo class gets an occasion, C3) so a caller can assert
    against the memo family's own records without re-scanning the corpus.
    """
    repo = _init_repo(root)
    fixture = build_corpus(repo, seed=seed, **build_corpus_kwargs)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "brightline fixture baseline")

    # Leave behind the archive candidate-index cache that ANY previous cycle
    # on this checkout would have left, so the measured figure is the
    # recurring cycle rather than a checkout's one-time initialisation.
    #
    # This is setup, not a thumb on the scale: a full `build_index` costs
    # 171.9ms at 1,470 records and is paid ONCE per checkout, ever, whereas
    # the budget governs a job that runs on a cadence. Measuring only the
    # cold pass would report a number the job produces once and never again.
    # The cold cost is real and is asserted separately by
    # `test_cold_first_build_is_reported_not_hidden` below.
    common_dir = git_common_dir(repo)
    worktree_root = main_worktree_root(common_dir)
    warm = archive_index_mod.build_index(worktree_root / "archive" / "handoffs")
    assert archive_index_mod.save_index(
        warm, archive_index_mod.cache_path_for(common_dir)
    ), "fixture could not pre-warm the index cache"
    return repo, fixture


def _run_one_cycle(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, cap: int = CAP
) -> Dict[str, Any]:
    """Run ONE gate-clearing cycle, bracketing `time.process_time()` and
    counting real git spawns around `cycle.run()` ONLY — fixture
    construction and the baseline commit above are excluded from both
    figures, matching this repo's own convention
    (`test_archival_commit_process_budget.py`'s "fixture setup, not the
    measured region").

    No live claim holders in this fixture (`corpus_fixture.build_corpus`
    writes none), so `cs_claim_holder_live` is stubbed to always-False
    rather than reaching for a claim-registry file that was never written —
    mirrors `test_cycle.py`'s own conflict-arm stub.

    Git spawn counting reuses this repo's own established convention
    (`coordinator_core/tests/oracles/test_archive_and_commit_spawn_floor.py
    :: _make_total_spawn_counter`): wrap the REAL
    `asyncio.create_subprocess_exec` and count every call, since every git
    invocation on this path goes through it (DR-211 D4's async mandate —
    `archive_and_commit`'s own module docstring: "NEVER uses blocking
    subprocess.run"). `COORDINATOR_AUTO_PUSH_SYNC` is deliberately left
    UNSET (production default): the post-commit auto-push hook detaches
    into its own child process and is therefore outside this process's own
    `asyncio.create_subprocess_exec` calls, which is exactly why the
    measured count below reads 1 (the shared-index resync's `git restore
    --staged`) rather than folding in the push subsystem's own spawns —
    matching `GIT_SPAWN_COUNT_TOTAL_RATCHET`'s own documented invariant
    ("archive_and_commit issues ZERO git spawns of its own; the path
    observably spawns exactly ONE real git process ... git restore
    --staged").
    """
    monkeypatch.setattr(cycle, "cs_claim_holder_live", lambda claim_path: False)

    spawn_total = [0]
    real_spawn = asyncio.create_subprocess_exec

    async def _counting_spawn(*argv, **kwargs):
        spawn_total[0] += 1
        return await real_spawn(*argv, **kwargs)

    lookup_candidate_counts: List[int] = []
    real_lookup = archive_index_mod.ArchiveIndex.lookup

    def _counting_lookup(self, handoff_id):
        candidates = real_lookup(self, handoff_id)
        lookup_candidate_counts.append(len(candidates))
        return candidates

    monkeypatch.setattr(archive_index_mod.ArchiveIndex, "lookup", _counting_lookup)

    t0 = time.process_time()
    with patch("asyncio.create_subprocess_exec", side_effect=_counting_spawn):
        result = cycle.run(str(repo), cap=cap)
    elapsed_ms = (time.process_time() - t0) * 1000.0

    result = dict(result)
    result["_process_time_ms"] = elapsed_ms
    result["_git_spawns"] = spawn_total[0]
    result["_archive_lookup_calls"] = len(lookup_candidate_counts)
    result["_archive_candidate_reads"] = sum(lookup_candidate_counts)
    return result


def test_brightline_gate_clears_and_archives_within_budget(tmp_path_factory, monkeypatch):
    """The prime exit criterion, on the gate-clearing case, over N_OUTER
    independent real-shaped reps (module docstring's quantisation
    section)."""
    samples_ms: List[float] = []
    spawn_counts: List[int] = []
    live_read_counts: List[int] = []
    archive_lookup_calls: List[int] = []
    archive_candidate_reads: List[int] = []
    closed_counts: List[int] = []
    archived_lists: List[list] = []

    for rep in range(N_OUTER):
        root = tmp_path_factory.mktemp(f"brightline_{rep}")
        repo, fixture = _build_and_commit_fixture(root, seed=20260829 + rep)

        result = _run_one_cycle(repo, monkeypatch)

        assert result["closed"] >= 1, (
            f"rep {rep}: gate-clearing case did not clear a gate -- this is the "
            f"prime exit criterion's own case (module docstring), not the easier "
            f"idle one. result={result!r}"
        )
        assert result["archived"], (
            f"rep {rep}: no terminal handoff archived -- the criterion names a "
            f"cycle that 'clears a gate AND archives a handoff'. result={result!r}"
        )
        assert result["conflicts"] == [], f"rep {rep}: unexpected gate-clear CONFLICT: {result!r}"
        assert result["failed"] == [], f"rep {rep}: archival failure(s): {result!r}"
        assert result["live_read_count"] == TOTAL_LIVE, (
            f"rep {rep}: live corpus read {result['live_read_count']} times this "
            f"cycle, expected exactly TOTAL_LIVE={TOTAL_LIVE} (one read per live "
            f"record, per cycle, per C3's own contract) -- a re-scan/re-read "
            f"regression. result={result!r}"
        )
        # C3 (2026-08-30, the actioned-memo class gets an occasion): the ONE
        # assertion this existing test could not already make -- that a memo
        # actually moved, and the negative control (a non-terminal memo)
        # stayed. Everything else (spawn count, process time) is already
        # covered below over the SAME fixture, now that it carries a memo
        # corpus too -- a memo-leg dirty-check regression already fails
        # `max_spawns <= GIT_SPAWN_COUNT_TOTAL_RATCHET` below with zero new
        # test code (this chunk's own brief).
        assert len(result["memos_archived"]) == MEMO_TERMINAL_COUNT, (
            f"rep {rep}: expected all {MEMO_TERMINAL_COUNT} clean fixture memos "
            f"archived (cap={CAP} comfortably exceeds the fixture's memo count): "
            f"result={result!r}"
        )
        assert result["memos_failed"] == [], f"rep {rep}: memo archival failure(s): {result!r}"
        for noise in fixture.memo_noise_records:
            assert noise["path"].exists(), (
                f"rep {rep}: non-terminal (status: open) memo noise control "
                f"{noise['rel_name']!r} must be retained in the inbox, never "
                f"archived: result={result!r}"
            )

        samples_ms.append(result["_process_time_ms"])
        spawn_counts.append(result["_git_spawns"])
        live_read_counts.append(result["live_read_count"])
        archive_lookup_calls.append(result["_archive_lookup_calls"])
        archive_candidate_reads.append(result["_archive_candidate_reads"])
        closed_counts.append(result["closed"])
        archived_lists.append(result["archived"])

    median_ms = statistics.median(samples_ms)
    max_spawns = max(spawn_counts)
    total_archive_candidate_reads = sum(archive_candidate_reads)

    detail = (
        f"C7 brightline gate, {N_OUTER} independent full-shape reps "
        f"(~{TOTAL_LIVE} live / ~1,470 archived records each, gate genuinely "
        f"clears every rep): "
        f"process_time_ms samples={samples_ms}, median={median_ms}ms "
        f"(budget {CYCLE_PROCESS_TIME_BUDGET_MS}ms). "
        f"git spawn counts (asyncio.create_subprocess_exec calls, this process "
        f"only, production async-detached auto-push left unforced)={spawn_counts}, "
        f"max={max_spawns} (ratchet GIT_SPAWN_COUNT_TOTAL_RATCHET="
        f"{GIT_SPAWN_COUNT_TOTAL_RATCHET}, imported from "
        f"coordinator_core.benchmarks.tests.test_archival_commit_process_budget). "
        f"live_read_count per rep={live_read_counts} (each must equal "
        f"TOTAL_LIVE={TOTAL_LIVE}). "
        f"ArchiveIndex.lookup() calls per rep={archive_lookup_calls} (one per "
        f"awaiting_gate record's resolution attempt); candidate reads returned per "
        f"rep={archive_candidate_reads}, total={total_archive_candidate_reads} -- "
        f"this fixture's one clearing gate's blocker is a LIVE terminal record, so "
        f"every lookup here resolves to zero archive candidates, never the ~1,470-"
        f"file archive itself (contract: 'the archive read only for resolved "
        f"candidates, never wholesale'). closed per rep={closed_counts}; archived "
        f"per rep={archived_lists}."
    )

    assert total_archive_candidate_reads == 0, (
        f"archive candidate resolution read {total_archive_candidate_reads} file(s) "
        f"across all reps -- expected exactly 0 for this fixture (the one clearing "
        f"gate's blocker is a live record, never archived), and in any case never "
        f"anywhere close to the ~1,470-file archive (a wholesale re-read regression "
        f"would show up here as a count in the hundreds or thousands). {detail}"
    )
    assert median_ms <= CYCLE_PROCESS_TIME_BUDGET_MS, (
        f"whole-cycle process time exceeded the {CYCLE_PROCESS_TIME_BUDGET_MS}ms "
        f"brightline (this plan's own prime exit criterion, inside DR-344's 500ms "
        f"PM-ratified bar). Per this chunk's own instruction: if this budget turns "
        f"out to be wrong, restate it out loud with the measurement and take the "
        f"consequence -- never absorb it into a total quietly. {detail}"
    )
    assert max_spawns <= GIT_SPAWN_COUNT_TOTAL_RATCHET, (
        f"whole-cycle git spawn count exceeded GIT_SPAWN_COUNT_TOTAL_RATCHET="
        f"{GIT_SPAWN_COUNT_TOTAL_RATCHET} (imported, never hand-copied, from "
        f"coordinator_core.benchmarks.tests.test_archival_commit_process_budget) -- "
        f"a second git spawn entered the cycle. {detail}"
    )
    print(detail)


def test_cap_applies_independently_per_family_not_over_the_union(tmp_path_factory, monkeypatch):
    """CAP FIXTURE (staff-eng Finding 5, superseded by overengineering-
    reviewer Finding 1, EM-adjudicated): each family is capped
    independently, by its own existing planner (`compute_terminal_set` for
    handoffs, `plan_sweep` for memos), never a shared cap over the union. A
    fixture exceeding `cap` in BOTH families combined must still archive up
    to `cap` from EACH family, not `cap` total split between them.

    A single rep, not part of the N_OUTER budget loop above -- this asserts
    a functional property (which items got archived), not process time or
    spawn count.
    """
    root = tmp_path_factory.mktemp("brightline_cap")
    repo, fixture = _build_and_commit_fixture(root, seed=20260830)

    small_cap = 2
    # Sanity: the default fixture shape already exceeds small_cap in BOTH
    # families -- LIVE_STATE_COUNTS's own terminal live records (shipped=2,
    # closed=1, continued=1 == 4) and MEMO_TERMINAL_COUNT=5 actioned memos --
    # so "archived == cap" below is a genuine cap-slot, never a vacuous count
    # that just happens to equal the corpus size.
    assert MEMO_TERMINAL_COUNT > small_cap
    terminal_live_count = sum(
        count for state, count in LIVE_STATE_COUNTS.items() if state in TERMINAL_STATES
    )
    assert terminal_live_count > small_cap

    result = _run_one_cycle(repo, monkeypatch, cap=small_cap)

    assert len(result["archived"]) == small_cap, (
        f"handoff family should archive exactly cap={small_cap} despite "
        f"{terminal_live_count} terminal candidates existing -- its own "
        f"planner (compute_terminal_set) owns this cap slot: result={result!r}"
    )
    assert len(result["memos_archived"]) == small_cap, (
        f"memo family should ALSO archive exactly cap={small_cap} -- each "
        f"family is capped independently by its own planner (plan_sweep), "
        f"never a shared cap over the union (a shared cap would give this "
        f"family fewer than {small_cap} since the handoff family took its "
        f"share first): result={result!r}"
    )


def test_memo_overflow_corpus_survives_the_argv_budget_and_dirty_memo_is_retained(
    tmp_path_factory, monkeypatch
):
    """OVERFLOW FIXTURE (staff-eng Finding 3, major): a memo corpus large
    enough to overflow `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` (imported from
    `coordinator_core.git.argv_batch`, never hand-copied) exercises the
    branch C2's generalised `fallback_pathspecs` fix changes behaviour on.
    One survivor is worktree-dirty and must be retained; `git_spawns` must
    stay at exactly 1, unchanged from the non-overflow case.
    """
    root = tmp_path_factory.mktemp("brightline_overflow")
    repo, fixture = _build_and_commit_fixture(root, seed=20260831)

    overflow_records = build_memo_overflow_corpus(fixture.memo_inbox_dir, count=80)
    total_chars = sum(len(r["rel_name"]) + 1 for r in overflow_records)
    assert total_chars > _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS, (
        f"overflow fixture must exceed the argv budget to exercise the "
        f"branch under test: {total_chars} chars <= budget "
        f"{_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS}"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "overflow fixture: 80 more actioned memos")

    dirty_record = overflow_records[0]
    with dirty_record["path"].open("a", encoding="utf-8") as fh:
        fh.write("\nuncommitted dirty edit\n")
    dirty_rel = f"cross-repo/inbox/{dirty_record['rel_name']}"

    result = _run_one_cycle(repo, monkeypatch, cap=CAP)

    assert dirty_rel not in result["memos_archived"], (
        f"the worktree-dirty overflow memo must be retained, never archived: "
        f"result={result!r}"
    )
    assert dirty_record["path"].exists(), "dirty memo must still be present in the inbox on disk"
    assert result["_git_spawns"] == 1, (
        f"the overflow branch must still spawn exactly 1 git process (the "
        f"inherited main-index resync), unchanged from the non-overflow "
        f"case -- a second spawn here means the generalised "
        f"fallback_pathspecs fix regressed: result={result!r}"
    )


def test_warm_cycle_uses_the_cache_and_cold_first_build_is_reported_not_hidden(tmp_path):
    """The budget above is measured warm, so the cold cost must be stated
    somewhere or it disappears from the record entirely.

    Two facts, both asserted: the measured cycle really is using the cache
    (`index_rebuilt is False` -- otherwise the budget test above would be
    silently measuring a rebuild and passing for the wrong reason), and the
    cold first build on a fresh checkout costs what it costs. The cold
    figure is NOT budgeted: it is a once-per-checkout initialisation, not
    the recurring cycle DR-344 and this plan's criterion govern.
    """
    cold_repo = _init_repo(tmp_path / "cold")
    build_corpus(cold_repo, seed=9001)
    _git(cold_repo, "add", "-A")
    _git(cold_repo, "commit", "-q", "-m", "cold baseline")

    t0 = time.process_time()
    cold = cycle.run(str(cold_repo), cap=CAP)
    cold_ms = (time.process_time() - t0) * 1000.0
    assert cold["index_rebuilt"] is True, "a fresh checkout must pay the full build"
    assert cold["index_cache_written"] is True, "and must leave a cache behind"

    t0 = time.process_time()
    warm = cycle.run(str(cold_repo), cap=CAP)
    warm_ms = (time.process_time() - t0) * 1000.0
    assert warm["index_rebuilt"] is False, (
        "the second cycle on the same checkout must reuse the cache -- if this "
        "flips to True the persistence layer has silently stopped working and "
        "every cycle is paying the full archive walk again"
    )
    assert warm_ms < cold_ms, (
        f"the warm cycle must be cheaper than the cold one "
        f"(cold={cold_ms:.1f}ms, warm={warm_ms:.1f}ms)"
    )
