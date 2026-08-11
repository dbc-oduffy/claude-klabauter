"""
coordinator_core.tests.test_coverage_reviewed_set — regression test for the
build_reviewed_set per-record-union correctness fix.

Failure shape: the old batched git rev-list fast-path
    git rev-list A^..A B^..B C^..C ...
resolves positives={A,B,C,...} and negatives={A^,B^,C^,...} together, then
returns reachable(positives) \\ reachable(negatives). For a linear chain each
older commit's parent is a negative → it is excluded. Only the newest tip
survives. The per-record loop unions each range independently and returns all
reviewed SHAs correctly.

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C3
Bug fix: build_reviewed_set batched git rev-list discarded all but newest SHA
         on interleaved single-commit sha_range records (coverage.py Phase 2).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List

import pytest


# ---------------------------------------------------------------------------
# Git repo helper (mirrors pattern in test_lifecycle_worktree.py)
# ---------------------------------------------------------------------------

def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )


def _make_commit(repo: Path, message: str) -> str:
    """Make an empty commit in repo and return its full SHA."""
    _git(["commit", "--allow-empty", "-m", message], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialise a fresh git repo with required identity config."""
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


# Review: code-reviewer (Finding 9) — module-level helper collapses the
# 8-line nested closure duplicated verbatim across seven chain/session-scope
# tests; matches this file's existing module-level-helper idiom.
def _commit_for_session(repo: Path, message: str, session_id: str) -> str:
    """Make an empty commit stamped with the given Session-Id trailer."""
    _git(
        ["commit", "--allow-empty", "-m", f"{message}\n\nSession-Id: {session_id}"],
        repo,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Trail record helper
# ---------------------------------------------------------------------------

def _write_trail_record(path: Path, sha: str) -> None:
    """Write a minimal trail record JSON with a single-commit sha_range for sha.

    Schema mirrors real trail records (state/review-trail/*.json):
        sha_range  — "<sha>^..<sha>" (single-commit diff range)
        scope_kind — "diff" (so Phase 1 lets it through; not plan/integration)
        verdict    — "ok" (non-pending; _verdict_counts returns True)

    The sha_range format "<sha>^..<sha>" passes SAFE_RANGE and resolves via
    git rev-list to exactly {sha} when evaluated per-record.
    """
    record = {
        "sha_range": f"{sha}^..{sha}",
        "reviewer": "code-reviewer",
        "scope": "session",
        "scope_kind": "diff",
        "verdict": "ok",
        "diff_loc": 1,
        "session_id": "00000000-0000-0000-0000-000000000001",
    }
    path.write_text(json.dumps(record), encoding="utf-8")


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------

def test_build_reviewed_set_interleaved_linear_chain(tmp_path: Path) -> None:
    """Per-record union returns all 6 SHAs; the old batched path returned only 1.

    Interleaved-chain failure shape (why batching is wrong):
        commits: C0 → C1 → C2 → C3 → C4 → C5 → C6  (linear, oldest→newest)
        trail records: one per commit C1..C6, each with sha_range = Ci^..Ci

    Old batched call:
        git rev-list C1^..C1 C2^..C2 C3^..C3 C4^..C4 C5^..C5 C6^..C6
        = reachable({C1..C6}) \\ reachable({C0..C5})
        = {C6}   ← only the newest tip; every older commit is excluded
                    because its SHA appears as a negative (parent of a later tip)

    Correct per-record union:
        git rev-list Ci^..Ci  →  {Ci}  (for each i in 1..6)
        union = {C1, C2, C3, C4, C5, C6}   ← all 6 SHAs

    This test MUST fail against the old batched implementation and pass against
    the per-record-union fix.
    """
    from coordinator_core.coverage import build_reviewed_set

    # --- 1. Build a temp git repo with an initial commit + 6 more commits ---
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # C0: initial empty commit (not part of any trail record)
    _make_commit(repo, "C0: initial")

    # C1..C6: commits whose SHAs we will assert are ALL in reviewed_set
    commit_shas: List[str] = []
    for i in range(1, 7):
        sha = _make_commit(repo, f"C{i}: work commit {i}")
        commit_shas.append(sha)

    assert len(commit_shas) == 6, "fixture must produce exactly 6 commit SHAs"

    # --- 2. Write 6 trail records, one per commit ---
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    trail_paths: List[str] = []
    for i, sha in enumerate(commit_shas):
        record_path = trail_dir / f"record_{i:02d}.json"
        _write_trail_record(record_path, sha)
        trail_paths.append(str(record_path))

    # --- 3. Call build_reviewed_set and assert all 6 SHAs are returned ---
    reviewed = build_reviewed_set(
        trail_paths,
        on_record_error="fail",
        intersect_shas=None,
        repo_root=str(repo),
    )

    missing = set(commit_shas) - reviewed
    assert not missing, (
        f"build_reviewed_set missed {len(missing)} SHA(s) from the linear chain: "
        f"{missing!r}. "
        f"Expected all {len(commit_shas)} SHAs to be reviewed; got {len(reviewed)}. "
        "If exactly 1 SHA was returned (the newest), the old batched fast-path is active."
    )
    assert reviewed == set(commit_shas), (
        f"reviewed_set contains unexpected SHAs: {reviewed - set(commit_shas)!r}"
    )


def test_parse_trail_file_reads_suffixed_filename(tmp_path: Path) -> None:
    """``_parse_trail_file`` (coverage.py) round-trips a ``-2``-suffixed filename.

    ``_parse_trail_file`` parses purely by file *content*, never by filename, so
    this pins that the DR-216 same-second-collision fix's uniquifying suffix
    (``review_trail_write.py::_reserve_unique_trail_path``, e.g.
    ``2026-07-27-140000-abc12345-2.json``) is fully transparent to this consumer.
    """
    from coordinator_core.coverage import _parse_trail_file

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    sha = _make_commit(repo, "C1: work")

    suffixed_path = tmp_path / "2026-07-27-140000-abc12345-2.json"
    _write_trail_record(suffixed_path, sha)

    records = _parse_trail_file(str(suffixed_path))

    assert len(records) == 1
    assert records[0]["sha_range"] == f"{sha}^..{sha}"


def test_build_reviewed_set_pending_verdict_excluded(tmp_path: Path) -> None:
    """Trail records with verdict='pending' must NOT contribute to reviewed_set.

    Ensures _verdict_counts exclusion survives the per-record loop refactor.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    sha = _make_commit(repo, "C1: work")

    record = {
        "sha_range": f"{sha}^..{sha}",
        "scope_kind": "diff",
        "verdict": "pending",
    }
    record_path = tmp_path / "pending_record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="fail",
        repo_root=str(repo),
    )
    assert sha not in reviewed, (
        f"pending verdict must exclude SHA {sha!r} from reviewed_set"
    )


def test_build_reviewed_set_on_record_error_skip(tmp_path: Path) -> None:
    """on_record_error='skip' silently skips an unresolvable ref; valid records still counted."""
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    sha = _make_commit(repo, "C1: work")

    # Good record
    good = tmp_path / "good.json"
    _write_trail_record(good, sha)

    # Bad record — bogus SHA that git rev-list cannot resolve
    bad_record = {
        "sha_range": "deadbeef00000000000000000000000000000000^..deadbeef00000000000000000000000000000000",
        "scope_kind": "diff",
        "verdict": "ok",
    }
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_record), encoding="utf-8")

    reviewed = build_reviewed_set(
        [str(bad), str(good)],
        on_record_error="skip",
        repo_root=str(repo),
    )
    assert sha in reviewed, "valid record must still be counted when bad record is skipped"


def test_build_reviewed_set_on_record_error_fail(tmp_path: Path) -> None:
    """on_record_error='fail' raises RuntimeError for an unresolvable ref."""
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")

    bad_record = {
        "sha_range": "deadbeef00000000000000000000000000000000^..deadbeef00000000000000000000000000000000",
        "scope_kind": "diff",
        "verdict": "ok",
    }
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_record), encoding="utf-8")

    with pytest.raises(RuntimeError, match="git rev-list"):
        build_reviewed_set(
            [str(bad)],
            on_record_error="fail",
            repo_root=str(repo),
        )


# ---------------------------------------------------------------------------
# Single-graph-walk tests (durable perf lever — replaces the per-range fan-out
# with ONE `git rev-list --parents` build + in-memory set math). These promote
# the throwaway spike differential harness into durable regression tests.
# Spike verdict: state/handoffs/2026-07-15_082943_spike-result-coverage-gate-graph-walk.md
# ---------------------------------------------------------------------------

def _write_range_record(path: Path, sha_range: str, verdict: str = "ok") -> None:
    """Write a trail record citing an arbitrary sha_range (not just single-commit)."""
    record = {
        "sha_range": sha_range,
        "reviewer": "code-reviewer",
        "scope": "session",
        "scope_kind": "diff",
        "verdict": verdict,
        "session_id": "00000000-0000-0000-0000-000000000001",
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def _rev_list(sha_range: str, repo: Path) -> set:
    """git rev-list <range> → set of SHAs (ground-truth oracle)."""
    out = subprocess.run(
        ["git", "rev-list", sha_range],
        cwd=str(repo), capture_output=True, encoding="utf-8", check=True,
    ).stdout
    return {s.strip() for s in out.splitlines() if s.strip()}


def _rev_list_no_merges(sha_range: str, repo: Path) -> set:
    """git rev-list --no-merges <range> → set (mirrors flat-mode chain_set build).

    (Review: code-reviewer — Finding 9, moved up beside _rev_list; was previously
    defined at the bottom of the file, after all four call sites.)
    """
    out = subprocess.run(
        ["git", "rev-list", "--no-merges", sha_range],
        cwd=str(repo), capture_output=True, encoding="utf-8", check=True,
    ).stdout
    return {s.strip() for s in out.splitlines() if s.strip()}


def _make_merge_topology(repo: Path) -> dict:
    """Build a repo with a merge + second-parent branch + linear tail.

        P0 - C0 - C1 - C2 --------- C3 ---- M - C4   (main)
                          \\                 /
                           S1 - S2 --------- (side, second parent of M)

    Returns {name: full_sha}. chain window is typically C0..HEAD (P0 out-of-window).
    """
    _init_repo(repo)
    shas: dict = {}
    shas["P0"] = _make_commit(repo, "P0: pre-history (out-of-window base)")
    shas["C0"] = _make_commit(repo, "C0: window base")
    shas["C1"] = _make_commit(repo, "C1")
    shas["C2"] = _make_commit(repo, "C2")
    # side branch off C2
    _git(["checkout", "-b", "side", shas["C2"]], repo)
    shas["S1"] = _make_commit(repo, "S1")
    shas["S2"] = _make_commit(repo, "S2")
    # back to main, one more commit, then merge side in (no-ff → real merge commit)
    _git(["checkout", "main"], repo)
    shas["C3"] = _make_commit(repo, "C3")
    _git(["merge", "--no-ff", "-m", "M: merge side", "side"], repo)
    shas["M"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()
    shas["C4"] = _make_commit(repo, "C4")
    return shas


@pytest.fixture
def merge_topology(tmp_path: Path):
    """(repo, shas) built by _make_merge_topology, as a shared pytest fixture.

    (Review: code-reviewer — Finding 10, de-dups the repeated `repo = tmp_path /
    "repo"; repo.mkdir(); s = _make_merge_topology(repo)` boilerplate that grew to
    8 call sites across the graph-walk test corpus.)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, _make_merge_topology(repo)


def test_build_reviewed_set_graphwalk_matches_fanout_differential(tmp_path: Path, merge_topology) -> None:
    """DIFFERENTIAL: cross-checks the two strategies for consistency across a rich
    topology (merges, second-parent branches, out-of-window tips, ^/~N/^N suffixes,
    symmetric ...). By construction `new == old` can only prove the two
    implementations AGREE — it would pass even if both shared a latent bug. The
    actual ground-truth pin against an independently-computed oracle lives in
    test_build_reviewed_set_graphwalk_merge_reachability (`_rev_list(...) & chain_set`,
    a separate `git rev-list` call, not build_reviewed_set output). Promoted durable
    from the spike — exercises the REAL in-repo code paths.
    (Review: code-reviewer — Finding 5, reworded from "the spike's core correctness
    proof" per the audit sidecar.)
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology

    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    ranges = [
        f"{s['C1']}^..{s['C4']}",   # linear multi spanning the merge
        f"{s['S1']}^..{s['S2']}",   # second-parent branch commits
        f"{s['M']}^..{s['M']}",     # merge single-range (M^ == C3, first parent)
        f"{s['M']}^2..{s['S2']}",   # ^N second-parent suffix
        f"{s['C2']}~1..{s['C3']}",  # ~N suffix (C2~1 == C1)
        f"{s['P0']}^..{s['C0']}",   # fully out-of-window (below the chain base)
        f"{s['C1']}...{s['S2']}",   # symmetric-difference range
    ]
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    paths = []
    for i, sr in enumerate(ranges):
        p = trail_dir / f"rec_{i:02d}.json"
        _write_range_record(p, sr)
        paths.append(str(p))

    old = build_reviewed_set(
        paths, on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )  # no graph_range → per-range fan-out
    new = build_reviewed_set(
        paths, on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )  # graph_range → single-graph-walk

    assert new == old, (
        f"graph-walk diverged from fan-out.\n"
        f"  fan-out-only: {sorted(old - new)}\n"
        f"  graph-only:   {sorted(new - old)}"
    )
    # Guard against a trivially-empty pass — the corpus must actually cover commits.
    assert old, "differential corpus produced an empty reviewed set — fixture is not exercising the code"
    assert old <= chain_set, "reviewed_set must be a subset of chain_set (intersect semantics)"


def test_build_reviewed_set_graphwalk_merge_reachability(tmp_path: Path, merge_topology) -> None:
    """Merge-commit reachability: a merge single-range (M^..M) must include the
    second-parent branch commits (S1, S2), matching `git rev-list M^..M`. A first-parent
    -only walk would miss them — this pins the all-parents BFS.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    p = tmp_path / "merge_rec.json"
    _write_range_record(p, f"{s['M']}^..{s['M']}")

    reviewed = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    # M itself is a merge → excluded from chain_set (--no-merges); S1/S2 are the
    # second-parent branch commits reachable from M but not from M^ (== C3).
    oracle = _rev_list(f"{s['M']}^..{s['M']}", repo) & chain_set
    assert reviewed == oracle, f"expected {sorted(oracle)}, got {sorted(reviewed)}"
    assert s["S1"] in reviewed and s["S2"] in reviewed, (
        "second-parent branch commits must be reached through the merge (all-parents BFS)"
    )


def test_build_reviewed_set_graphwalk_out_of_window_tip(tmp_path: Path, merge_topology) -> None:
    """A record whose range tip is below the chain window contributes nothing (its
    chain-intersection is provably ∅), while a valid in-window record is still counted.
    Confirms the out-of-window collapse that makes the in-window graph sufficient.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    out_of_window = tmp_path / "oow.json"
    _write_range_record(out_of_window, f"{s['P0']}^..{s['P0']}")  # entirely below window
    in_window = tmp_path / "inw.json"
    _write_range_record(in_window, f"{s['C1']}^..{s['C1']}")  # → {C1}

    reviewed = build_reviewed_set(
        [str(out_of_window), str(in_window)],
        on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    assert reviewed == {s["C1"]}, (
        f"out-of-window tip must contribute ∅; only in-window C1 expected. got {sorted(reviewed)}"
    )


def test_build_reviewed_set_graphwalk_out_of_window_spawn_budget(tmp_path: Path) -> None:
    """DURABLE REGRESSION for the 2026-07-28 spawn-count fix: a graph-walk corpus
    with MULTIPLE out-of-window endpoints (some sharing a base token, some distinct,
    one abbreviated) must stay within a small fixed git-spawn budget REGARDLESS of
    how many records cite out-of-window tokens — not grow linearly with them.

    Root cause this pins (see coverage.py's _OutOfWindowCache): before this fix,
    every out-of-window endpoint cost TWO uncached spawns (`git rev-parse` existence
    probe in _probe_out_of_window, `git merge-base --is-ancestor` classification in
    _classify_out_of_window) — a 46-record live corpus turned a "<=2 spawn" design
    target into 124 spawns. The fix: (1) memoize _resolve_base's result by base
    token; (2) classify ancestor-of-base-vs-foreign via ONE lazily-built
    `git rev-list <graph_base>` ancestor set tested by in-memory membership, not a
    `merge-base` spawn per candidate; (3) batch ALL out-of-window existence probes
    into one `git cat-file --batch-check` spawn via an upfront pre-scan, instead of
    one `git rev-parse` per distinct token.

    Uses scope=None (not "session"/"chain"/"workstream-close-auto") so the
    orthogonal, per-range, NOT-cacheable-across-ranges foreign-session-scope
    narrowing (_narrow_foreign_session_scope, C7 — deliberately un-batchable,
    out of this fix's scope) contributes zero spawns, isolating exactly the
    endpoint-resolution budget this fix targets.

    Expected fixed budget: 1 (`git rev-list --parents` parent-map build) + 1
    (`git cat-file --batch-check` existence pre-scan) + 1 (`git rev-list
    <graph_base>` lazy ancestor-of-base set) = 3 spawns total, however many
    records/tokens are involved.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    pa = _make_commit(repo, "PA: pre-history")
    pb = _make_commit(repo, "PB: pre-history")
    c0 = _make_commit(repo, "C0: window base")
    c1 = _make_commit(repo, "C1")
    c2 = _make_commit(repo, "C2")
    c3 = _make_commit(repo, "C3")

    graph_range = f"{c0}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    def _write_scopeless_range_record(path: Path, sha_range: str) -> None:
        record = {
            "sha_range": sha_range,
            "reviewer": "code-reviewer",
            "scope_kind": "diff",
            "verdict": "ok",
        }
        path.write_text(json.dumps(record), encoding="utf-8")

    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    records = [
        f"{pa}^..{c1}",        # out-of-window base PA (ancestor-of-base)
        f"{pb}^..{c2}",        # a DIFFERENT out-of-window base PB
        f"{pa}..{c3}",         # PA reused verbatim — must hit the resolved-token cache
        f"{pb[:10]}..{c1}",    # PB cited via a DIFFERENT (abbreviated) token string
    ]
    paths = []
    for i, sr in enumerate(records):
        p = trail_dir / f"rec_{i:02d}.json"
        _write_scopeless_range_record(p, sr)
        paths.append(str(p))

    import coordinator_core.coverage as cov_mod

    orig_run = cov_mod._run
    orig_batch = cov_mod._batch_check_hex_tokens
    spawn_count = {"n": 0}

    def _counting_run(cmd, cwd=None):
        spawn_count["n"] += 1
        return orig_run(cmd, cwd=cwd)

    def _counting_batch(tokens, cwd):
        spawn_count["n"] += 1
        return orig_batch(tokens, cwd)

    cov_mod._run = _counting_run
    cov_mod._batch_check_hex_tokens = _counting_batch
    try:
        walk = build_reviewed_set(
            paths, on_record_error="skip", intersect_shas=chain_set,
            repo_root=str(repo), graph_range=graph_range,
        )
    finally:
        cov_mod._run = orig_run
        cov_mod._batch_check_hex_tokens = orig_batch

    fanout = build_reviewed_set(
        paths, on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    assert walk == fanout, (
        f"graph-walk diverged from fan-out.\n"
        f"  fan-out-only: {sorted(fanout - walk)}\n  graph-only: {sorted(walk - fanout)}"
    )
    # Every range's BASE (left) endpoint is out-of-window/ancestor-of-base by
    # construction (contributes ∅); the tip (right) endpoints are legitimately
    # in-window, so the union covers the whole chain.
    assert walk == chain_set, (
        f"expected the union to cover the whole in-window chain {sorted(chain_set)}, "
        f"got {sorted(walk)}"
    )
    assert spawn_count["n"] <= 3, (
        f"graph-walk out-of-window resolution spawned {spawn_count['n']} git "
        f"processes for 4 records sharing/abbreviating 2 out-of-window tokens; "
        f"expected <=3 (parent-map build + batch existence probe + lazy "
        f"ancestor-of-base set) — spawn count must not scale with record count"
    )


def test_build_reviewed_set_graphwalk_foreign_session_spawn_budget(tmp_path: Path) -> None:
    """DURABLE REGRESSION for the 2026-07-28 foreign-session-narrowing spawn-count
    fix: a graph-walk corpus with MULTIPLE _FOREIGN_STRIPPED_SCOPES records —
    spanning several DISTINCT sha_ranges, some sharing a session_id and some
    not — must stay within a small fixed git-spawn budget REGARDLESS of how
    many distinct (sha_range, session_id) pairs are involved, not grow linearly
    with them.

    Root cause this pins: before this fix, `_narrow_foreign_session_scope` (a
    thin wrapper over `session_attribution.trailer_foreign_shas`) spawned its
    own `git log` per DISTINCT (sha_range, session_id) pair, memoized only on
    that exact pair — a real corpus with 60+ records citing mostly-distinct
    ranges turned a "<=2 spawn" design target into ~47 extra spawns. The fix:
    one bulk `git log --no-merges <graph_range>` walk up front
    (`session_attribution.bulk_trailer_session_map`), building a sha ->
    session_id map once, then priming `session_cache` for every
    (sha_range, session_id) pair `_narrow` will ask about via cheap in-memory
    set math — see `_reviewed_via_graph_walk`'s upfront batch pre-scan for the
    two-step equivalence argument (shas-subset-of-window, then
    range-restriction-is-redundant-once-intersected-with-shas).

    Expected fixed budget: 1 (`git rev-list --parents` parent-map build) + 1
    (bulk `git log` trailer walk) = 2 spawns total, however many distinct
    ranges/session_ids the corpus's _FOREIGN_STRIPPED_SCOPES records cite.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    session_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    session_c = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    session_foreign = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    c0 = _make_commit(repo, "C0: window base")
    b1 = _commit_for_session(repo, "B1: session B's first segment start", session_b)
    foreign_1 = _commit_for_session(repo, "F1: interleaved foreign commit", session_foreign)
    b2 = _commit_for_session(repo, "B2: session B's first segment end", session_b)
    b3 = _commit_for_session(repo, "B3: session B's second (DISTINCT range) segment start", session_b)
    foreign_2 = _commit_for_session(repo, "F2: a SECOND interleaved foreign commit", session_foreign)
    b4 = _commit_for_session(repo, "B4: session B's second segment end", session_b)
    c1 = _commit_for_session(repo, "C1: session C's chain-scope segment start", session_c)
    foreign_3 = _commit_for_session(repo, "F3: a THIRD interleaved foreign commit", session_foreign)
    c2 = _commit_for_session(repo, "C2: session C's chain-scope segment end", session_c)

    graph_range = f"{c0}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    def _write_scoped_record(path: Path, sha_range: str, scope: str, session_id: str) -> None:
        record = {
            "sha_range": sha_range,
            "reviewer": "code-reviewer",
            "scope": scope,
            "scope_kind": "diff",
            "verdict": "ok",
            "session_id": session_id,
        }
        path.write_text(json.dumps(record), encoding="utf-8")

    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    records = [
        (f"{b1}..{b2}", "session", session_b),
        (f"{b3}..{b4}", "session", session_b),  # DISTINCT range, SAME session_id
        (f"{c1}..{c2}", "chain", session_c),    # DISTINCT session_id, different scope
    ]
    paths = []
    for i, (sr, scope, sid) in enumerate(records):
        p = trail_dir / f"rec_{i:02d}.json"
        _write_scoped_record(p, sr, scope, sid)
        paths.append(str(p))

    import coordinator_core.coverage as cov_mod

    orig_run = cov_mod._run
    spawn_count = {"n": 0}

    def _counting_run(cmd, cwd=None):
        spawn_count["n"] += 1
        return orig_run(cmd, cwd=cwd)

    cov_mod._run = _counting_run
    try:
        walk = build_reviewed_set(
            paths, on_record_error="skip", intersect_shas=chain_set,
            repo_root=str(repo), graph_range=graph_range,
        )
    finally:
        cov_mod._run = orig_run

    fanout = build_reviewed_set(
        paths, on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    assert walk == fanout, (
        f"graph-walk diverged from fan-out.\n"
        f"  fan-out-only: {sorted(fanout - walk)}\n  graph-only: {sorted(walk - fanout)}"
    )
    for foreign_sha in (foreign_1, foreign_2, foreign_3):
        assert foreign_sha not in walk, (
            f"interleaved foreign commit {foreign_sha} must be excluded from every "
            f"_FOREIGN_STRIPPED_SCOPES record's credited set, regardless of which "
            f"distinct sha_range/session_id observed it"
        )
    # Note: `..` excludes its LEFT endpoint (b1, b3, c1 are each a range's own
    # base and so are never IN their own range) — only each range's right
    # endpoint is asserted here; that's ordinary range semantics, unrelated to
    # foreign-session narrowing.
    for own_sha in (b2, b4, c2):
        assert own_sha in walk, (
            f"a session's OWN attributed commit {own_sha} must remain fully credited"
        )
    assert spawn_count["n"] <= 2, (
        f"graph-walk foreign-session narrowing spawned {spawn_count['n']} git "
        f"processes for 3 records spanning 3 distinct (sha_range, session_id) "
        f"pairs across 2 distinct session_ids; expected <=2 (parent-map build + "
        f"bulk trailer walk) — spawn count must not scale with record/range count"
    )


def test_build_reviewed_set_graphwalk_symbolic_and_abbrev_endpoints(tmp_path: Path, merge_topology) -> None:
    """REGRESSION: real trail records cite ranges with ABBREVIATED SHAs and a
    symbolic-branch tip (e.g. `<sha>..main`) — not just full-SHA endpoints. The
    symbolic endpoint (and any abbreviated ref) must resolve to its concrete
    in-window SHA, NOT be mistaken for out-of-window and collapsed to ∅. This
    bug produced a false UNCOVERED on the live gate and was invisible to a
    full-SHA-only differential corpus.

    Uses branch name "main" (not "HEAD") as the symbolic-ref stand-in: a
    stored literal "HEAD" is a DIFFERENT, later-fixed defect (sha_range
    false-COVERED — state/improvement-queue/2026-06-30-review-coverage-gate-
    false-covered-on-tr.yaml) that Phase 1 classification now excludes
    trail records for entirely, regardless of resolution correctness. "main"
    exercises the identical _resolve_base/_resolve_endpoint symbolic-ref code
    path (shared with HEAD resolution for the graph_range CLI argument, which
    legitimately still uses HEAD) without hitting that exclusion.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    ranges = [
        f"{s['C1'][:8]}..main",        # abbreviated base + symbolic branch tip
        f"{s['P0']}..main",            # out-of-window base + branch tip → covers whole window
        "main~2..main",                # symbolic base with ~N suffix
        f"{s['S1'][:7]}^..{s['S2'][:7]}",  # abbreviated both sides + ^ suffix
    ]
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    paths = []
    for i, sr in enumerate(ranges):
        p = trail_dir / f"rec_{i:02d}.json"
        _write_range_record(p, sr)
        paths.append(str(p))

    old = build_reviewed_set(
        paths, on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    new = build_reviewed_set(
        paths, on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    assert new == old, (
        f"symbolic/abbrev endpoints diverged.\n"
        f"  fan-out-only: {sorted(old - new)}\n  graph-only: {sorted(new - old)}"
    )
    # `<old>..main` must cover the whole non-merge window — the crux of the live-gate bug.
    # Assert against `new` directly (Review: code-reviewer — Finding 8): the prior
    # assertion pinned only `old`, transitively covering `new` via the equality check
    # above; a future edit reordering/removing that check would silently stop pinning
    # the graph-walk path against ground truth.
    assert chain_set <= new, "a <base>..main record must mark the whole window reviewed"


# ---------------------------------------------------------------------------
# MAJOR correctness fixtures (the Staff Engineer review 2026-07-15,
# state/review-trail/findings/2026-07-15-the Staff Engineer-coverage-graph-walk-correctness.md).
# Both pin the false-COVERED direction on a correctness gate: MUST fail against
# pre-fix code and pass once the fix lands. Ground-truth is the fan-out (`old`)
# PLUS an independent `git rev-list` oracle — not merely new==old (Finding 5).
# ---------------------------------------------------------------------------

def test_build_reviewed_set_graphwalk_caret_n_beyond_parents(tmp_path: Path, merge_topology) -> None:
    """MAJOR-1: a malformed ^N beyond a commit's parent count on the NEGATIVE endpoint
    must skip the whole range (matching git's `fatal: bad revision`, rc!=0), not
    collapse to an in-memory ∅ that leaves the positive side's entire reach
    uncontested — the false-COVERED bug: reach(R) - ∅ = reach(R) = the whole chain.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    # C1 is a non-merge (single-parent) commit — C1^2 is a malformed ref (git
    # itself fails to resolve it: "fatal: bad revision").
    bad_range = f"{s['C1']}^2..HEAD"
    p = tmp_path / "bad_caret.json"
    _write_range_record(p, bad_range)

    old = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    new = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    assert old == set(), "fan-out sanity: git must fail a malformed ^N ref, range skips to ∅"
    assert new == old, (
        f"graph-walk did not skip the malformed-^N range like fan-out does — "
        f"false COVERED of: {sorted(new - old)}"
    )


def test_build_reviewed_set_graphwalk_foreign_negative_endpoint(tmp_path: Path, merge_topology) -> None:
    """MAJOR-2: a valid negative endpoint off the window's lineage (a side-branch
    tip descended from an in-window commit, never merged, and NOT an ancestor of
    the window base) must fall back to a real git rev-list for that range — the
    in-window graph is insufficient to compute its reach. Collapsing it to ∅
    silently marks its shared ancestors as reviewed (false COVERED).

    Uses branch name "main" (not "HEAD") as the positive endpoint — see
    test_build_reviewed_set_graphwalk_symbolic_and_abbrev_endpoints's docstring
    for why: a stored literal "HEAD" now hits the separate sha_range
    false-COVERED Phase 1 exclusion, unrelated to what this test exercises.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    # Branch off C2 that is never merged back — a genuinely foreign, off-lineage
    # tip: git can resolve it, but it is neither in-window nor an ancestor of C0.
    _git(["checkout", "-b", "abandoned", s["C2"]], repo)
    abandoned_tip = _make_commit(repo, "AB: abandoned side work, never merged")
    _git(["checkout", "main"], repo)

    foreign_range = f"{abandoned_tip}..main"
    p = tmp_path / "foreign_neg.json"
    _write_range_record(p, foreign_range)

    old = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    new = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    # Independent ground-truth oracle (Finding 5 discipline — not merely new==old).
    oracle = _rev_list(foreign_range, repo) & chain_set
    assert old == oracle, "fan-out sanity check against the independent oracle"
    assert new == old, (
        f"graph-walk diverged on a foreign negative endpoint.\n"
        f"  fan-out-only (missed by graph-walk): {sorted(old - new)}\n"
        f"  graph-only (false COVERED): {sorted(new - old)}"
    )
    # C1/C2 are ancestors of BOTH abandoned_tip and HEAD → must be subtracted.
    assert s["C1"] not in new and s["C2"] not in new, (
        "commits shared with the abandoned side-branch must be subtracted, "
        "not falsely covered"
    )


# ---------------------------------------------------------------------------
# Test-quality audit findings (code-reviewer 2026-07-15, state/review-trail/findings/
# 2026-07-15-codereview-slicecoverage-reviewed-set-test-audit-coordinator-core-tests-
# test-coverage-rev.md). F1-F4 close P1 endpoint-resolution coverage gaps; F6-F7 close
# P2 topology gaps.
# ---------------------------------------------------------------------------

def test_build_reviewed_set_graphwalk_below_window_tilde_n(tmp_path: Path, merge_topology) -> None:
    """F2 (P1, below-window leg): a `~N` op-chain that walks BELOW the chain base
    mid-walk (as opposed to a base token that's already out-of-window) must still
    collapse to ∅ correctly and agree with the fan-out. C1~10 walks off the front
    of a short repo history — well below P0/C0.

    Uses branch name "main" (not "HEAD") as the positive endpoint — see
    test_build_reviewed_set_graphwalk_symbolic_and_abbrev_endpoints's docstring
    for why: a stored literal "HEAD" now hits the separate sha_range
    false-COVERED Phase 1 exclusion, unrelated to what this test exercises.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    # C1~2 lands on P0 (the pre-history root) — below the window base (C0), but
    # still a real, git-resolvable ancestor (unlike a large N that walks off the
    # front of history entirely and produces a bad ref, which git itself fails).
    below_window_range = f"{s['C1']}~2..main"
    p = tmp_path / "below_window.json"
    _write_range_record(p, below_window_range)

    old = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    new = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    oracle = _rev_list(below_window_range, repo) & chain_set
    assert old == oracle
    assert new == old, (
        f"graph-walk diverged on a below-window ~N walk.\n"
        f"  fan-out-only: {sorted(old - new)}\n  graph-only: {sorted(new - old)}"
    )
    assert chain_set <= new, "C1~2..main must cover the whole non-merge window"


def test_build_reviewed_set_graphwalk_on_record_error_fail(tmp_path: Path, merge_topology) -> None:
    """F3 (P1): on_record_error='fail' combined with graph_range must raise identically
    to the fan-out path for a genuinely bad ref. Prior coverage only exercised
    'fail' without graph_range (fan-out only); the graph-walk's own fail-mode raise
    (_reviewed_via_graph_walk) and its _ENDPOINT_UNRESOLVED plumbing had zero coverage.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    bad_record = {
        "sha_range": "deadbeef00000000000000000000000000000000^..deadbeef00000000000000000000000000000000",
        "scope_kind": "diff",
        "verdict": "ok",
    }
    bad = tmp_path / "bad_graphwalk.json"
    bad.write_text(json.dumps(bad_record), encoding="utf-8")

    with pytest.raises(RuntimeError, match="git rev-list"):
        build_reviewed_set(
            [str(bad)], on_record_error="fail", intersect_shas=chain_set,
            repo_root=str(repo), graph_range=graph_range,
        )


def test_build_reviewed_set_graphwalk_graph_build_failure_falls_back(tmp_path: Path) -> None:
    """F4 (P2): a SAFE_RANGE-valid but git-unresolvable graph_range must silently
    degrade to the per-range fan-out (build_reviewed_set:536-546 docstring claim),
    not raise or return an empty/wrong set. flat mode always passes graph_range, so a
    broken degrade condition would go undetected in production without this test.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    sha = _make_commit(repo, "C1: work")

    good = tmp_path / "good.json"
    _write_trail_record(good, sha)

    # SAFE_RANGE-valid (starts alnum both sides) but `nonexistentbranch` cannot be
    # resolved by `git rev-list --parents` → _build_parent_map returns None.
    unresolvable_graph_range = "nonexistentbranch..HEAD"

    reviewed = build_reviewed_set(
        [str(good)], on_record_error="skip", intersect_shas=None,
        repo_root=str(repo), graph_range=unresolvable_graph_range,
    )
    assert sha in reviewed, (
        "a parent-map build failure must silently degrade to the fan-out, "
        "not drop valid records"
    )


def test_build_reviewed_set_graphwalk_branch_name_endpoint(tmp_path: Path, merge_topology) -> None:
    """F6 (P2): a plain branch-name endpoint (not just HEAD) must resolve correctly.
    _resolve_base's own docstring names "symbolic ref (HEAD, branch, origin/main,
    tag)" as valid shapes, but the prior symbolic/abbrev test only ever exercised HEAD.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo, s = merge_topology
    graph_range = f"{s['C0']}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    # Branch name must satisfy SAFE_RANGE's char class ([0-9A-Za-z_/.~^]) — no
    # hyphens (a hyphenated name would be silently skipped in Phase 1, producing
    # a misleadingly-empty reviewed_set rather than exercising this endpoint shape).
    _git(["branch", "sidetip", s["S2"]], repo)
    branch_range = f"{s['C2']}..sidetip"
    p = tmp_path / "branch_name.json"
    _write_range_record(p, branch_range)

    old = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
    )
    new = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    oracle = _rev_list(branch_range, repo) & chain_set
    assert old == oracle
    assert new == old, (
        f"branch-name endpoint diverged.\n"
        f"  fan-out-only: {sorted(old - new)}\n  graph-only: {sorted(new - old)}"
    )
    assert s["S1"] in new and s["S2"] in new


def test_build_reviewed_set_graphwalk_octopus_merge(tmp_path: Path) -> None:
    """F7 (P2): _reach_chain's "all-parents BFS" must reach through an octopus merge
    (>2 parents), not just a 2-parent merge. A regression that silently truncated to
    the first N-1 parents would not be caught by the 2-parent merge fixture alone.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _make_commit(repo, "base")

    _git(["checkout", "-b", "branch-a", base], repo)
    a1 = _make_commit(repo, "A1")
    _git(["checkout", "-b", "branch-b", base], repo)
    b1 = _make_commit(repo, "B1")
    _git(["checkout", "-b", "branch-c", base], repo)
    c1 = _make_commit(repo, "C1")

    _git(["checkout", "main"], repo)
    main_tip = _make_commit(repo, "main tip")
    # Octopus merge: three additional parents in one merge commit.
    _git(["merge", "--no-ff", "-m", "octopus", "branch-a", "branch-b", "branch-c"], repo)
    octopus_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()
    tail = _make_commit(repo, "tail")

    graph_range = f"{base}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    p = tmp_path / "octopus_rec.json"
    _write_range_record(p, f"{octopus_sha}^..{octopus_sha}")

    reviewed = build_reviewed_set(
        [str(p)], on_record_error="skip", intersect_shas=chain_set, repo_root=str(repo),
        graph_range=graph_range,
    )
    oracle = _rev_list(f"{octopus_sha}^..{octopus_sha}", repo) & chain_set
    assert reviewed == oracle
    for parent_tip in (a1, b1, c1):
        assert parent_tip in reviewed, (
            f"octopus-merge BFS must reach ALL parent branches, missed {parent_tip!r}"
        )


def test_build_reviewed_set_probe_in_window_ambiguous_prefix(tmp_path: Path) -> None:
    """F1 (P1): the `len(matches) > 1` branch of _probe_in_window (an abbreviated
    prefix matching >1 in-window SHA, disambiguated via `git rev-parse`) has zero
    coverage in the promoted suite — every existing prefix fixture is unique
    in-window. Real SHA collisions at short prefix lengths cannot be reliably mined
    (content-addressed, unpredictable), so this white-box unit test constructs a
    synthetic ambiguous parent_map directly and exercises _probe_in_window against
    the real repo's git rev-parse disambiguation.
    """
    from coordinator_core.coverage import _probe_in_window, _ENDPOINT_OUT_OF_WINDOW, _OutOfWindowCache

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0")
    real_sha = _make_commit(repo, "C1: the real match")

    # Synthetic ambiguous parent_map: a fabricated sibling SHA sharing the same
    # 6-char prefix as real_sha, so `matches` (computed by the caller, _resolve_base)
    # would have length 2 — the scenario that routes into _probe_in_window.
    prefix = real_sha[:6]
    fake_sibling = prefix + "f" * (40 - len(prefix))
    if fake_sibling == real_sha:
        fake_sibling = prefix + "e" * (40 - len(prefix))
    parent_map = {real_sha: [], fake_sibling: []}

    # git rev-parse the bare prefix in the REAL repo resolves unambiguously to
    # real_sha (fake_sibling was never actually committed) — this exercises the
    # genuine `git rev-parse --verify --quiet <token>` disambiguation call.
    oow_cache = _OutOfWindowCache()
    resolved = _probe_in_window(prefix, parent_map, str(repo), "skip", real_sha, oow_cache)
    assert resolved == real_sha, (
        f"expected _probe_in_window to disambiguate {prefix!r} to {real_sha!r} "
        f"via git rev-parse, got {resolved!r}"
    )

    # A prefix git cannot resolve at all must fall back to OUT_OF_WINDOW under skip.
    unresolvable = _probe_in_window("ffffff", parent_map, str(repo), "skip", real_sha, oow_cache)
    assert unresolvable is _ENDPOINT_OUT_OF_WINDOW


def test_batch_check_hex_tokens_ambiguous_token_not_treated_as_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review: code-reviewer item 3 + EM follow-up (2026-07-28) — a genuinely
    ambiguous short prefix must never come back as a "resolved" SHA.

    A real ambiguous-SHA collision cannot be reliably mined (content-addressed,
    unpredictable — same rationale as the sibling _probe_in_window test above),
    so this monkeypatches subprocess.run to reproduce git's empirically-observed
    `--batch-check` output shape for an ambiguous token verbatim
    ("<token> ambiguous" on stdout, rc=0, diagnostics on stderr) and asserts the
    parser resolves it to None rather than handing back the bare abbreviated
    token as though it were a full objectname.
    """
    from coordinator_core import coverage as cov_mod

    def _fake_run(cmd, **kwargs):
        assert cmd[:2] == ["git", "cat-file"]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="aaa missing\n332c ambiguous\nffffffffffff missing\n",
            stderr="error: short object ID 332c is ambiguous\nhint: ...\n",
        )

    monkeypatch.setattr(cov_mod.subprocess, "run", _fake_run)

    result = cov_mod._batch_check_hex_tokens(["aaa", "332c", "ffffffffffff"], str(tmp_path))

    assert result == {"aaa": None, "332c": None, "ffffffffffff": None}


def test_build_reviewed_set_session_scope_excludes_concurrent_peer_commit(tmp_path: Path) -> None:
    """scope="session" must not credit a chronologically-interleaved commit
    authored by a DIFFERENT concurrent session, even though it is genuine git
    ancestry within the record's own sha_range.

    Regression for the 2026-07-26 incident: on a shared branch with two
    CONCURRENT (not sequential-baton) sessions, session B's own scope="session"
    trail record (bdfe4bfe~1..6f593f3c-shaped: B's own commit boundaries) swept
    in session A's commit that happened to land chronologically in between —
    git rev-list is real ancestry (B2 IS an ancestor of B3 on a linear history),
    but session B never reviewed A's diff. Fixed via _narrow_foreign_session_scope:
    a commit whose OWN Session-Id trailer names a different session is excluded
    from a scope="session" record's credited set.

    This test MUST fail against the pre-fix build_reviewed_set (which trusted
    raw sha_range git-rev-list reachability for scope="session" identically to
    scope="chain") and pass after.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    session_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    b1 = _commit_for_session(repo, "B1: session B starts its own segment", session_b)
    a1 = _commit_for_session(repo, "A1: session A's UNRELATED concurrent commit", session_a)
    b2 = _commit_for_session(repo, "B2: session B's review-integration commit", session_b)

    chain_set = {a1}

    # Session B writes a scope="session" record spanning ITS OWN boundaries
    # (b1..b2) — a1 falls chronologically inside that window purely because the
    # two sessions interleaved on the shared branch, not because B reviewed it.
    record_path = tmp_path / "session_b_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "session",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 10,
                "session_id": session_b,
            }
        ),
        encoding="utf-8",
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )

    assert a1 not in reviewed, (
        "session B's scope='session' record must not credit session A's "
        "interleaved, unreviewed commit a1 — false COVERED regression"
    )
    assert reviewed == set()


def test_chain_ancestry_waiver_scope_mismatch_does_not_relax_credit(
    tmp_path: Path,
) -> None:
    """AC3's NAMED test: a chain-ancestry waiver minted for chain A must NOT
    relax the foreign-session strip for a record belonging to a DIFFERENT
    chain B — this is the point of the chunk (C1). If this test passes
    trivially (e.g. because the waiver relaxes for ANY record, presence-only
    like DR-243's pm-vouches), the read-side scope check was not actually
    implemented.

    Same fixture shape as the sibling exclusion test above
    (``test_build_reviewed_set_session_scope_excludes_concurrent_peer_commit``),
    but the waiver here is minted for ``chain_a`` while the reading record's
    own ``session_id`` (its chain identity, scope="chain") is ``chain_b``.
    """
    from coordinator_core import chain_ancestry_waivers
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    chain_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    chain_a = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    b1 = _commit_for_session(repo, "B1: chain B's own segment start", chain_b)
    a1 = _commit_for_session(repo, "A1: foreign commit, waived only for chain A", session_a)
    b2 = _commit_for_session(repo, "B2: chain B's review-integration commit", chain_b)

    chain_set = {a1}

    # Chain-B's own scope="chain" record over its OWN boundaries.
    record_path = tmp_path / "chain_b_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 10,
                "session_id": chain_b,
            }
        ),
        encoding="utf-8",
    )

    # Mint a1's waiver for chain A only — NOT chain B.
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({a1}), chain_a,
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )

    assert a1 not in reviewed, (
        "a chain-ancestry waiver minted for chain A must not relax the "
        "foreign-session strip for chain B's record — scope mismatch must "
        "still refuse credit (AC3)"
    )
    assert reviewed == set()


def test_chain_ancestry_waiver_credits_matching_chain(tmp_path: Path) -> None:
    """Positive counterpart to the scope-mismatch test above: a waiver
    minted for chain B DOES relax the strip for chain B's own record — the
    exact-chain-identity match this module implements (see
    coverage.py's `_chain_ancestry_waived_shas` docstring for why exact
    match, not ancestry-node membership, was chosen).
    """
    from coordinator_core import chain_ancestry_waivers
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    chain_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    b1 = _commit_for_session(repo, "B1: chain B's own segment start", chain_b)
    a1 = _commit_for_session(repo, "A1: ancestry commit, chain-waived for chain B", session_a)
    b2 = _commit_for_session(repo, "B2: chain B's review-integration commit", chain_b)

    chain_set = {a1}

    record_path = tmp_path / "chain_b_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 10,
                "session_id": chain_b,
            }
        ),
        encoding="utf-8",
    )

    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({a1}), chain_b,
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )

    assert a1 in reviewed, (
        "a1's waiver was minted for chain B, and the reading record's own "
        "session_id IS chain B — this exact chain-identity match must "
        "credit it"
    )


def test_chain_ancestry_waiver_idempotent_remint(tmp_path: Path) -> None:
    """Re-minting the SAME (sha, chain_id) pair must be a no-op — the first
    mint's waiver file is neither duplicated nor overwritten with different
    content, and the read side still resolves exactly one waived sha.
    """
    from coordinator_core import chain_ancestry_waivers

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sha = "d" * 40
    chain_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({sha}), chain_id, source_handoff="state/handoffs/first.md",
    )
    waiver_path = (
        chain_ancestry_waivers.chain_waiver_dir(str(repo), chain_id) / f"{sha}.json"
    )
    first_content = waiver_path.read_text(encoding="utf-8")

    # Re-mint: a second call for the identical (sha, chain_id) pair, even
    # naming a DIFFERENT source_handoff, must not alter the persisted file —
    # first mint wins, exactly like DR-243's O_CREAT|O_EXCL pm-vouches.
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({sha}), chain_id, source_handoff="state/handoffs/second.md",
    )

    assert waiver_path.read_text(encoding="utf-8") == first_content, (
        "re-mint of an already-waived (sha, chain_id) pair must be a "
        "true no-op, not a silent overwrite"
    )
    assert chain_ancestry_waivers.chain_ancestry_waived_shas(str(repo), chain_id) == {sha}


def test_chain_ancestry_waiver_chain_id_with_trailing_newline_is_rejected(
    tmp_path: Path,
) -> None:
    """A `chain_id` ending in a newline must fail the directory-name-safety
    check, and must mint nothing.

    Regression for the `$`-vs-`\\Z` anchor: Python's `$` matches at
    end-of-string OR immediately before one trailing newline, so the guard
    admitted a `chain_id` whose newline then landed in a directory name.
    Not a traversal (no separator is admitted either way), but this regex is
    the ONLY path-safety validator on that value, and a validator with a
    known hole is worse than an honest absence — the next reader trusts it.
    """
    from coordinator_core import chain_ancestry_waivers

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sha = "e" * 40
    bad_chain_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee\n"

    assert chain_ancestry_waivers.chain_waiver_dir(str(repo), bad_chain_id) is None, (
        "a chain_id with a trailing newline must not resolve to a path"
    )

    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({sha}), bad_chain_id,
    )
    assert not chain_ancestry_waivers.chain_root_dir(str(repo)).exists(), (
        "a shape-invalid chain_id must mint nothing at all, not a "
        "newline-named subdirectory under the waiver root"
    )

    # The read side agrees: no path resolves, so no sha is waived.
    assert chain_ancestry_waivers.chain_ancestry_waived_shas(
        str(repo), bad_chain_id
    ) == frozenset()


def test_chain_ancestry_waiver_multi_chain_same_sha(tmp_path: Path) -> None:
    """The routine case the per-chain-subdirectory design exists for: a SHA
    in more than one chain's ancestry gets a LEGITIMATE waiver minted for
    EACH chain, and neither mint silently denies the other — the design
    hazard a single O_CREAT|O_EXCL scalar-per-sha file would hit.
    """
    from coordinator_core import chain_ancestry_waivers

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sha = "e" * 40
    chain_x = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    chain_y = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    chain_ancestry_waivers.record_chain_ancestry_waiver(str(repo), frozenset({sha}), chain_x)
    chain_ancestry_waivers.record_chain_ancestry_waiver(str(repo), frozenset({sha}), chain_y)

    assert chain_ancestry_waivers.chain_ancestry_waived_shas(str(repo), chain_x) == {sha}, (
        "chain X's waiver for the shared sha must exist even though chain Y "
        "also minted a waiver for the identical sha"
    )
    assert chain_ancestry_waivers.chain_ancestry_waived_shas(str(repo), chain_y) == {sha}, (
        "chain Y's waiver for the shared sha must exist independently of "
        "chain X's — neither mint may deny the other"
    )
    # And each chain's own set is genuinely independent — a third, never-
    # minted chain sees nothing for this sha.
    assert chain_ancestry_waivers.chain_ancestry_waived_shas(str(repo), "0" * 40) == frozenset()


def test_build_reviewed_set_session_scope_fails_closed_on_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `_narrow_foreign_session_scope` git subprocess failure must NOT revive the
    over-crediting bug it exists to close.

    Review: code-reviewer — Finding 1, WARN 958054a5. Pre-fix, `rc != 0`
    from the backing `git log` call left `foreign` as the empty set, so
    `shas - frozenset()` credited the record's FULL raw sha_range reachability
    — identical to having no session-scope narrowing at all. This forces that
    subprocess to fail and asserts the fix: `on_record_error="skip"` excludes
    the record's entire contribution (a1 must NOT be credited, mirroring the
    concurrent-peer-exclusion test's assertion), and `on_record_error="fail"`
    propagates instead of silently falling back to open-credit.
    """
    import coordinator_core.coverage as coverage_mod
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    b1 = _make_commit(repo, "B1: session B's own segment start")
    a1 = _make_commit(repo, "A1: an interleaved commit that must NOT be credited")
    b2 = _make_commit(repo, "B2: session B's own segment end")

    chain_set = {a1}

    record_path = tmp_path / "session_b_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "session",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 10,
                "session_id": session_b,
            }
        ),
        encoding="utf-8",
    )

    real_run = coverage_mod._run

    def _failing_run(cmd, cwd=None):
        if cmd[:2] == ["git", "log"]:
            return 1, "", "simulated git log failure"
        return real_run(cmd, cwd=cwd)

    monkeypatch.setattr(coverage_mod, "_run", _failing_run)

    # skip: fail closed — the record contributes nothing, never falls back
    # to crediting a1 as if no foreign commits were found.
    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )
    assert a1 not in reviewed, (
        "a git-log failure inside _narrow_foreign_session_scope must fail CLOSED — "
        "the scope='session' record must not silently regain full-width "
        "crediting of an unreviewed interleaved commit"
    )
    assert reviewed == set()

    # fail: propagates rather than silently degrading to open-credit.
    with pytest.raises(Exception):
        build_reviewed_set(
            [str(record_path)],
            on_record_error="fail",
            intersect_shas=chain_set,
            repo_root=str(repo),
            graph_range=None,
        )


