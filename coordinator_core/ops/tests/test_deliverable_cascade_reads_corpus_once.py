"""
coordinator_core.ops.tests.test_deliverable_cascade_reads_corpus_once — C3: parity
and process-time coverage for the collapsed (single-corpus-read) leg (b) path.

Purpose: pins the behavioural equivalence between the pre-existing unindexed
leg (b) path (`corpus_metas=None`) and the collapsed, metas-indexed path
(`corpus_metas=<pre-read map>`) that C1 added, and pins the process-time claim
(reads flat from fanout 1 to 2, one advanced candidate under 200ms process time
against a >= 275-file corpus). This is the falsifier's fast-tier sibling: the
falsifier measures the REAL corpus once, ad hoc; this suite is the repeatable
pytest regression surface, using synthetic fixtures sized to reproduce the
same read-count shape.

Spec backlink: docs/plans/2026-08-30-the-terminal-cascade-reads-the-corpus-once.md § C3

Negative-spec: does NOT re-test AC5/AC6/AC6a (sizing-kind corpus discrimination,
already owned by test_deliverable_cascade_kinds.py), does NOT change
`_collect_live_candidates_for_kind`'s return arity (plan anti-scope — every
call here unpacks the existing 3-tuple), and does NOT batch/parallelise reads
(also anti-scope) — the read-count assertions pin the EXISTING synchronous,
one-thread read shape.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_deliverable_cascade_reads_corpus_once.py -q
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import pytest
import yaml

import coordinator_core.dag as dag_mod
import coordinator_core.ops.deliverable_cascade as cascade_mod
from coordinator_core.frontmatter.primitives import (
    read_fm_field,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _git,
    _init_repo,
    _seed_handoff,
)

_handler = cascade_mod._handler

# Declared, not excused: this file spawns real `git` processes for the same
# reason its sibling test_deliverable_cascade_kinds.py does — the property
# under test (commit scoping, fixture repo state) is that binary's own
# behaviour. See that file's own pytestmark comment for the ratchet note.
pytestmark = [pytest.mark.spawns_process]


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _seed_successor_handoff(
    repo: Path,
    name: str,
    *,
    predecessor_path: str,
    deployment_state: str = "ready_to_fire",
    deliverable_id: str = "dlv-successor-0",
) -> Path:
    """A live handoff naming `predecessor_path` (repo-relative posix) as its
    own `predecessor:` — the edge leg (b) tests via CONTINUATION_EDGE_KINDS."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Successor {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        f'predecessor: "{predecessor_path}"\n'
        f"deployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _seed_archived_successor_handoff(
    repo: Path,
    name: str,
    *,
    predecessor_path: str,
    deliverable_id: str = "dlv-archived-successor-0",
) -> Path:
    """An ARCHIVE-RESIDENT handoff naming `predecessor_path` as its own
    predecessor — used to pin the archive-equivalence delta (3b): the index
    is built over non-archive nodes only, but `reverse_membership` still
    judges against the full live+archive path list and then drops
    archive-resident referencers via `_is_terminal_or_archived_child`."""
    path = repo / "archive" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Archived Successor {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: closed\n"
        f'predecessor: "{predecessor_path}"\n'
        "deployment_state: shipped\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _make_scoped_commit(repo: Path, session_id: str) -> str:
    """Commit `feature.txt` (the scope: target every fixture below uses) and
    return its OWN short sha -- this, not a later HEAD read, is the sha
    `_advance_one`'s Position A (scope-derived) resolves for shipped_in, since
    no later commit here touches feature.txt again."""
    scoped = repo / "feature.txt"
    scoped.write_text("feature body\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(
        repo, "commit", "-m",
        f"implement the feature this handoff scopes\n\nSession-Id: {session_id}",
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()[:8]


def _pad_corpus(repo: Path, n: int, *, prefix: str = "pad") -> None:
    """Seed `n` additional, unrelated LIVE handoffs so the corpus reaches a
    given size — none of them match any deliverable_id under test, so they
    contribute pure read-count/read-time cost without affecting outcomes."""
    for i in range(n):
        _seed_handoff(
            repo,
            f"20260101-{prefix}-{i:04d}.md",
            deliverable_id=f"dlv-pad-{prefix}-{i:04d}",
        )


# ---------------------------------------------------------------------------
# 1. PARITY — fanout 1 and 2, plus at least one refusal case per leg
# ---------------------------------------------------------------------------


def test_parity_fanout1_advances_identically_baseline_vs_collapsed(tmp_path, monkeypatch):
    """Fanout 1: a single live candidate that clears every leg. `_handler`
    with the collapsed metas path (its own current behaviour) is run against
    one fixture, and a second identical fixture is run through the SAME
    `_predicate_refusal` call shape `_handler` used BEFORE C1 (`corpus_metas`
    absent) — same partitions, byte-identical shipped_in."""
    session_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _build_repo(repo: Path, did: str, handoff_name: str) -> tuple[Path, str]:
        _init_repo(repo)
        feature_sha = _make_scoped_commit(repo, session_id)
        handoff = repo / "state" / "handoffs" / handoff_name
        handoff.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            f'title: "Test Handoff {handoff_name}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            f"deliverable_id: {did}\n"
            "scope:\n"
            "  - feature.txt\n"
        )
        handoff.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(handoff.relative_to(repo)))
        _git(repo, "commit", "-m", "add handoff")
        return handoff, feature_sha

    # Collapsed arm — today's `_handler`, which always threads corpus_metas.
    repo_collapsed = tmp_path / "repo_collapsed"
    handoff_collapsed, feature_sha_collapsed = _build_repo(
        repo_collapsed, "dlv-parity-1-collapsed", "20260101-h.md"
    )
    result_collapsed = _run(
        {
            "deliverable_id": "dlv-parity-1-collapsed",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
        },
        repo_root=repo_collapsed / ".git",
    )

    # Baseline arm — same fixture shape, but `_predicate_refusal` called
    # directly with corpus_metas absent, exactly as before C1.
    repo_baseline = tmp_path / "repo_baseline"
    handoff_baseline, _feature_sha_baseline = _build_repo(
        repo_baseline, "dlv-parity-1-baseline", "20260101-h.md"
    )
    matches, scan_incomplete, _unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo_baseline, "dlv-parity-1-baseline", kind=cascade_mod._HANDOFF_KIND
    )
    assert len(matches) == 1
    reason_baseline = asyncio.run(
        cascade_mod._predicate_refusal(
            matches[0]["path"], matches[0]["fm"], repo_baseline / ".git",
            kind=cascade_mod._HANDOFF_KIND,
        )
    )

    assert len(result_collapsed["advanced"]) == 1
    assert result_collapsed["refused"] == []
    assert result_collapsed["already_advanced"] == []
    assert reason_baseline is None  # baseline also clears every leg — same partition

    split = split_frontmatter(handoff_collapsed.read_text(encoding="utf-8"))
    shipped_in_collapsed = read_fm_field_unquoted(split.fm_text, "shipped_in")
    assert shipped_in_collapsed is not None

    # Byte-identical shipped_in: derive the COLLAPSED repo's own ship sha the
    # same way _advance_one would (scope-derived, against feature.txt) --
    # each repo's commit hashes are its own (absolute tmp path, timestamps),
    # so the two repos are never comparable to each other, only each to its
    # own HEAD.
    assert shipped_in_collapsed == feature_sha_collapsed