def test_build_reviewed_set_session_scope_credits_untrailered_commit(tmp_path: Path) -> None:
    """scope="session" must still credit a commit that carries NO Session-Id
    trailer at all — `_narrow_foreign_session_scope` is deliberately EXCLUSION-based
    (strip only commits AFFIRMATIVELY attributed to a different session), not
    inclusion-based (keep only commits attributed to own_session_id). This
    pins the safe-side guarantee `_narrow_foreign_session_scope`'s own docstring makes
    explicit: "a commit with NO Session-Id trailer at all ... is left credited
    exactly as before."

    That property is exercised incidentally by every other scope="session"
    test in this file (their fixture commits are untrailered), but no test
    name or docstring states it as the property under test — see Finding 4,
    WARN 958054a5: a future refactor that adds trailers "for realism" to
    those fixtures could silently drop the only coverage of this guarantee
    with nothing flagging the loss. This test names it directly and MUST
    fail if `_narrow_foreign_session_scope` is ever changed to inclusion-based
    (own_session_id-only) filtering.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    # No Session-Id trailer on either commit — untrailered authoring history.
    b1 = _make_commit(repo, "B1: untrailered commit before the record's own tip")
    b2 = _make_commit(repo, "B2: untrailered commit at the record's own tip")

    chain_set = {b2}

    record_path = tmp_path / "session_b_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "session",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 10,
                "session_id": session_b,
            }
        ),
        encoding="utf-8",
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )

    assert b2 in reviewed, (
        "scope='session' must credit an untrailered commit exactly as before "
        "— _narrow_foreign_session_scope is exclusion-based, not inclusion-based, "
        "and must not strip a commit that carries no Session-Id trailer"
    )


# ---------------------------------------------------------------------------
# Stored literal-HEAD tests — the sha_range false-COVERED defect (read side).
# state/improvement-queue/2026-06-30-review-coverage-gate-false-covered-on-tr.yaml:
# a trail record persisted with a literal "HEAD" on the right of its sha_range
# (e.g. "0227ea17..HEAD") re-resolves at READ time against whatever HEAD is
# current when the gate runs — so its certified width silently grows to cover
# every commit landed after the record was written, none of which any
# reviewer opened. These tests reproduce that growth directly against a real
# git repo and assert the fix (coverage.py's _record_range_has_stored_head
# Phase-1 exclusion) holds it flat.
# ---------------------------------------------------------------------------


def test_build_reviewed_set_stored_head_does_not_grow_with_new_commits(tmp_path: Path) -> None:
    """A record citing '<sha>..HEAD' must not silently credit commits landed
    AFTER the record was written — reproduces the exact live defect
    (example-doctrine-repo 2026-07-25, work/machine-a/2026-07-21: chain_commits=70
    covered=70 uncovered=0 off 8 ..HEAD records).

    Sequence: C0 (base) -> C1 (reviewed, tip at write time) -> record cites
    "C0..HEAD" (HEAD == C1 when written) -> C2, C3 land afterward, unreviewed.
    A pre-fix reader re-resolves HEAD to the CURRENT tip (C3) and credits
    C1, C2, AND C3. The fix must credit only what "HEAD" meant at write time
    is unknowable from the stored literal, so it must credit NOTHING from
    this record — never over-credit, per the module's conservative-exclusion
    contract.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    c0 = _make_commit(repo, "C0: base")
    c1 = _make_commit(repo, "C1: reviewed, HEAD at record-write time")

    record_path = tmp_path / "head_record.json"
    _write_range_record(record_path, f"{c0}..HEAD")

    # Commits landed AFTER the trail record was (conceptually) written —
    # never reviewed, never should be credited.
    c2 = _make_commit(repo, "C2: landed after the record, unreviewed")
    c3 = _make_commit(repo, "C3: landed after the record, unreviewed")

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas={c0, c1, c2, c3},
        repo_root=str(repo),
        graph_range=f"{c0}..{c3}",
    )

    assert c2 not in reviewed and c3 not in reviewed, (
        f"stored literal 'HEAD' must not credit commits landed after the "
        f"record was written — got reviewed={reviewed!r}, expected c2={c2!r} "
        f"and c3={c3!r} absent (false COVERED regression)"
    )
    # Conservative-exclusion contract: the whole record is dropped, including
    # c1 (which genuinely was reviewed at write time but has no persisted
    # anchor to prove it) — under-crediting is the safe direction here.
    assert c1 not in reviewed
    assert reviewed == set()


def test_build_reviewed_set_stored_head_excluded_via_fanout_path_too(tmp_path: Path) -> None:
    """Same defect, exercised through the per-range fan-out fallback (no
    graph_range/intersect_shas) — the Phase-1 classification filter runs
    before either strategy, so both must be immune identically.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    c0 = _make_commit(repo, "C0: base")
    _make_commit(repo, "C1: reviewed, HEAD at record-write time")

    record_path = tmp_path / "head_record_fanout.json"
    _write_range_record(record_path, f"{c0}..HEAD")

    c2 = _make_commit(repo, "C2: landed after the record, unreviewed")

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        repo_root=str(repo),
    )

    assert c2 not in reviewed
    assert reviewed == set()


def test_build_reviewed_set_concrete_range_unaffected_by_head_filter(tmp_path: Path) -> None:
    """Sanity check: a record with a genuine concrete-SHA range (the shape the
    write-side fix now always produces) is unaffected by the stored-HEAD
    filter — it must still be credited normally.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    c0 = _make_commit(repo, "C0: base")
    c1 = _make_commit(repo, "C1: reviewed")

    record_path = tmp_path / "concrete_record.json"
    _write_range_record(record_path, f"{c0}..{c1}")

    c2 = _make_commit(repo, "C2: landed later, unreviewed")

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas={c0, c1, c2},
        repo_root=str(repo),
        graph_range=f"{c0}..{c2}",
    )

    assert c1 in reviewed, "a genuine concrete-SHA record must still be credited"
    assert c2 not in reviewed