def test_parity_fanout2_advances_identically_baseline_vs_collapsed(tmp_path, monkeypatch):
    """Fanout 2: two live candidates for the SAME deliverable_id, both
    clearing every leg — the multi-pass fixpoint path first engages here.
    Collapsed `_handler` must advance both, with the same shipped_in each."""
    session_id = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo = tmp_path / "repo"
    _init_repo(repo)
    feature_sha = _make_scoped_commit(repo, session_id)

    did = "dlv-parity-2-000"
    handoffs = []
    for i in range(2):
        name = f"20260101-h{i}.md"
        handoff = repo / "state" / "handoffs" / name
        handoff.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            f'title: "Test Handoff {name}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            f"deliverable_id: {did}\n"
            "scope:\n"
            "  - feature.txt\n"
        )
        handoff.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(handoff.relative_to(repo)))
        handoffs.append(handoff)
    _git(repo, "commit", "-m", "add both handoffs")

    result = _run(
        {"deliverable_id": did, "source_kind": "plan", "source_path": "docs/plans/dummy.md"},
        repo_root=repo / ".git",
    )

    assert len(result["advanced"]) == 2
    assert result["refused"] == []

    for handoff in handoffs:
        split = split_frontmatter(handoff.read_text(encoding="utf-8"))
        shipped_in = read_fm_field_unquoted(split.fm_text, "shipped_in")
        assert shipped_in == feature_sha