# ---------------------------------------------------------------------------
# C4b — read-side crediting, the no-record floor (AC6), the finding-0
# containment regression (AC7), and the chain-set bounding of gate-minted
# crediting (AC11). See docs/plans/2026-07-31-review-trail-chain-ancestry-
# discriminator.md § "What the inversion actually buys, and what it does
# not" and § "Honest accounting — the disclosed limit": this chunk decides
# whether the inversion is sound and whether it bounds its own fail-open.
# ---------------------------------------------------------------------------


def test_chain_ancestry_waiver_alone_without_any_trail_record_credits_nothing(
    tmp_path: Path,
) -> None:
    """AC6 — the real floor, narrower than "no HALT can pre-authorise a
    review that never happened" (a `verdict=blocked` record from ANY
    session, at ANY time, clears the floor too — see the sibling test
    below). The floor this test pins is genuinely narrower: crediting
    requires SOME trail record whose range spans the SHA — zero records,
    with only a minted waiver on disk, must credit nothing.

    Traced structurally, not just behaviourally: `build_reviewed_set`
    returns `set()` immediately on an empty `trail_paths` list (Phase 1's
    `if not valid_ranges: return set()`), and `_chain_ancestry_waived_shas`
    has exactly one call site (`_narrow_foreign_session_scope`), reached
    only per-record inside Phase 2 — a waiver with no accompanying record
    is simply never consulted at all.
    """
    from coordinator_core import chain_ancestry_waivers
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sha = _make_commit(repo, "C1: never opened by any reviewer, only waived")

    chain_id = "12345678-1234-4123-8123-123456789abc"
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({sha}), chain_id,
    )

    # No trail record file at all — trail_paths is empty.
    reviewed = build_reviewed_set(
        [],
        on_record_error="skip",
        intersect_shas={sha},
        repo_root=str(repo),
    )
    assert reviewed == set(), (
        "a minted chain-ancestry waiver with NO accompanying trail record "
        "must credit nothing — the real AC6 floor, narrower than 'no HALT "
        "can pre-authorise a review that never happened'"
    )


def test_build_reviewed_set_blocked_verdict_record_still_credits_chain_waived_commit(
    tmp_path: Path,
) -> None:
    """AC6's named `verdict=blocked` case: `_verdict_counts` excludes ONLY
    `pending` (see that function's own docstring/comment — "ok/warn/
    blocked/waived/absent → INCLUDED"), so a `verdict=blocked` record
    clears the no-record floor too, and its chain-ancestry-waived foreign
    commit is credited exactly like an `ok`-verdict record would be.

    Encoded reading (per AC6's own instruction to "assert whichever ...
    this plan intends"): this is INTENDED, not a bug this chunk found. A
    `blocked` verdict is itself evidence that SOME review happened and was
    disposed unfavourably (findings blocked the change) — it is not the
    absence of a record AC6's floor guards against. `_verdict_counts`
    treating `blocked` as counting toward `reviewed_set` pre-dates this
    plan and is unchanged here; this test pins it explicitly against this
    chunk's new chain-ancestry-waiver crediting path rather than leaving it
    as an unstated assumption.

    Same fixture shape as
    `test_chain_ancestry_waiver_credits_matching_chain` above, with ONE
    difference: the record's verdict is `"blocked"`, not `"ok"`.
    """
    from coordinator_core import chain_ancestry_waivers
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    chain_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    b1 = _commit_for_session(repo, "B1: chain B's own segment start", chain_b)
    a1 = _commit_for_session(repo, "A1: ancestry commit, chain-waived, blocked-verdict record", session_a)
    b2 = _commit_for_session(repo, "B2: chain B's review-integration commit", chain_b)

    chain_set = {a1}

    record_path = tmp_path / "chain_b_blocked_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "blocked",
                "diff_loc": 10,
                "session_id": chain_b,
            }
        ),
        encoding="utf-8",
    )

    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({a1}), chain_b,
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )

    assert a1 in reviewed, (
        "a verdict='blocked' record clears the AC6 no-record floor (only "
        "'pending' is excluded by _verdict_counts) — its chain-waived "
        "commit is credited exactly like an 'ok' verdict would be. This is "
        "intended: 'blocked' is evidence a review genuinely happened and "
        "was disposed unfavourably, not the zero-record case AC6 guards."
    )