def test_parity_leg_b_live_successor_refusal_matches_baseline_and_collapsed(tmp_path):
    """Leg (b), the rerouted leg — a candidate with a live successor naming
    it as `predecessor:` must be refused, identically, whether leg (b)
    answers via the unindexed baseline path or the collapsed metas path."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = _seed_handoff(repo, "20260101-candidate.md", deliverable_id="dlv-legb-0")
    candidate_rel = str(candidate.relative_to(repo)).replace("\\", "/")
    _seed_successor_handoff(repo, "20260101-successor.md", predecessor_path=candidate_rel)

    # Baseline: corpus_metas absent (the unindexed _handoff_has_live_children path).
    reason_baseline = asyncio.run(
        cascade_mod._predicate_refusal(
            candidate, cascade_mod._HANDOFF_KIND.reader(str(candidate)), repo / ".git",
            kind=cascade_mod._HANDOFF_KIND,
        )
    )

    # Collapsed: corpus_metas populated by the collect pass, exactly as _handler does it.
    corpus_metas: dict = {}
    matches, _scan_incomplete, _unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-legb-0", kind=cascade_mod._HANDOFF_KIND, metas_out=corpus_metas
    )
    assert len(matches) == 1
    reason_collapsed = asyncio.run(
        cascade_mod._predicate_refusal(
            matches[0]["path"], matches[0]["fm"], repo / ".git",
            kind=cascade_mod._HANDOFF_KIND, corpus_metas=corpus_metas,
        )
    )

    assert reason_baseline is not None
    assert reason_collapsed is not None
    assert "live successor" in reason_baseline
    assert "live successor" in reason_collapsed


# ---------------------------------------------------------------------------
# 2. READ COUNT — flat from fanout 1 to 2, not tripled
# ---------------------------------------------------------------------------


def _count_read_meta_calls(monkeypatch) -> list:
    """Wrap dag._read_meta and return a list whose length grows by one per call."""
    calls: list = []
    original = dag_mod._read_meta

    def _wrapped(file_path):
        calls.append(file_path)
        return original(file_path)

    monkeypatch.setattr(dag_mod, "_read_meta", _wrapped)
    return calls


def test_read_count_is_flat_from_fanout_1_to_2(tmp_path, monkeypatch):
    """The actual claim: `dag._read_meta` call count for the collapsed path's
    leg (b) index build must be the SAME at fanout 1 and fanout 2 (both read
    the whole corpus once), not scale with the number of candidates."""
    session_id = "33333333-3333-3333-3333-333333333333"

    def _build(repo: Path, did: str, n_candidates: int, pad: int) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        _init_repo(repo)
        _make_scoped_commit(repo, session_id)
        for i in range(n_candidates):
            name = f"20260101-c{i}.md"
            handoff = repo / "state" / "handoffs" / name
            handoff.parent.mkdir(parents=True, exist_ok=True)
            fm = (
                f'title: "Candidate {name}"\n'
                "created: 2026-01-01\n"
                "branch: work/test/2026-01-01\n"
                "status: open\n"
                'predecessor: "none"\n'
                "deployment_state: ready_to_fire\n"
                f"deliverable_id: {did}\n"
                "scope:\n"
                "  - feature.txt\n"
            )
            handoff.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
            _git(repo, "add", str(handoff.relative_to(repo)))
        _pad_corpus(repo, pad, prefix=f"flat-{n_candidates}")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "seed corpus")

    repo1 = tmp_path / "repo1"
    _build(repo1, "dlv-flat-1", n_candidates=1, pad=20)
    calls1 = _count_read_meta_calls(monkeypatch)
    result1 = _run(
        {"deliverable_id": "dlv-flat-1", "source_kind": "plan", "source_path": "docs/plans/dummy.md"},
        repo_root=repo1 / ".git",
    )
    reads_1 = len(calls1)

    monkeypatch.undo()
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo2 = tmp_path / "repo2"
    _build(repo2, "dlv-flat-2", n_candidates=2, pad=20)
    calls2 = _count_read_meta_calls(monkeypatch)
    result2 = _run(
        {"deliverable_id": "dlv-flat-2", "source_kind": "plan", "source_path": "docs/plans/dummy.md"},
        repo_root=repo2 / ".git",
    )
    reads_2 = len(calls2)

    assert len(result1["advanced"]) == 1
    assert len(result2["advanced"]) == 2
    # `dag._read_meta` is used only by the leg (b) index-build path (via
    # build_reverse_edge_index) in this call graph -- the collect pass reads
    # frontmatter through `kind.reader`, a distinct function. Flat means: the
    # corpus-wide index build cost does not grow with candidate count.
    assert reads_1 == reads_2, (
        f"leg (b)'s dag._read_meta call count must be FLAT across fanout "
        f"(1 candidate: {reads_1} reads, 2 candidates: {reads_2} reads) -- a "
        "constant-factor drop that still scales with fanout is not the claim"
    )


# ---------------------------------------------------------------------------
# 3. FALLBACK IS INTACT — a node absent from metas is still read and still
#    contributes its edges
# ---------------------------------------------------------------------------


def test_fallback_node_absent_from_metas_still_contributes_edges(tmp_path):
    """A successor handoff that appears on disk AFTER the collect pass (so it
    is absent from `metas_out`) must still be read via the `_read_meta`
    fallback inside `build_reverse_edge_index`/`has_live_children_from_metas`,
    and must still be found as a live child -- proving `metas` is a LOOKUP,
    never authoritative."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = _seed_handoff(repo, "20260101-candidate.md", deliverable_id="dlv-fallback-0")
    candidate_rel = str(candidate.relative_to(repo)).replace("\\", "/")

    # Collect pass runs BEFORE the successor exists -- metas_out will not carry it.
    corpus_metas: dict = {}
    matches, _scan_incomplete, _unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-fallback-0", kind=cascade_mod._HANDOFF_KIND, metas_out=corpus_metas
    )
    assert len(matches) == 1
    successor_abs = str(
        (repo / "state" / "handoffs" / "20260101-successor.md").resolve()
    )
    assert successor_abs not in corpus_metas

    # Now the successor appears -- naming candidate as its predecessor.
    _seed_successor_handoff(repo, "20260101-successor.md", predecessor_path=candidate_rel)

    reason = asyncio.run(
        cascade_mod._predicate_refusal(
            matches[0]["path"], matches[0]["fm"], repo / ".git",
            kind=cascade_mod._HANDOFF_KIND, corpus_metas=corpus_metas,
        )
    )

    assert reason is not None
    assert "live successor" in reason