def test_ac7_finding0_collateral_commit_verdict_is_covered(tmp_path: Path) -> None:
    """AC7 — the Staff Engineer's finding-0 containment regression, built with
    `commit_anchors` doing the REAL stamping (a hand-written trailer would
    not reproduce the defect: the whole point is that
    `commit_anchors._resolve_plan_from_diff` reads the STAGED diff's single
    `docs/plans/*.md` file's frontmatter, regardless of whose unrelated code
    rides along in the same commit).

    Topology: a peer session (`peer_session_id`) stages a one-line edit to
    the CLOSING chain's own plan file (which carries the closing
    deliverable_id) alongside its own wholly unrelated code
    (`peer_unrelated.txt`). `commit_anchors` stamps that commit with the
    closing chain's `Deliverable-Id`, which sweeps it into `chain_set` via
    leg (a) of `_derive_dag_chain_set`'s segment attribution — exactly
    the Staff Engineer's reported mechanism, reproduced against the real production
    stamping code, not a synthetic trailer.

    Sequence: derive chain_set for real (`_derive_dag_chain_set`) — confirm
    the collateral commit is swept in — mint a chain-ancestry waiver for
    every (uncovered) chain commit, as C2 does at HALT — write the close
    record (scope="chain") over the whole chain range, as
    `/workstream-complete` would — recompute `reviewed_set` — and assert
    PLAINLY what the collateral commit's verdict actually comes out to be.

    Per § "What the inversion actually buys" and § "Honest accounting":
    the plan's own analysis says this comes out COVERED (crediting is
    range-based, not per-SHA — the close record's sha_range credits every
    waived commit in its range whether or not it was individually opened).
    This test pins that outcome plainly, making the disclosed limit
    MEASURED rather than merely asserted.
    """
    from coordinator_core import chain_ancestry_waivers
    from coordinator_core import coverage as cov
    from coordinator_core.coverage import build_reviewed_set
    from coordinator_core.ops import commit_anchors

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    closing_session_id = "11111111-1111-4111-8111-111111111111"
    peer_session_id = "22222222-2222-4222-8222-222222222222"
    deliverable_id = "dlv-ac7-finding0-topology"

    base_sha = _make_commit(repo, "base")

    # The CLOSING chain's own plan file — carries the deliverable_id that
    # commit_anchors._resolve_plan_from_diff reads from the STAGED index.
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    plan_path = plans_dir / "closing-plan.md"
    plan_path.write_text(
        "---\n"
        "title: closing plan\n"
        f"deliverable_id: {deliverable_id}\n"
        "---\n"
        "Body.\n",
        encoding="utf-8",
    )
    _git(["add", "docs/plans/closing-plan.md"], repo)
    _git(
        ["commit", "-m", f"plan: add closing plan\n\nSession-Id: {closing_session_id}"],
        repo,
    )
    plan_add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # The closing handoff naming this same deliverable_id.
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    closing_handoff = handoffs / "closing.md"
    closing_handoff.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: none\n"
        f"deliverable_id: {deliverable_id}\n"
        "---\n"
        "Closing body.\n",
        encoding="utf-8",
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {closing_session_id}"],
        repo,
    )
    closing_add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert closing_add_sha  # keep the intermediate SHA referenced for clarity

    # --- the Staff Engineer's finding-0 topology: a peer session stages a one-line edit
    # to the closing chain's plan file ALONGSIDE its own unrelated code. ---
    peer_file = repo / "peer_unrelated.txt"
    peer_file.write_text("peer's own unrelated work\n", encoding="utf-8")
    _git(["add", "peer_unrelated.txt"], repo)

    plan_path.write_text(
        plan_path.read_text(encoding="utf-8") + "\nOne more collateral line.\n",
        encoding="utf-8",
    )
    _git(["add", "docs/plans/closing-plan.md"], repo)

    # commit_anchors doing the REAL stamping — the mechanism under test.
    plan_info = commit_anchors._resolve_plan_from_diff(repo)
    assert plan_info is not None and plan_info["deliverable_id"] == deliverable_id, (
        f"fixture setup: commit_anchors must resolve the staged plan file's "
        f"deliverable_id — got {plan_info!r}"
    )
    _git(
        [
            "commit", "-m",
            "peer: unrelated work + plan bookkeeping\n\n"
            f"Session-Id: {peer_session_id}\n"
            f"Deliverable-Id: {plan_info['deliverable_id']}",
        ],
        repo,
    )
    peer_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # -----------------------------------------------------------------
    # Derive chain_set the way the gate does at HALT.
    # -----------------------------------------------------------------
    result = cov._derive_dag_chain_set(
        str(closing_handoff.resolve()), str(repo), closing_session_id=closing_session_id,
    )
    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert peer_sha in result.shas, (
        "fixture precondition: the peer's collateral commit must be swept "
        "into chain_set via the Deliverable-Id match on the staged plan "
        f"file — this IS finding-0's mechanism; shas={result.shas!r}"
    )
    chain_set = set(result.shas)

    # -----------------------------------------------------------------
    # Mint (C2's action at HALT): a chain-ancestry waiver per uncovered
    # chain commit — no trail record exists yet, so every chain_set member
    # is uncovered at this point.
    # -----------------------------------------------------------------
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset(chain_set), closing_session_id,
        source_handoff=str(closing_handoff.resolve()),
    )

    # -----------------------------------------------------------------
    # Write the close record (scope="chain") over the whole chain range —
    # what /workstream-complete's own review-trail write would produce.
    # -----------------------------------------------------------------
    record_path = tmp_path / "close_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{base_sha}..{peer_sha}",
                "reviewer": "closing-em",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 999,
                "session_id": closing_session_id,
            }
        ),
        encoding="utf-8",
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=f"{base_sha}..{peer_sha}",
    )

    # THE MEASURED ENDPOINT (not merely "obliged" — that would be true by
    # construction and prove nothing): what does the collateral commit's
    # verdict actually come out to be?
    assert peer_sha in reviewed, (
        "AC7 — the collateral commit comes out COVERED. Crediting is "
        "range-based (the close record's sha_range), not per-SHA: the "
        "chain-ancestry waiver that admitted this record (by clearing the "
        "write-side foreign-session refusal) ALSO credits the peer's "
        "untouched, unopened collateral commit at read time. This matches "
        "the plan's stated expectation in § 'What the inversion actually "
        "buys, and what it does not' and makes the disclosed range-based-"
        "crediting limit in § 'Honest accounting' MEASURED, not merely "
        "asserted."
    )


def test_chain_ancestry_waiver_admitted_record_credits_only_chain_set_intersection(
    tmp_path: Path,
) -> None:
    """AC11's named test — the structural bound on this chunk's own AC7
    finding: a chain-admitted record (admitted because its foreign SHA was
    a gate-minted chain-ancestry waiver) whose resolved `sha_range` spans a
    commit OUTSIDE `chain_set` must credit only the intersection with
    `chain_set` — not the record's full raw range.

    `off_chain` below is deliberately UNTRAILERED (no Session-Id at all),
    so `_narrow_foreign_session_scope`'s exclusion-based foreign-commit
    strip — which only strips a commit PROVABLY attributed, via its own
    trailer, to a different session — never touches it; absent the
    `intersect_shas` bound this AC requires, it would ride in credited for
    free purely because it falls inside the record's sha_range. This is
    exactly the 2026-07-27 defect-1 widening mechanism named in §
    "Cross-plan coordination": a wide chain-admitted record's untrailered,
    interleaved, non-chain commits riding in credited alongside the
    genuinely chain-waived commit.

    Verified load-bearing below by mutation: this test is proven to
    actually depend on `build_reviewed_set`'s chain_set intersection, not
    merely to pass because the fixture happens to agree with it.
    """
    from coordinator_core import chain_ancestry_waivers
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    chain_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    b1 = _commit_for_session(repo, "B1: chain B's own segment start", chain_b)
    # Untrailered, interleaved commit — NOT part of the derived chain_set
    # (stands in for a concurrent peer's unrelated, un-attributed work that
    # happens to land inside this record's sha_range).
    off_chain = _make_commit(repo, "OFF: untrailered, non-chain interleaved commit")
    a1 = _commit_for_session(
        repo, "A1: foreign commit, chain-waived — this is what admits the record", session_a,
    )
    b2 = _commit_for_session(repo, "B2: chain B's review-integration commit", chain_b)

    # Only a1 is part of the derived chain_set — off_chain is NOT, exactly
    # the "spans commits outside chain_set" shape AC11 names.
    chain_set = {a1}

    record_path = tmp_path / "chain_b_record.json"
    record_path.write_text(
        json.dumps(
            {
                "sha_range": f"{b1}..{b2}",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
                "diff_loc": 10,
                "session_id": chain_b,
            }
        ),
        encoding="utf-8",
    )

    # Mint the waiver that admits the record (a1 would otherwise refuse the
    # whole record at write time — see AC1/AC1b).
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(repo), frozenset({a1}), chain_b,
    )

    reviewed = build_reviewed_set(
        [str(record_path)],
        on_record_error="skip",
        intersect_shas=chain_set,
        repo_root=str(repo),
        graph_range=None,
    )

    assert reviewed == {a1}, (
        f"AC11: a chain-admitted record must credit ONLY the intersection "
        f"with chain_set ({chain_set!r}), not its full raw sha_range; got "
        f"{reviewed!r}"
    )
    assert off_chain not in reviewed, (
        "the untrailered, non-chain interleaved commit must NOT ride in "
        "credited for free just because it falls inside the admitted "
        "record's sha_range — this is the 2026-07-27 defect-1 widening "
        "mechanism AC11 exists to bound"
    )


# ---------------------------------------------------------------------------
# Kind-aware crediting (C5, docs/plans/2026-08-05-coverage-gate-planning-
# artifact-class.md § C5): a scope_kind="plan" trail record can now credit
# planning-artifact commits — and must NEVER credit a code commit, however its
# sha_range is drawn. AC5, AC6, AC9.
# ---------------------------------------------------------------------------