# ---------------------------------------------------------------------------
# 3b. ARCHIVE EQUIVALENCE — indexed-over-301 vs unindexed-over-1202 give the
#     SAME answer for both an archive-resident and a live referencer
# ---------------------------------------------------------------------------


def test_archive_resident_referencer_is_not_a_live_child_either_path(tmp_path):
    """An ARCHIVE-RESIDENT handoff naming the candidate as its predecessor
    must NOT count as a live child, on both the baseline (unindexed) path and
    the collapsed (metas-indexed) path -- the equivalence the archive-split
    depends on: `reverse_membership` drops archive-resident referencers via
    `_is_terminal_or_archived_child` AFTER the membership test, regardless of
    whether the index was built over the archive-filtered or full set."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = _seed_handoff(repo, "20260101-candidate.md", deliverable_id="dlv-archive-eq-0")
    candidate_rel = str(candidate.relative_to(repo)).replace("\\", "/")
    _seed_archived_successor_handoff(
        repo, "20260101-archived-successor.md", predecessor_path=candidate_rel
    )

    reason_baseline = asyncio.run(
        cascade_mod._predicate_refusal(
            candidate, cascade_mod._HANDOFF_KIND.reader(str(candidate)), repo / ".git",
            kind=cascade_mod._HANDOFF_KIND,
        )
    )

    corpus_metas: dict = {}
    matches, _scan_incomplete, _unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-archive-eq-0", kind=cascade_mod._HANDOFF_KIND, metas_out=corpus_metas
    )
    assert len(matches) == 1
    reason_collapsed = asyncio.run(
        cascade_mod._predicate_refusal(
            matches[0]["path"], matches[0]["fm"], repo / ".git",
            kind=cascade_mod._HANDOFF_KIND, corpus_metas=corpus_metas,
        )
    )

    assert reason_baseline is None, "an archive-resident referencer must NOT be treated as a live child (baseline)"
    assert reason_collapsed is None, "an archive-resident referencer must NOT be treated as a live child (collapsed)"


def test_live_referencer_is_a_live_child_either_path(tmp_path):
    """The paired positive case: a LIVE handoff naming the candidate as its
    predecessor IS a live child, on both paths -- without this pair, the
    archive-equivalence claim above is untested against a false-negative
    regression (an index-set narrowing that accidentally drops live
    referencers too, not just archived ones)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    candidate = _seed_handoff(repo, "20260101-candidate.md", deliverable_id="dlv-live-eq-0")
    candidate_rel = str(candidate.relative_to(repo)).replace("\\", "/")
    _seed_successor_handoff(
        repo, "20260101-live-successor.md", predecessor_path=candidate_rel,
        deliverable_id="dlv-live-eq-successor-0",
    )

    reason_baseline = asyncio.run(
        cascade_mod._predicate_refusal(
            candidate, cascade_mod._HANDOFF_KIND.reader(str(candidate)), repo / ".git",
            kind=cascade_mod._HANDOFF_KIND,
        )
    )

    corpus_metas: dict = {}
    matches, _scan_incomplete, _unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-live-eq-0", kind=cascade_mod._HANDOFF_KIND, metas_out=corpus_metas
    )
    assert len(matches) == 1
    reason_collapsed = asyncio.run(
        cascade_mod._predicate_refusal(
            matches[0]["path"], matches[0]["fm"], repo / ".git",
            kind=cascade_mod._HANDOFF_KIND, corpus_metas=corpus_metas,
        )
    )

    assert reason_baseline is not None and "live successor" in reason_baseline
    assert reason_collapsed is not None and "live successor" in reason_collapsed