def _write_plan_record(
    path: Path, sha_range: str, scope: str = "session",
    session_id: str = "00000000-0000-0000-0000-0000000000aa",
) -> None:
    """Write a scope_kind="plan" trail record citing sha_range."""
    record = {
        "sha_range": sha_range,
        "reviewer": "plan-reviewer",
        "scope": scope,
        "scope_kind": "plan",
        "verdict": "ok",
        "session_id": session_id,
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def _write_integration_record(path: Path, sha_range: str) -> None:
    """Write a scope_kind="integration" trail record citing sha_range."""
    record = {
        "sha_range": sha_range,
        "reviewer": "plan-reviewer",
        "scope": "session",
        "scope_kind": "integration",
        "verdict": "ok",
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def _make_path_commit(repo: Path, rel_path: str, message: str) -> str:
    """Commit a single file at rel_path (creating parent dirs) and return its SHA."""
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"{message}\n", encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()


def test_build_reviewed_set_plan_record_credits_planning_artifact_commit(tmp_path: Path) -> None:
    """AC5: a scope_kind="plan" trail record credits a commit whose only touched
    path is a planning-artifact path (docs/plans/), via the C2 classifier
    (_classify_bookkeeping_shas), reused rather than reinvented.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")

    record_path = tmp_path / "plan_record.json"
    _write_plan_record(record_path, f"{plan_sha}^..{plan_sha}")

    reviewed = build_reviewed_set(
        [str(record_path)], on_record_error="fail", repo_root=str(repo),
    )
    assert plan_sha in reviewed, (
        "a plan review of a planning-artifact-only commit must credit it "
        "(AC5) — the crediting path is now open for scope_kind='plan'"
    )


def test_build_reviewed_set_plan_record_never_credits_code_commit(tmp_path: Path) -> None:
    """AC6 (primary): a plan review's sha_range spans BOTH a planning-artifact
    commit and a genuine code commit — the plan record must credit ONLY the
    planning commit, never the code commit, however the range is drawn.

    This is the Anti-scope's named trap: "just delete the
    `scope_kind in ('plan','integration'): continue` skip" would credit the
    ENTIRE resolved range unconditionally, including the code commit — proven
    below by monkeypatching the kind-aware credit collapse to the naive
    unconditional union and showing THIS SAME assertion then fails.
    """
    import coordinator_core.coverage as cov_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")
    code_sha = _make_path_commit(repo, "src/example.py", "code change")

    record_path = tmp_path / "plan_record.json"
    _write_plan_record(record_path, f"{plan_sha}^..{code_sha}")

    # --- Real (fixed) behavior: code commit is never credited by a plan record.
    reviewed = cov_mod.build_reviewed_set(
        [str(record_path)], on_record_error="fail", repo_root=str(repo),
    )
    assert plan_sha in reviewed, "the planning-artifact commit in range must still be credited"
    assert code_sha not in reviewed, (
        "AC6: a plan review must NEVER credit a code commit, even when its "
        "sha_range happens to span one — false COVERED regression"
    )

    # --- Watched-to-fail: the naive "just delete the skip" shortcut credits
    # the plan bucket's ENTIRE resolved range unconditionally (no planning
    # filter). Simulated by monkeypatching the kind-aware collapse to a bare
    # union of every kind's bucket — this test MUST fail against it, proving
    # this assertion actually exercises AC6 rather than passing vacuously.
    def _naive_delete_the_skip_credit(reviewed_by_kind, cwd):
        credited = set()
        for shas in reviewed_by_kind.values():
            credited |= shas
        return credited

    orig = cov_mod._credit_from_kind_partition
    cov_mod._credit_from_kind_partition = _naive_delete_the_skip_credit
    try:
        naive_reviewed = cov_mod.build_reviewed_set(
            [str(record_path)], on_record_error="fail", repo_root=str(repo),
        )
    finally:
        cov_mod._credit_from_kind_partition = orig

    assert code_sha in naive_reviewed, (
        "sanity check on the negative-test harness itself: the naive "
        "delete-the-skip shortcut must wrongly credit the code commit — if "
        "this fails, the harness is not actually simulating the shortcut "
        "and the AC6 assertion above is not proven to catch it"
    )


def test_build_reviewed_set_plan_record_bookkeeping_only_commit_uncredited(
    tmp_path: Path,
) -> None:
    """A plan record's sha_range may also span a BOOKKEEPING-only commit (not
    code, not planning) — e.g. a state/ ceremony-exhaust commit sitting
    between the planning commit and the range's other endpoint. EXHAUST wins
    on overlap (see _classify_bookkeeping_shas), so this commit is neither
    exhaust-credited (only "diff"-kind buckets get unconditional credit) nor
    planning-credited (it touches no planning-artifact path at all) — it must
    simply not appear in the plan bucket's credited set.

    Review: code-reviewer — closes the one untested combination the AC6/AC9
    surface named: a plan record's range spanning a planning commit AND a
    bookkeeping (not code) commit.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")
    bookkeeping_sha = _make_path_commit(
        repo, "state/some-ledger.jsonl", "bookkeeping-only commit"
    )

    record_path = tmp_path / "plan_record.json"
    _write_plan_record(record_path, f"{plan_sha}^..{bookkeeping_sha}")

    reviewed = build_reviewed_set(
        [str(record_path)], on_record_error="fail", repo_root=str(repo),
    )
    assert plan_sha in reviewed, "the planning-artifact commit in range must still be credited"
    assert bookkeeping_sha not in reviewed, (
        "a bookkeeping-only commit within a plan record's range must not be "
        "credited — it is neither planning nor unconditionally-credited diff"
    )


def test_build_reviewed_set_plan_record_graphwalk_never_credits_code_commit(
    tmp_path: Path,
) -> None:
    """AC6 via Strategy A (single-graph-walk): the same never-credit-code
    guarantee must hold when graph_range/intersect_shas route resolution
    through _reviewed_via_graph_walk's per-kind partition, not just the
    per-range fan-out exercised by the sibling test above.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")
    code_sha = _make_path_commit(repo, "src/example.py", "code change")

    graph_range = f"{base}..HEAD"
    chain_set = _rev_list_no_merges(graph_range, repo)

    record_path = tmp_path / "plan_record.json"
    _write_plan_record(record_path, f"{plan_sha}^..{code_sha}")

    reviewed = build_reviewed_set(
        [str(record_path)], on_record_error="fail", repo_root=str(repo),
        intersect_shas=chain_set, graph_range=graph_range,
    )
    assert plan_sha in reviewed
    assert code_sha not in reviewed, (
        "AC6 must hold identically through the single-graph-walk strategy"
    )


def test_build_reviewed_set_planning_commit_uncredited_without_a_plan_record(
    tmp_path: Path,
) -> None:
    """AC9 (non-vacuous): a planning-artifact commit is NOT auto-credited just
    because it is classifiable PLANNING — it still owes an actual plan review
    trail record. Absent one, it stays uncovered, exactly like any other
    unreviewed commit. This is what keeps the gate honest: planning status
    downgrades the review OBLIGATION (code review -> plan review), it does
    not exempt the commit from needing one.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")

    reviewed = build_reviewed_set([], on_record_error="fail", repo_root=str(repo))
    assert plan_sha not in reviewed, (
        "AC9: a planning-artifact commit with NO trail record covering it "
        "must remain uncovered — planning status is not itself credit"
    )


def test_build_reviewed_set_integration_scope_kind_still_skipped(tmp_path: Path) -> None:
    """Anti-scope: `integration` remains uncreditable — only `plan` is reopened
    by this chunk. A scope_kind="integration" record must credit nothing, even
    when its sha_range points at a genuine planning-artifact commit.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")

    record_path = tmp_path / "integration_record.json"
    _write_integration_record(record_path, f"{plan_sha}^..{plan_sha}")

    reviewed = build_reviewed_set(
        [str(record_path)], on_record_error="fail", repo_root=str(repo),
    )
    assert reviewed == set(), (
        "scope_kind='integration' must stay skipped in Phase 1 — it is a "
        "different question with a different blast radius than 'plan' "
        "(Anti-scope), not reopened by this chunk"
    )


def test_build_reviewed_set_dedup_key_kind_collision(tmp_path: Path) -> None:
    """Regression for the 13-on-disk-record collision this chunk names: a
    'diff' record and a 'plan' record citing the IDENTICAL
    (sha_range, scope, session_id) must each be resolved and bucketed
    independently — not silently collapsed to whichever parses first via the
    Phase 2 dedup `setdefault`, which (pre-fix) could bucket a genuine code
    commit under 'plan' and have it wrongly filtered out.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    code_sha = _make_path_commit(repo, "src/example.py", "code change")

    same_range = f"{code_sha}^..{code_sha}"
    same_scope = "session"
    same_session = "00000000-0000-0000-0000-0000000000bb"

    diff_record = tmp_path / "diff_record.json"
    diff_record.write_text(
        json.dumps(
            {
                "sha_range": same_range, "reviewer": "code-reviewer",
                "scope": same_scope, "scope_kind": "diff", "verdict": "ok",
                "session_id": same_session,
            }
        ),
        encoding="utf-8",
    )
    plan_record = tmp_path / "plan_record.json"
    _write_plan_record(plan_record, same_range, scope=same_scope, session_id=same_session)

    reviewed = build_reviewed_set(
        [str(diff_record), str(plan_record)], on_record_error="fail", repo_root=str(repo),
    )
    assert code_sha in reviewed, (
        "the 'diff' record's credit for the shared sha_range must survive "
        "regardless of dict-iteration order against the co-resident 'plan' "
        "record citing the identical range/scope/session"
    )


def test_build_reviewed_set_plan_record_chain_scope_narrows_foreign_session(
    tmp_path: Path,
) -> None:
    """A scope_kind='plan' record whose scope='chain' (in
    _FOREIGN_STRIPPED_SCOPES) routes through _narrow_foreign_session_scope
    exactly like a 'diff' record does — this is where the 13 on-disk plan
    records (scope_kind='plan', scope='chain') actually land. A planning
    commit authored by a DIFFERENT session than the record's own session_id
    must be excluded, even though it is otherwise plan-creditable.
    """
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "base")

    session_own = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    session_foreign = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

    b1 = _commit_for_session(repo, "B1: own session starts", session_own)
    foreign_plan_sha = _commit_for_session(
        repo, "F1: foreign session's plan commit\n\n"
        "touches docs/plans/foreign.md", session_foreign,
    )
    # Give the foreign commit an actual planning-artifact path (the message
    # trailer above only stamps Session-Id; the diff shape matters here).
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / "foreign.md").write_text("foreign plan\n", encoding="utf-8")
    _git(["add", "docs/plans/foreign.md"], repo)
    _git(
        ["commit", "--amend", "-m", "F1: foreign session's plan commit\n\nSession-Id: "
         f"{session_foreign}"],
        repo,
    )
    foreign_plan_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()
    b2 = _commit_for_session(repo, "B2: own session ends", session_own)

    record_path = tmp_path / "chain_plan_record.json"
    _write_plan_record(record_path, f"{b1}..{b2}", scope="chain", session_id=session_own)

    reviewed = build_reviewed_set(
        [str(record_path)], on_record_error="fail", repo_root=str(repo),
    )
    assert foreign_plan_sha not in reviewed, (
        "scope='chain' plan records must narrow out a foreign session's "
        "interleaved commit exactly like a 'diff' record does — the "
        "planning classification does not bypass foreign-session narrowing"
    )


def _write_unrecognized_kind_record(path: Path, sha_range: str, scope_kind: str) -> None:
    """Write a trail record carrying a scope_kind outside the schema's closed
    enum ({"diff", "plan", "integration"}) — the shape a hand-authored record
    can still produce even after the write-time enum guard, e.g. a record
    written before the 1.1.0 -> 1.2.0 schema bump landed."""
    record = {
        "sha_range": sha_range,
        "reviewer": "code-reviewer",
        "scope": "session",
        "scope_kind": scope_kind,
        "verdict": "ok",
        "session_id": "00000000-0000-0000-0000-0000000000bb",
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def test_build_reviewed_set_unrecognized_scope_kind_degrades_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """2026-08-10 coverage-gate wedge (cross-repo/inbox/2026-08-10-project-
    rag-ue-addon-em-coverage-gate-crashes-on-chunk-and-inline-dispatch-
    kinds.md): a review-trail corpus containing ONE record with an
    unrecognized scope_kind ("chunk") must still let build_reviewed_set
    return a verdict-computable set — never AssertionError the whole gate.
    The unrecognized record earns ZERO credit (fail-closed) and a WARN naming
    the kind and the record is emitted to stderr; a sibling "diff" record in
    the same corpus is credited normally."""
    from coordinator_core.coverage import build_reviewed_set

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    chunk_sha = _make_commit(repo, "C1: would-be chunk review")
    diff_sha = _make_commit(repo, "C2: normal diff review")

    chunk_record = tmp_path / "chunk_record.json"
    _write_unrecognized_kind_record(chunk_record, f"{chunk_sha}^..{chunk_sha}", "chunk")
    diff_record = tmp_path / "diff_record.json"
    _write_trail_record(diff_record, diff_sha)

    reviewed = build_reviewed_set(
        [str(chunk_record), str(diff_record)], on_record_error="fail", repo_root=str(repo),
    )

    assert diff_sha in reviewed, "the sibling diff record must still be credited normally"
    assert chunk_sha not in reviewed, (
        "an unrecognized scope_kind must credit nothing — fail-closed, "
        "not fatal to the whole gate"
    )
    err = capsys.readouterr().err
    assert "chunk" in err and "WARN" in err, (
        "the unrecognized kind must be named in a loud WARN to stderr, not "
        "silently swallowed"
    )