# ---------------------------------------------------------------------------
# 4. FAIL-CLOSED PRESERVED — unscannable corpus / empty live set -> exit_code 2
# ---------------------------------------------------------------------------


def test_fail_closed_empty_live_set_through_metas_path(tmp_path):
    """An empty live set (no state/handoffs at all) must fail closed
    (exit_code 2) through `has_live_children_from_metas`, exactly as through
    `_handoff_has_live_children`."""
    import coordinator_core.ops.handoff_children as hc_mod

    repo = tmp_path / "repo"
    _init_repo(repo)
    # No state/handoffs directory at all -- _collect_handoff_paths returns
    # an empty live_paths list, which is the fail-closed condition.
    candidate = repo / "nonexistent.md"

    result = asyncio.run(
        hc_mod.has_live_children_from_metas(
            str(candidate), repo / ".git",
            edge_kinds=hc_mod.CONCLUSION_EDGE_KINDS, metas={},
        )
    )
    assert result["exit_code"] == 2
    assert result["children"] == []


def test_fail_closed_candidate_escapes_allowed_roots_through_metas_path(tmp_path):
    """A candidate path outside state/handoffs or archive/handoffs escapes
    containment and must fail closed (exit_code 2) through the metas path."""
    import coordinator_core.ops.handoff_children as hc_mod

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    escapee = repo / "outside.md"
    escapee.write_text("not a handoff\n", encoding="utf-8")

    result = asyncio.run(
        hc_mod.has_live_children_from_metas(
            str(escapee), repo / ".git",
            edge_kinds=hc_mod.CONCLUSION_EDGE_KINDS, metas={},
        )
    )
    assert result["exit_code"] == 2


# ---------------------------------------------------------------------------
# 5. PROCESS TIME — under 200ms at one advanced candidate against a >=275-file
#    corpus, marked cadence so it does not run in the fast tier
# ---------------------------------------------------------------------------


@pytest.mark.cadence
def test_process_time_under_200ms_at_one_advanced_candidate(tmp_path, monkeypatch):
    """A timing assertion that does not name its advanced count is the exact
    defect this whole chain exists to correct -- both are asserted in the
    SAME test."""
    session_id = "44444444-4444-4444-4444-444444444444"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo = tmp_path / "repo"
    _init_repo(repo)
    _make_scoped_commit(repo, session_id)

    handoff = repo / "state" / "handoffs" / "20260101-perf-candidate.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Perf Candidate"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
        "deliverable_id: dlv-perf-0\n"
        "scope:\n"
        "  - feature.txt\n"
    )
    handoff.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(handoff.relative_to(repo)))

    # >= 275-file corpus, per the plan's own reference measurement scale.
    _pad_corpus(repo, 280, prefix="perf")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed perf corpus")

    corpus_size = len(list((repo / "state" / "handoffs").glob("*.md")))
    assert corpus_size >= 275

    pt_before = time.process_time()
    result = _run(
        {"deliverable_id": "dlv-perf-0", "source_kind": "plan", "source_path": "docs/plans/dummy.md"},
        repo_root=repo / ".git",
    )
    pt_after = time.process_time()
    elapsed_ms = (pt_after - pt_before) * 1000.0

    assert len(result["advanced"]) == 1, "a zero-candidate sample passes any bar -- must name would_advance_count == 1"
    assert elapsed_ms < 200.0, f"process_time {elapsed_ms:.2f}ms exceeded the 200ms bar"
