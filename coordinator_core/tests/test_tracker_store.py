"""
coordinator_core.tests.test_tracker_store — tests for the sovereign-tracker
per-machine event store (coordinator_core.tracker_store).

Coverage requirements (per chunk spec C3, plan
docs/plans/2026-07-28-sat-01-sovereign-tracker-substrate.md):
  AC1  — two concurrent cross-process writers on ONE shard: lost-update-free,
         strictly-increasing per-shard sequence, no duplicates, no gaps.
  AC1b — cross-machine merge safety: two independently-built shards merge with
         zero git conflict and a deterministic global order regardless of
         merge direction / shard-glob order. Duplicate sequence values across
         shards are EXPECTED, not an error.
  AC1c — append_event writes ONLY the calling machine's shard; peer shards'
         bytes AND mtimes are unchanged.
  AC1d — read_events FILTERS OUT applied_at:null events entirely (not a
         null-safe sort placement).
  AC1e — append_event rejects missing id/observed_at and same-shard duplicate
         id; cross-machine duplicate id is explicitly NOT caught.
  AC1f — retroactive-reorder characterization: merging a shard with earlier
         applied_at values shifts already-observed events' read-order position.
  AC1g — the filter-is-on-applied_at-not-tier trap: a tier:auto event that
         stamps applied_at projects; one that does not is excluded, visibly.
  AC1h — logical_clock is carried, strictly increasing per shard (incl. a
         same-millisecond counter bump and a backwards clock step), and is
         INERT in read_events' sort key; append_event reads no shard but its
         own while stamping it.
  AC2  — a writer killed mid-write leaves the shard parseable and does not
         wedge the next writer (locked_rmw crash-safety regression).
  AC4  — the shard is created on first write under state/sovereign-tracker/
         and is git-trackable; the C11 settings-home durable-data plane is
         untouched.
  AC7  — static guard: no module under coordinator_core/ops/ imports
         tracker_store, and OP_CLASSIFICATION never references it.
  AC8  — no POSIX-only assumption in path construction, newline handling, or
         lock backend (inherited from locked_rmw; asserted here too).

Spec backlink: pln-sat-01-sovereign-tracker-subst-a66742 § C3

Read first: coordinator_core/tests/test_locked_write.py — the sleep-widened
synthetic cross-process writer technique borrowed for AC1/AC2 below.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# POSIX/Windows lock-backend guard (mirrors test_locked_write.py)
# ---------------------------------------------------------------------------

try:
    import fcntl as _fcntl  # noqa: F401
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Declared, not excused: this file spawns real git because several tests exercise
# `resolve_observed_set`'s actual conflict-marker parsing (`<<<<<<<`/`=======`/`>>>>>>>`)
# against a real merge conflict git itself produces -- no mock reproduces git's own
# conflict-marker byte layout. Each test builds its own repo via `_make_git_repo`, and
# the AC7 revert path mutates real refs/working-tree state, so the fixture is not
# hoisted to module scope.
pytestmark = [
    pytest.mark.skipif(
        not _LOCKING_AVAILABLE,
        reason="locked_rmw needs a file-lock backend (fcntl or msvcrt) — neither available",
    ),
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# ---------------------------------------------------------------------------
# Import under test
# ---------------------------------------------------------------------------

from coordinator_core import tracker_store as ts  # noqa: E402
from coordinator_core.tracker_store import (  # noqa: E402
    CAUSAL_ORDER_A_DOMINATES,
    CAUSAL_ORDER_B_DOMINATES,
    CAUSAL_ORDER_CONCURRENT,
    CAUSAL_ORDER_EQUAL,
    CAUSAL_ORDER_INDETERMINATE,
    EVENTS_DIR_RELPATH,
    OBSERVED_SET_UNKNOWN,
    TrackerStoreDuplicateIdError,
    TrackerStoreError,
    append_event,
    append_events,
    compare_events_causal_order,
    compare_observed_set_vectors,
    fold_observed_set,
    max_sequence,
    read_events,
    resolve_observed_set,
    resolve_observed_set_for_event,
    resolve_observed_set_union_for_event,
    rotate_month,
    shard_path,
)
from coordinator_core.locked_write import LockTimeout, MutateAbort  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Shared git-repo factory (mirrors test_locked_write.py's _make_git_repo)
# ---------------------------------------------------------------------------


def _make_git_repo(root: Path) -> Path:
    """Init a minimal git repository under *root* and return the repo root."""
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-store-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Store Test")
    _git("config", "commit.gpgsign", "false")
    # Pin the two-way conflict-marker format regardless of the developer's
    # global config (Finding 6, code-reviewer, 2026-07-28) — AC7's revert
    # test parses `<<<<<<< HEAD` / `=======` / `>>>>>>>` markers assuming
    # this format; a global `merge.conflictStyle = diff3` would otherwise
    # inject an unstripped `|||||||` base section whose content is not bare
    # JSON, crashing that test's id-filter pass with an unrelated-looking
    # JSONDecodeError instead of the intended assertion.
    _git("config", "merge.conflictStyle", "merge")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


def _git(root: Path, *args: str) -> None:
    """Run one git command against *root*, capturing output (raises on failure).

    Module-level so C2's AC5b (real rebase/reset-and-re-append reproduction)
    and any other test needing raw git plumbing can reuse it without
    re-deriving ``_make_git_repo``'s inner closure.
    """
    subprocess.run(
        ["git"] + list(args),
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _event(event_id: str, observed_at: str, *, applied_at=None, **extra) -> dict:
    """Build a minimal event dict satisfying append_event's id/observed_at contract."""
    d = {"id": event_id, "observed_at": observed_at}
    if applied_at is not None:
        d["applied_at"] = applied_at
    d.update(extra)
    return d


def _write_shard(repo_root: Path, machine: str, records: list[dict]) -> Path:
    """Directly write a shard file for *machine* — simulates an offline/peer
    machine's shard without going through append_event (which only ever
    writes THIS machine's shard)."""
    path = shard_path(repo_root, machine=machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    path.write_text(text, encoding="utf-8")
    return path


def _read_raw_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# AC1 — two concurrent cross-process writers on ONE shard
# ---------------------------------------------------------------------------

_CONCURRENT_APPENDER_SCRIPT = textwrap.dedent("""\
    \"\"\"Sleep-widened concurrent appender: N sequential append_event calls,
    each with an artificially-widened lock-hold window so two processes
    racing on the same shard are highly likely to actually contend rather
    than happen to interleave without ever overlapping.
    \"\"\"
    import sys, time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from coordinator_core import tracker_store as ts

    repo_root = Path(sys.argv[2])
    prefix = sys.argv[3]
    count = int(sys.argv[4])
    delay = float(sys.argv[5])

    _orig_locked_rmw = ts.locked_rmw

    def _slow_locked_rmw(target, mutate, **kwargs):
        def _slow_mutate(old_text):
            time.sleep(delay)
            return mutate(old_text)
        return _orig_locked_rmw(target, _slow_mutate, **kwargs)

    ts.locked_rmw = _slow_locked_rmw

    for i in range(count):
        ts.append_event(
            {"id": f"{prefix}-{i}", "observed_at": "2026-01-01T00:00:00Z"},
            repo_root=repo_root,
        )
""")


class TestAC1ConcurrentSingleShardWriters:
    def test_two_processes_appending_same_shard_no_lost_updates_no_gaps(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        script = tmp_path / "concurrent_appender.py"
        script.write_text(_CONCURRENT_APPENDER_SCRIPT, encoding="utf-8")

        per_process = 5
        p1 = subprocess.Popen(
            [sys.executable, str(script), _PROJECT_ROOT, str(repo), "p1", str(per_process), "0.05"],
            creationflags=_NO_WINDOW,
        )
        p2 = subprocess.Popen(
            [sys.executable, str(script), _PROJECT_ROOT, str(repo), "p2", str(per_process), "0.05"],
            creationflags=_NO_WINDOW,
        )
        p1.wait(timeout=60)
        p2.wait(timeout=60)
        assert p1.returncode == 0
        assert p2.returncode == 0

        shard = shard_path(repo)
        records = _read_raw_lines(shard)

        assert len(records) == per_process * 2, "some appends were lost"

        ids = [r["id"] for r in records]
        assert len(set(ids)) == len(ids), f"duplicate ids in shard: {ids}"

        sequences = sorted(r["sequence"] for r in records)
        assert sequences == list(range(1, per_process * 2 + 1)), (
            f"sequence is not gap-free / strictly increasing: {sequences}"
        )


# ---------------------------------------------------------------------------
# AC1b — cross-machine merge safety (the regression this plan was amended for)
# ---------------------------------------------------------------------------


class TestAC1bCrossMachineMergeSafety:
    def test_merge_of_two_offline_shards_is_deterministic_regardless_of_order(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        # Two machines, built independently ("offline"), each with its own
        # monotonic per-shard sequence. Chronological (applied_at) truth is
        # interleaved: a1(:01), b1(:02), a2(:03), b2(:04).
        shard_a_records = [
            {**_event("evt-a1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"), "sequence": 1, "machine": "host-zeta"},
            {**_event("evt-a2", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:03Z"), "sequence": 2, "machine": "host-zeta"},
        ]
        shard_b_records = [
            {**_event("evt-b1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:02Z"), "sequence": 1, "machine": "host-alpha"},
            {**_event("evt-b2", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:04Z"), "sequence": 2, "machine": "host-alpha"},
        ]

        expected_order = ["evt-a1", "evt-b1", "evt-a2", "evt-b2"]

        # A naive "single shared file" implementation that only concatenates
        # in shard-glob (i.e. filename-alphabetical) order, without the final
        # cross-shard chronological sort, gets this wrong — proving the
        # fixture actually discriminates. host-alpha sorts before host-zeta,
        # so naive concatenation yields b1,b2,a1,a2.
        naive_order = [r["id"] for r in shard_b_records] + [r["id"] for r in shard_a_records]
        assert naive_order != expected_order, (
            "fixture does not discriminate naive concat-only merge from correct order"
        )

        # Build in one filename order (alpha before zeta on disk)...
        _write_shard(repo, "host-alpha", shard_b_records)
        _write_shard(repo, "host-zeta", shard_a_records)
        result_1 = [r["id"] for r in read_events(repo_root=repo)]

        # ...then rebuild with the opposite write/merge order, and reversed
        # glob-visitation isn't possible to force directly (glob is always
        # alphabetical) but we can flip which shard is written last, and we
        # can additionally verify sequence duplication across shards (both
        # shards use sequence 1 and 2) is tolerated without error.
        import shutil
        shutil.rmtree(repo / EVENTS_DIR_RELPATH)
        _write_shard(repo, "host-zeta", shard_a_records)
        _write_shard(repo, "host-alpha", shard_b_records)
        result_2 = [r["id"] for r in read_events(repo_root=repo)]

        assert result_1 == expected_order
        assert result_2 == expected_order
        assert result_1 == result_2, "merge result depends on write/merge order"

        # Duplicate sequence values across shards are expected, not an error.
        all_events = read_events(repo_root=repo)
        sequences = [e["sequence"] for e in all_events]
        assert sequences.count(1) == 2 and sequences.count(2) == 2

        # Zero git-conflict shape: both shards are disjoint files under
        # EVENTS_DIR_RELPATH, so a git merge of two branches each holding one
        # shard can never produce a textual conflict — assert the two shard
        # files are in fact physically distinct paths.
        assert shard_path(repo, machine="host-alpha") != shard_path(repo, machine="host-zeta")


# ---------------------------------------------------------------------------
# AC1c — append_event writes ONLY the calling machine's shard
# ---------------------------------------------------------------------------


def _run_ac1c_peer_shard_untouched_check(tmp_path, monkeypatch) -> None:
    """Shared body for AC1c's own test and its AC2 re-run
    (``TestFoldObservedSetAC2AppendEventUnmodified``) — factored so an edit
    to either assertion updates both call sites (Finding 4, code-reviewer,
    2026-07-28: the two were previously hand-copied and could silently
    drift apart)."""
    repo = _make_git_repo(tmp_path / "repo")

    peer_path = _write_shard(
        repo, "peer-machine", [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-machine"}]
    )
    peer_bytes_before = peer_path.read_bytes()
    peer_mtime_before = peer_path.stat().st_mtime_ns

    monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

    result = append_event(_event("own-evt-1", "2026-01-01T00:00:01Z"), repo_root=repo)

    assert result["machine"] == "this-machine"
    own_path = shard_path(repo, machine="this-machine")
    assert own_path.exists()
    assert own_path != peer_path

    assert peer_path.read_bytes() == peer_bytes_before, "peer shard bytes changed"
    assert peer_path.stat().st_mtime_ns == peer_mtime_before, "peer shard mtime changed"


class TestAC1cWritesOnlyOwnShard:
    def test_peer_shard_bytes_and_mtime_unchanged(self, tmp_path, monkeypatch):
        _run_ac1c_peer_shard_untouched_check(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# AC1d — read_events FILTERS OUT applied_at:null events entirely
# ---------------------------------------------------------------------------


class TestAC1dNullAppliedAtFilteredNotSorted:
    def test_null_applied_at_event_is_absent_from_result(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        records = [
            {**_event("evt-suggest", "2026-01-01T00:00:00Z", applied_at=None), "sequence": 1, "machine": "m1", "tier": "suggest"},
            {**_event("evt-applied", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:05Z"), "sequence": 2, "machine": "m1"},
        ]
        _write_shard(repo, "m1", records)

        # A naive null-safe sort (placing null applied_at last, rather than
        # excluding it) would still include evt-suggest in the result —
        # proving the fixture discriminates filter-vs-sort.
        naive_result = sorted(
            records,
            key=lambda e: ((e.get("applied_at") is None), e.get("applied_at") or "", e.get("observed_at"), e.get("id")),
        )
        naive_ids = [r["id"] for r in naive_result]
        assert "evt-suggest" in naive_ids, (
            "fixture does not discriminate a null-safe sort from the ratified filter"
        )

        result = read_events(repo_root=repo)
        result_ids = [e["id"] for e in result]

        assert "evt-suggest" not in result_ids, "null-applied_at event was not filtered out"
        assert result_ids == ["evt-applied"]


# ---------------------------------------------------------------------------
# AC1e — id/observed_at validation; cross-machine duplicate id NOT caught
# ---------------------------------------------------------------------------


class TestAC1eValidationAndDuplicateIdBounds:
    def test_missing_id_rejected(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        with pytest.raises(TrackerStoreError):
            append_event({"observed_at": "2026-01-01T00:00:00Z"}, repo_root=repo)

    def test_missing_observed_at_rejected(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        with pytest.raises(TrackerStoreError):
            append_event({"id": "evt-1"}, repo_root=repo)

    def test_same_shard_duplicate_id_rejected(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-dup", "2026-01-01T00:00:00Z"), repo_root=repo)
        with pytest.raises(TrackerStoreError):
            append_event(_event("evt-dup", "2026-01-01T00:00:01Z"), repo_root=repo)

    def test_cross_machine_duplicate_id_is_explicitly_not_caught(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-a")
        append_event(_event("evt-shared", "2026-01-01T00:00:00Z"), repo_root=repo)

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-b")
        # This is the documented gap: append_event has no visibility into
        # peer shards, so a duplicate id minted independently on a different
        # machine is NOT rejected here.
        result = append_event(_event("evt-shared", "2026-01-01T00:00:01Z"), repo_root=repo)
        assert result["id"] == "evt-shared"
        assert result["machine"] == "machine-b"

        shard_a = _read_raw_lines(shard_path(repo, machine="machine-a"))
        shard_b = _read_raw_lines(shard_path(repo, machine="machine-b"))
        assert any(r["id"] == "evt-shared" for r in shard_a)
        assert any(r["id"] == "evt-shared" for r in shard_b)


    def test_bare_digit_id_rejected(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        with pytest.raises(TrackerStoreError):
            append_event(_event("123", "2026-01-01T00:00:00Z"), repo_root=repo)

    def test_ordinary_non_digit_id_accepted(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        result = append_event(_event("note-1", "2026-01-01T00:00:00Z"), repo_root=repo)
        assert result["id"] == "note-1"


# ---------------------------------------------------------------------------
# Malformed-tail-line raise path (append_event / max_sequence)
# ---------------------------------------------------------------------------


class TestMalformedTailLineRaisesTrackerStoreError:
    def test_append_event_raises_on_non_json_tail_line(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not valid json\n", encoding="utf-8")

        with pytest.raises(TrackerStoreError):
            append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)

    def test_append_event_raises_on_non_object_tail_line(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[1, 2, 3]\n", encoding="utf-8")

        with pytest.raises(TrackerStoreError):
            append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)

    def test_max_sequence_raises_on_non_json_tail_line(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not valid json\n", encoding="utf-8")

        with pytest.raises(TrackerStoreError):
            max_sequence(repo_root=repo)

    def test_max_sequence_raises_on_non_object_tail_line(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('"just a string"\n', encoding="utf-8")

        with pytest.raises(TrackerStoreError):
            max_sequence(repo_root=repo)


# ---------------------------------------------------------------------------
# read_events wraps a malformed non-tail line into TrackerStoreError
# ---------------------------------------------------------------------------


class TestReadEventsMalformedLineRaisesTrackerStoreError:
    def test_non_json_non_tail_line_raises(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {**_event("evt-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"), "sequence": 1, "machine": "m1"},
            sort_keys=True,
        )
        target.write_text("not valid json\n" + good + "\n", encoding="utf-8")

        with pytest.raises(TrackerStoreError):
            read_events(repo_root=repo)

    def test_non_object_non_tail_line_raises(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(
            {**_event("evt-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"), "sequence": 1, "machine": "m1"},
            sort_keys=True,
        )
        target.write_text("[1, 2, 3]\n" + good + "\n", encoding="utf-8")

        with pytest.raises(TrackerStoreError):
            read_events(repo_root=repo)


# ---------------------------------------------------------------------------
# AC1f — retroactive-reorder characterization
# ---------------------------------------------------------------------------


class TestAC1fRetroactiveReorderCharacterization:
    def test_merging_earlier_applied_at_shifts_prior_events_position(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        _write_shard(
            repo,
            "m1",
            [
                {**_event("evt-mid", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:02Z"), "sequence": 1, "machine": "m1"},
                {**_event("evt-late", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:04Z"), "sequence": 2, "machine": "m1"},
            ],
        )

        before = [e["id"] for e in read_events(repo_root=repo)]
        assert before == ["evt-mid", "evt-late"]
        position_before = before.index("evt-mid")

        # Merge a shard with an EARLIER applied_at than already-observed events.
        _write_shard(
            repo,
            "m2",
            [
                {**_event("evt-early", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"), "sequence": 1, "machine": "m2"},
            ],
        )

        after = [e["id"] for e in read_events(repo_root=repo)]
        assert after == ["evt-early", "evt-mid", "evt-late"]
        position_after = after.index("evt-mid")

        assert position_after != position_before, (
            "evt-mid's global read-order position did not shift after a "
            "retroactive earlier-applied_at merge — the ordering contract is "
            "deterministic-given-a-fixed-shard-set, NOT append-monotonic-"
            "prefix-stable, and this must be visible"
        )


# ---------------------------------------------------------------------------
# AC1g — filter-is-on-applied_at-not-tier trap (cockpit-named)
# ---------------------------------------------------------------------------


class TestAC1gFilterIsOnAppliedAtNotTier:
    def test_tier_auto_with_applied_at_projects_without_is_excluded(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        # A tier:auto event that never stamps applied_at (e.g. auto-tiered
        # but not yet reconciled) — written via the append_event writer path.
        not_yet_applied = append_event(
            _event("evt-auto-unapplied", "2026-01-01T00:00:00Z", tier="auto"),
            repo_root=repo,
        )
        assert not_yet_applied.get("applied_at") is None

        result_before = [e["id"] for e in read_events(repo_root=repo)]
        assert "evt-auto-unapplied" not in result_before, (
            "a tier:auto event without applied_at silently projected — the "
            "filter must key on applied_at, not on tier"
        )

        # A tier:auto event that DOES carry applied_at at write time (the
        # caller has already reconciled it) — same writer path, one extra key.
        applied = append_event(
            _event("evt-auto-applied", "2026-01-01T00:00:01Z", applied_at="2026-01-01T00:00:02Z", tier="auto"),
            repo_root=repo,
        )
        assert applied.get("applied_at") == "2026-01-01T00:00:02Z"

        result_after = [e["id"] for e in read_events(repo_root=repo)]
        assert "evt-auto-unapplied" not in result_after, "still must not project"
        assert "evt-auto-applied" in result_after, "applied tier:auto event must project"


# ---------------------------------------------------------------------------
# AC1h — logical_clock carried, non-authoritative
# ---------------------------------------------------------------------------


def _run_ac1h_c_no_peer_read_check(tmp_path, monkeypatch) -> None:
    """Shared body for AC1h(c)'s own test and its AC2 re-run
    (``TestFoldObservedSetAC2AppendEventUnmodified``) — factored so an edit
    to either assertion updates both call sites (Finding 4, code-reviewer,
    2026-07-28)."""
    repo = _make_git_repo(tmp_path / "repo")

    peer_path = _write_shard(
        repo, "peer-machine", [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-machine"}]
    )
    peer_bytes_before = peer_path.read_bytes()
    peer_mtime_before = peer_path.stat().st_mtime_ns

    monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")
    append_event(_event("own-evt-1", "2026-01-01T00:00:01Z"), repo_root=repo)

    assert peer_path.read_bytes() == peer_bytes_before, (
        "peer shard bytes changed while stamping logical_clock — "
        "append_event must not perform a cross-shard read"
    )
    assert peer_path.stat().st_mtime_ns == peer_mtime_before


class TestAC1hLogicalClockCarriedNotAuthoritative:
    def test_clock_monotonic_including_same_ms_bump_and_backwards_step(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")

        clock_values = iter([1_000_000, 1_000_000, 999_000, 1_000_500])
        monkeypatch.setattr(ts, "_now_ms", lambda: next(clock_values))

        e1 = append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)
        e2 = append_event(_event("evt-2", "2026-01-01T00:00:01Z"), repo_root=repo)
        e3 = append_event(_event("evt-3", "2026-01-01T00:00:02Z"), repo_root=repo)
        e4 = append_event(_event("evt-4", "2026-01-01T00:00:03Z"), repo_root=repo)

        assert e1["logical_clock"] == {"wall_ms": 1_000_000, "counter": 0}
        # Same millisecond as tail -> counter bumps, wall_ms unchanged.
        assert e2["logical_clock"] == {"wall_ms": 1_000_000, "counter": 1}
        # Backwards clock step (999_000 < tail 1_000_000) -> wall_ms holds at
        # tail's value (max(now, tail)), counter keeps bumping.
        assert e3["logical_clock"] == {"wall_ms": 1_000_000, "counter": 2}
        # Clock moves forward again -> wall_ms advances, counter resets to 0.
        assert e4["logical_clock"] == {"wall_ms": 1_000_500, "counter": 0}

        clocks = [e["logical_clock"] for e in (e1, e2, e3, e4)]
        tuples = [(c["wall_ms"], c["counter"]) for c in clocks]
        assert all(b >= a for a, b in zip(tuples, tuples[1:])), "wall_ms ever decreased"
        assert tuples == sorted(tuples), "logical_clock is not monotonic per shard"

    def test_permuted_logical_clock_does_not_change_read_order(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        records = [
            {
                **_event("evt-first", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"),
                "sequence": 1,
                "machine": "m1",
                # Deliberately disagrees with applied_at order: this clock is
                # LATER than evt-second's, yet evt-first must still sort first.
                "logical_clock": {"wall_ms": 9_999_999, "counter": 9},
            },
            {
                **_event("evt-second", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:02Z"),
                "sequence": 2,
                "machine": "m1",
                "logical_clock": {"wall_ms": 1, "counter": 0},
            },
        ]
        _write_shard(repo, "m1", records)

        result_ids = [e["id"] for e in read_events(repo_root=repo)]
        assert result_ids == ["evt-first", "evt-second"], (
            "read_events order changed when logical_clock disagreed with "
            "applied_at — logical_clock must be INERT in the sort key"
        )

    def test_append_event_reads_no_shard_but_its_own_while_stamping_clock(self, tmp_path, monkeypatch):
        _run_ac1h_c_no_peer_read_check(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# AC2 — crash-safety regression over locked_rmw
# ---------------------------------------------------------------------------

_HOLD_FOREVER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    sys.path.insert(0, sys.argv[2])
    from coordinator_core.locked_write import _plat_try_lock

    lock_path = sys.argv[1]
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert _plat_try_lock(fd), "holder could not acquire the lock"
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    while True:
        time.sleep(10)
""")


class TestAC2CrashSafety:
    def test_killed_holder_does_not_wedge_next_appender(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = shard_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({**_event("evt-0", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "m"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        import hashlib
        from coordinator_core.lifecycle import git_common_dir

        key = hashlib.sha1(os.path.realpath(str(target)).encode()).hexdigest()
        lock_dir = git_common_dir(repo) / "coordinator-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{key}.lock"

        holder_script = tmp_path / "hold_forever.py"
        holder_script.write_text(_HOLD_FOREVER_SCRIPT, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(holder_script), str(lock_path), _PROJECT_ROOT],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        try:
            line = proc.stdout.readline()
            assert line.strip() == b"LOCKED"

            proc.kill()
            proc.wait()

            result = append_event(_event("evt-1", "2026-01-01T00:00:01Z"), repo_root=repo)
            assert result["sequence"] == 2

            records = _read_raw_lines(target)
            assert len(records) == 2
            assert [r["id"] for r in records] == ["evt-0", "evt-1"]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# AC4 — placement + git-trackability + C11 durable-data-plane isolation
# ---------------------------------------------------------------------------


class TestAC4PlacementAndDurablePlaneIsolation:
    def test_shard_created_under_events_dir_relpath_and_is_git_trackable(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        result = append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)
        machine = result["machine"]

        expected = repo / EVENTS_DIR_RELPATH / f"events.{machine}.jsonl"
        assert expected.exists()
        assert expected == shard_path(repo)

        # git-trackable: not under .git, and `git add` succeeds without error.
        assert ".git" not in expected.relative_to(repo).parts
        subprocess.run(
            ["git", "add", str(expected)],
            cwd=str(repo),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            check=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
        assert "events." in status.stdout

    def test_settings_home_durable_data_plane_untouched(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        fake_settings_home = tmp_path / "fake-settings-home"
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(fake_settings_home))

        append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)
        read_events(repo_root=repo)
        max_sequence(repo_root=repo)

        assert not fake_settings_home.exists(), (
            "tracker_store wrote under the C11 settings-home durable-data "
            "plane — DEC-8 requires sat-01 to leave it untouched"
        )


# ---------------------------------------------------------------------------
# AC9 (sat-01b) — affirmation-era guard: bounded registration, not zero
#   registration.
#
# Successor to sat-01's TestAC7NoOpRegistrationLiveGuard (formerly here,
# grep this name if you followed a stale reference). That guard enforced
# DEC-4's "zero ops registered until DR-241's five bounds are affirmed
# against real handler code" by forbidding ANY coordinator_core/ops/
# reference to tracker_store outright. sat-01b C4 lands that affirmation —
# see docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
# § "Amendment (2026-07-28) — the five-bound affirmation lands early, at
# sat-01b, not sat-06" — so the predicate the old guard protected is now
# MET, and the guard is replaced (never deleted, per this plan's Anti-scope
# and AC9) with one that enforces the bounds themselves: an explicit,
# exact-match allowlist of referencing modules; MUTATING (never
# COMPUTE_ONLY) classification for any tracker.* op; write-target
# confinement to state/sovereign-tracker/ reached only via tracker_store's
# own EVENTS_DIR_RELPATH constant; and per-repo common_dir scoping with no
# UDS/HTTP exposure. Every assertion below is phrased "if such a thing
# exists, it must satisfy X" — sat-01b C4 lands BEFORE C5 registers the op,
# so the allowlisted files do not exist on disk yet; the guard must stay
# green both now (nothing registered) and after C5 lands (the op
# registered) — see docs/plans/2026-07-28-sat-01b-observed-set-fold-
# actuator.md § Tasks C4/C5 and § Acceptance Criteria AC9.
# ---------------------------------------------------------------------------

# Exact-match allowlist of tracker_store referencers permitted across BOTH
# scanned surfaces — coordinator_core/ops/**/*.py (recursive) and top-level
# coordinator_core/*.py (non-recursive) — expressed as POSIX-style paths
# relative to the repo root (Windows/macOS both first-class — compare via
# as_posix(), never a raw os.sep join). Adding a new store-writing op or a
# new top-level referencer module requires a FRESH DR-241 bounds affirmation
# against that code (see docs/decisions/DR-241-sovereign-tracker-substrate-
# write-carveout.md § Amendment), not an allowlist edit made to go green —
# see the failure messages below.
#
# sat-02 C6 widened this guard's WALK (not just this set) to also see
# top-level coordinator_core/*.py modules — VERIFIED ON DISK ahead of C6:
# the walk previously enumerated ONLY coordinator_core/ops/**/*.py, so
# tracker_entities.py/tracker_projection.py (both top-level, sat-02 C1-C4)
# sat entirely outside it. Landing DR-241's C5 affirmation with no
# enforcing guard behind those two modules would be prose with no
# mechanism — the posture DR-241's own D1 records as refused.
# sat-03 (2026-08-11) added tracker_transitions.py, a new top-level module
# emitting transition/reopen/snapshot events via append_event/append_events.
# Backed by a fresh DR-241 bound-by-bound affirmation, not a bare widening —
# see docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
# § Amendment (2026-08-11) — sat-03's transition-event vocabulary affirmed
# bound-by-bound.
#
# sat-06 (2026-08-20) added coordinator_core/ops/tracker/push_suggestion.py
# — C4's producer-facing tracker.push_suggestion op, the seam a foreign
# cross-language process (cockpit) uses to push one sovereign-tracker
# item-event bundle in. RULING 2026-08-20 ("C4 mints the event id; only
# then is the DR-241 affirmation honest") required claude-klabauter to mint the event
# id itself before this affirmation could be truthfully written — see that
# ruling in docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md and
# push_suggestion.py's own `_mint_event_id`. Fresh DR-241 bound-by-bound
# affirmation against push_suggestion.py's real handler code, in the same
# shape as the `## Amendment (2026-08-05)` and `## Amendment (2026-08-11)`
# tables in docs/decisions/DR-241-sovereign-tracker-substrate-write-
# carveout.md:
#
#   (i)   id content-derived AND globally unique — `push_suggestion.py ::
#         _mint_event_id` mints `evt-<machine_slug()>-<digest12>`,
#         `digest12` a SHA-256 hexdigest prefix (12 hex chars) over the
#         canonical JSON of the caller's event content plus a fresh
#         microsecond timestamp folded in as the per-event nonce.
#         Content-derived (digest over the event) satisfies the base half;
#         the `<machine>-` component satisfies the global-uniqueness half.
#         A caller-supplied `event.id` is refused loud
#         (`PushSuggestionMalformedError`), never silently overwritten —
#         `push_suggestion.py`'s handler body, the `if "id" in event:`
#         check immediately after the event-shape validation.
#   (ii)  commutative modulo total order — this op performs exactly ONE
#         `tracker_store.append_event` call per invocation (local arm,
#         `_write_local`) or delivers exactly one envelope (peer arm,
#         `_deliver_envelope`) — never a batch, never a re-ordering of
#         prior events; `append_event`'s own sequence/logical-clock bump
#         governs ordering unchanged, this op adds no second ordering
#         mechanism.
#   (iii) git-reversible — the local arm is append-only via
#         `tracker_store.append_event` (no in-place mutation); the peer
#         arm's envelope is a plain committed file addition
#         (`_commit_envelope`), reversible by `git revert` like any other
#         commit.
#   (iv)  no terminality-re-verify — this op never reads an item's prior
#         terminal state before writing; it is a pure forward append/
#         delivery.
#   (v)   in-process command-type dispatch only — registered as a
#         command-type op via `@register_op("tracker.push_suggestion")`; no
#         UDS/HTTP surface exists in this repo (see
#         `test_no_uds_or_http_surface_exists_to_expose_tracker_ops` below).
#
# Confinement of the write target: the local arm calls
# `tracker_store.append_event` only (never a hand-built `state/`
# literal — see `test_allowlisted_referencers_confine_writes_via_tracker_
# store_api_only` below); the peer arm writes only under the resolved
# receiver's `cross-repo/inbox/`, never `state/sovereign-tracker/` in a
# peer tree (module docstring negative-spec, DR-338 D4).
_ALLOWED_TRACKER_STORE_REFERENCERS = frozenset(
    {
        "coordinator_core/ops/tracker/fold_observed_set.py",
        "coordinator_core/ops/session/boot_sweep.py",
        "coordinator_core/ops/tracker/push_suggestion.py",
        "coordinator_core/tracker_entities.py",
        "coordinator_core/tracker_projection.py",
        "coordinator_core/tracker_transitions.py",
    }
)


def _executable_lines(text: str) -> "list[tuple[int, str]]":
    """Return ``(lineno, source)`` for lines carrying EXECUTABLE code only.

    Docstrings and comments are excluded. The write-target confinement bound
    below constrains what an op module *does*, never what it *says*: a module
    that names its own store directory in a docstring is documenting the
    surface it is confined to, which is the opposite of a confinement
    violation. Scanning raw file text instead would force sanctioned modules
    to write around their own domain vocabulary — the prose-false-positive
    shape this repo has been burned by before, and one that fights the
    RAG-bait-at-structural-boundaries requirement in CLAUDE.md.

    Comments never appear in the AST at all, so they fall out for free; a
    docstring is any string-literal expression opening a module, class, or
    function body. A file that does not parse propagates ``SyntaxError``
    straight out of this function — surfacing as a pytest ERROR rather than
    an empty list, since a syntax-invalid module under ``coordinator_core/
    ops/`` is a loud failure this repo wants, not a silently-swallowed case.
    (Review: code-reviewer, Finding 3, 2026-07-28 — this docstring formerly
    claimed the opposite: a "yields no executable lines" graceful return the
    code has never implemented.)
    """
    tree = ast.parse(text)

    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        head = body[0]
        if isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant):
            if isinstance(head.value.value, str):
                end = head.value.end_lineno or head.value.lineno
                docstring_lines.update(range(head.value.lineno, end + 1))
        # PEP-257 "attribute docstring" convention (a bare string Expr
        # immediately following an Assign/AnnAssign in the SAME body) —
        # sat-02 C6 latent-bug fix: surfaced by widening this guard's walk
        # onto tracker_entities.py, whose module-level constants (e.g.
        # EVENT_KINDS) carry exactly this Sphinx-recognized shape. Without
        # this, such a string is ordinary executable text to this scanner
        # and a prose mention of "sovereign-tracker" inside it false-
        # positives the confinement ban this function's own docstring
        # above says such mentions are exempt from.
        for prev_stmt, cur_stmt in zip(body, body[1:]):
            if (
                isinstance(prev_stmt, (ast.Assign, ast.AnnAssign))
                and isinstance(cur_stmt, ast.Expr)
                and isinstance(cur_stmt.value, ast.Constant)
                and isinstance(cur_stmt.value.value, str)
            ):
                end = cur_stmt.value.end_lineno or cur_stmt.value.lineno
                docstring_lines.update(range(cur_stmt.value.lineno, end + 1))

    executable: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if lineno in docstring_lines:
            continue
        if line.strip().startswith("#"):
            continue
        executable.append((lineno, line))
    return executable


def _tracker_scope_string_literals(
    tree: "ast.Module", executable_linenos: "set[int]"
) -> "list[ast.Constant]":
    """Return every string-literal ``Constant`` node in *tree* that sits in
    the same top-level SCOPE — a function/async-function body (including any
    nested helpers defined within it), or the module's own top-level
    statements outside any ``def``/``class`` — as a reference to the
    tracker/sovereign-tracker surface.

    "Reference" means: a ``Name`` or ``Attribute`` whose identifier contains
    "tracker" (case-insensitive); an ``import``/``from ... import`` whose
    module path or bound name contains "tracker"; or a string literal that
    itself contains "tracker".

    This is deliberately SCOPE-based, not line-based (Review: code-reviewer
    Finding 1, 2026-07-28): a same-line-co-location check misses an
    offending literal one line away from the tracker-touching statement in
    the same function — e.g. ``target = "archive/legacy"`` immediately
    followed by ``tracker_store.append_event(evt, repo_root=target)``. Both
    statements share this function's scope, so the literal is caught here
    even though it never appears on a line mentioning "tracker" itself.
    Docstring/comment literals are excluded via *executable_linenos*
    (``_executable_lines``' own docstring-exclusion set) — naming the store
    in documentation is not a confinement violation.
    """

    def _mentions_tracker(node: "ast.AST") -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and "tracker" in n.id.lower():
                return True
            if isinstance(n, ast.Attribute) and "tracker" in n.attr.lower():
                return True
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = getattr(n, "module", None) or ""
                if "tracker" in mod.lower():
                    return True
                for alias in n.names:
                    bound_name = alias.asname or alias.name
                    if "tracker" in bound_name.lower():
                        return True
            if (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and "tracker" in n.value.lower()
            ):
                return True
        return False

    def _string_literals(node: "ast.AST"):
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and n.lineno in executable_linenos
            ):
                yield n

    literals: "list[ast.Constant]" = []

    module_level_stmts = [
        stmt
        for stmt in tree.body
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if module_level_stmts and any(_mentions_tracker(s) for s in module_level_stmts):
        for stmt in module_level_stmts:
            literals.extend(_string_literals(stmt))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _mentions_tracker(node):
                literals.extend(_string_literals(node))

    return literals


def _references_tracker_store_in_code(text: str) -> bool:
    """Return whether *text* references ``tracker_store`` from EXECUTABLE
    code — an import, a ``Name``/``Attribute`` access — as opposed to merely
    naming it in a docstring or comment.

    Needed for the top-level ``coordinator_core/*.py`` scan (sat-02 C6):
    unlike the pre-existing ``coordinator_core/ops/`` walk (a raw whole-file
    substring check, left unchanged here), the top-level surface includes
    modules such as ``ipc.py`` whose long-form "why" docstring NAMES
    ``tracker_store`` for narrative/backlink purposes without importing or
    calling it — a raw substring check over such a file's whole text would
    flag it as a false-positive offender. Reuses ``_executable_lines``'
    docstring/comment-exclusion set so the two confinement checks in this
    module and this reference check apply an identical "documentation is
    not code" discipline.
    """
    tree = ast.parse(text)
    executable_linenos = {lineno for lineno, _ in _executable_lines(text)}

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            if "tracker_store" in mod:
                return True
            for alias in node.names:
                # Check the ORIGINAL imported name independently of any
                # `as` alias (Review: code-reviewer, Finding 1, 2026-08-05):
                # `from coordinator_core import tracker_store as ts` binds
                # "ts" locally, but the import itself still references
                # tracker_store and must not become invisible to this guard
                # just because the caller renamed its local handle.
                if "tracker_store" in alias.name:
                    return True
                bound_name = alias.asname or alias.name
                if "tracker_store" in bound_name:
                    return True
        elif isinstance(node, ast.Name):
            if "tracker_store" in node.id and node.lineno in executable_linenos:
                return True
        elif isinstance(node, ast.Attribute):
            if "tracker_store" in node.attr and node.lineno in executable_linenos:
                return True
    return False


def _tracker_store_referencer_offenders(
    scan_dir: Path,
    repo_root: Path,
    allowed: "frozenset[str]",
    *,
    recursive: bool,
    use_ast_check: bool = False,
) -> "list[str]":
    """Return the sorted list of *.py files under *scan_dir* that reference
    ``tracker_store`` but are not in *allowed* (paths relative to
    *repo_root*, POSIX-style).

    Shared by both scans this guard runs — the pre-existing recursive
    ``coordinator_core/ops/`` walk and the sat-02 C6-added non-recursive
    top-level ``coordinator_core/*.py`` walk — so both real-repo tests below
    and ``TestTopLevelWalkBiteTest``'s synthetic tmp_path fixtures exercise
    IDENTICAL detection logic (reuses ``_confinement_check_for_file``'s own
    real-vs-synthetic sharing discipline, first established at sat-01b for
    exactly this "a comment claimed a bite-test existed; none did" failure
    mode — this module reapplies that discipline, it did not invent it).

    *use_ast_check* selects ``_references_tracker_store_in_code`` (docstring-
    aware) over the plain whole-file substring check the ops/ walk has used
    since sat-01b — the top-level scan needs the AST-aware form to avoid a
    false positive on a module (e.g. ``ipc.py``) that only NAMES
    ``tracker_store`` in a narrative docstring/backlink comment, never in
    executable code.
    """
    offenders: "list[str]" = []
    iterator = scan_dir.rglob("*.py") if recursive else scan_dir.glob("*.py")
    for py_file in iterator:
        text = py_file.read_text(encoding="utf-8")
        if use_ast_check:
            referenced = "tracker_store" in text and _references_tracker_store_in_code(
                text
            )
        else:
            referenced = "tracker_store" in text
        if not referenced:
            continue
        rel = py_file.relative_to(repo_root).as_posix()
        if rel not in allowed:
            offenders.append(rel)
    return sorted(offenders)


def _confinement_check_for_file(path: Path, rel: str) -> None:
    """Run DR-241's write-target-confinement bound checks against *path*
    (labeled *rel* in failure messages): the unconditional 'sovereign-
    tracker' literal ban, plus the AST-scope 'state/'/'archive/' literal
    ban (see ``_tracker_scope_string_literals``). Raises ``AssertionError``
    on the first violation found.

    Shared by ``test_allowlisted_referencers_confine_writes_via_tracker_
    store_api_only`` (run against the real allowlisted files) and
    ``TestWriteTargetConfinementBiteTest`` (run against synthetic
    tmp_path fixtures) so both exercise IDENTICAL check logic — Finding 2
    (code-reviewer, 2026-07-28): a comment used to CLAIM a bite-test proved
    this logic fires; none existed. ``TestWriteTargetConfinementBiteTest``
    below is that proof, and this factoring is what lets it prove the
    real check, not a reimplementation of it.
    """
    text = path.read_text(encoding="utf-8")
    code_lines = _executable_lines(text)
    executable_linenos = {lineno for lineno, _ in code_lines}

    for lineno, line in code_lines:
        assert "sovereign-tracker" not in line and "sovereign_tracker" not in line, (
            f"{rel}:{lineno} hardcodes a 'sovereign-tracker' literal in "
            f"executable code instead of importing "
            f"tracker_store.EVENTS_DIR_RELPATH: {line.strip()!r} — "
            "DR-241's write-target confinement bound requires the "
            "sanctioned write target be reached ONLY through "
            "tracker_store's own API/constant, never a duplicated "
            "literal. (Naming the store in a docstring or comment is "
            "documentation, not a confinement violation, and is "
            "deliberately not scanned.)"
        )

    tree = ast.parse(text)
    for literal_node in _tracker_scope_string_literals(tree, executable_linenos):
        value = literal_node.value
        assert "state/" not in value, (
            f"{rel}:{literal_node.lineno} hand-builds a state/ path literal "
            f"reachable from tracker-related code instead of importing "
            f"tracker_store.EVENTS_DIR_RELPATH: {value!r} — DR-241's "
            "write-target confinement bound requires the sanctioned write "
            "target be reached ONLY through tracker_store's own "
            "API/constant. Reachability is scoped via AST (same enclosing "
            "function/module-level block as a tracker reference), not "
            "same-line co-location, so a violating literal on its own line "
            "is still caught."
        )
        assert "archive/" not in value, (
            f"{rel}:{literal_node.lineno} references archive/ reachable "
            f"from tracker-related code: {value!r} — DR-241's write-target "
            "confinement bound forbids any tracker.* op from touching "
            "archive/ or any state/ subtree other than "
            "state/sovereign-tracker/ (reached via EVENTS_DIR_RELPATH)."
        )


class TestAffirmationEraBoundedRegistrationGuard:
    """Enforces DR-241's five affirmed bounds against any tracker_store
    referencer under coordinator_core/ops/ AND top-level coordinator_core/
    (sat-02 C6 widened the walk to also see the latter — the ops/ tree alone
    never reached tracker_entities.py/tracker_projection.py), in place of
    sat-01's blanket zero-registration guard (see module-comment above this
    class for the full replacement rationale and the DR-241/plan
    citations)."""

    # -- Bound: bounded referencer set --------------------------------
    def test_ops_tree_referencers_are_exact_match_allowlisted(self):
        ops_dir = Path(_PROJECT_ROOT) / "coordinator_core" / "ops"
        repo_root = Path(_PROJECT_ROOT)
        offenders = _tracker_store_referencer_offenders(
            ops_dir, repo_root, _ALLOWED_TRACKER_STORE_REFERENCERS, recursive=True
        )
        assert offenders == [], (
            "coordinator_core/ops/ module(s) reference tracker_store outside "
            "the DR-241-affirmed allowlist "
            f"({sorted(_ALLOWED_TRACKER_STORE_REFERENCERS)}): {offenders}. "
            "Adding a new store-writing op requires a fresh DR-241 five-bound "
            "affirmation against that op's real handler code (see "
            "docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md "
            "§ Amendment) — do not just widen this allowlist to go green."
        )

    # -- Bound: bounded referencer set, top-level surface (sat-02 C6) -
    def test_top_level_coordinator_core_referencers_are_exact_match_allowlisted(self):
        # Non-recursive by design: coordinator_core/ops/**/*.py is already
        # covered by test_ops_tree_referencers_are_exact_match_allowlisted
        # above; a recursive top-level scan would double-scan that subtree
        # under a different iteration order for no new coverage. This scan
        # exists because tracker_entities.py/tracker_projection.py (both
        # top-level, sat-02 C1-C4) sit OUTSIDE the ops/ walk entirely —
        # VERIFIED ON DISK ahead of C6 — so no guard previously saw them.
        top_dir = Path(_PROJECT_ROOT) / "coordinator_core"
        repo_root = Path(_PROJECT_ROOT)
        offenders = _tracker_store_referencer_offenders(
            top_dir,
            repo_root,
            _ALLOWED_TRACKER_STORE_REFERENCERS,
            recursive=False,
            use_ast_check=True,
        )
        assert offenders == [], (
            "top-level coordinator_core/ module(s) reference tracker_store "
            "outside the DR-241-affirmed allowlist "
            f"({sorted(_ALLOWED_TRACKER_STORE_REFERENCERS)}): {offenders}. "
            "A new top-level tracker_store referencer requires a fresh "
            "DR-241 bounds affirmation against that module's real code (see "
            "docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md "
            "§ Amendment) — do not just widen this allowlist to go green."
        )

    # -- Bound: classification is MUTATING, never COMPUTE_ONLY --------
    def test_tracker_ops_are_classified_mutating_not_compute_only(self):
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass

        tracker_keys = sorted(k for k in OP_CLASSIFICATION if k.startswith("tracker."))
        for key in tracker_keys:
            assert OP_CLASSIFICATION[key] is OpClass.MUTATING, (
                f"OP_CLASSIFICATION[{key!r}] is {OP_CLASSIFICATION[key]!r}, not "
                "MUTATING — DR-241's amendment classifies every tracker.* op "
                "MUTATING by construction (it writes files; DR-208 §5's "
                "COMPUTE_ONLY checklist does not apply). A tracker.* op "
                "classified COMPUTE_ONLY is the exact write-without-affirmed-"
                "bounds hazard sat-01's DEC-4 existed to prevent."
            )

        classification_path = (
            Path(_PROJECT_ROOT) / "coordinator_core" / "authz" / "classification.py"
        )
        classification_text = classification_path.read_text(encoding="utf-8")
        if "tracker_store" in classification_text and not tracker_keys:
            pytest.fail(
                "classification.py references tracker_store but OP_CLASSIFICATION "
                "carries no tracker.* key — a referencer with no live "
                "classification entry defeats DR-208 §5's fail-closed default "
                "and this guard's ability to check it."
            )

    # -- Bound: write-target confinement -------------------------------
    def test_allowlisted_referencers_confine_writes_via_tracker_store_api_only(self):
        # AST-scope check (see _tracker_scope_string_literals), not a
        # whole-file substring ban — coordinator_core/ops/session/
        # boot_sweep.py is a multi-purpose sweep that legitimately hand-
        # builds OTHER state/ subtree literals (e.g. "state/handoffs/...")
        # for concerns unrelated to tracker_store, in functions that never
        # mention "tracker" at all; those stay out of scope by construction
        # because they don't share a scope with any tracker reference. A
        # whole-file ban would be a false positive on that pre-existing,
        # unrelated code, and a tautology masquerading as a real check is
        # exactly what this brief forbids shipping.
        for rel in sorted(_ALLOWED_TRACKER_STORE_REFERENCERS):
            path = Path(_PROJECT_ROOT) / rel
            if not path.exists():
                # Historically pre-C5: nothing registered yet, nothing to
                # confine. Both allowlisted files exist as of C5 landing, so
                # this branch is dead for the CURRENT allowlist — retained
                # for any future allowlist entry added before its own file
                # lands. The confinement logic itself firing on a real
                # violation is proven independently by
                # TestWriteTargetConfinementBiteTest below (synthetic
                # offender fixtures under tmp_path), not by this guard alone.
                continue
            _confinement_check_for_file(path, rel)

    # -- Bound: per-repo, command-type only, no cross-repo/claude-klabauter-tree --
    def test_tracker_ops_are_common_dir_scoped(self):
        from coordinator_core.op_scopes import _OP_KEY_SCOPE

        tracker_keys = sorted(k for k in _OP_KEY_SCOPE if k.startswith("tracker."))
        for key in tracker_keys:
            assert _OP_KEY_SCOPE[key] == "common_dir", (
                f"_OP_KEY_SCOPE[{key!r}] is {_OP_KEY_SCOPE[key]!r}, not "
                "'common_dir' — DR-241's amendment pins tracker.* ops to the "
                "same per-repo common_dir scope session.boot_sweep uses; any "
                "other scope either fails to resolve repo_root (silent "
                "degradation to None) or crosses the per-repo confinement "
                "bound (DEC-11)."
            )

    def test_no_uds_or_http_surface_exists_to_expose_tracker_ops(self):
        # DR-215 retired coordinator_core's resident UDS daemon entirely —
        # this repo ships no UDS/HTTP server surface at all today, so bound
        # (v) ("in-process command-type dispatch only") reduces to: no such
        # surface exists to expose ANY op, tracker.* included. If a server
        # transport is ever reintroduced, this assertion fails loudly and
        # must be extended to check that surface for tracker.* keys rather
        # than silently passing.
        ipc_text = (Path(_PROJECT_ROOT) / "coordinator_core" / "ipc.py").read_text(
            encoding="utf-8"
        )
        assert "def start_server_async" not in ipc_text, (
            "a UDS server transport (start_server_async) has been "
            "reintroduced in ipc.py, retired by DR-215 — DR-241 bound (v) "
            "requires tracker.* ops stay in-process command-type dispatch "
            "only; extend this test to assert no tracker.* key is exposed "
            "on the reintroduced surface rather than deleting this check."
        )

    def test_allowlisted_referencers_do_not_cross_repo_or_hardcode_claude_klabauter_tree(self):
        for rel in sorted(_ALLOWED_TRACKER_STORE_REFERENCERS):
            path = Path(_PROJECT_ROOT) / rel
            if not path.exists():
                continue  # pre-C5: nothing to check yet, see note above.
            text = path.read_text(encoding="utf-8")
            for forbidden in ("claude-klabauter", "/Users/", "C:\\\\"):
                assert forbidden not in text, (
                    f"{rel} contains {forbidden!r} — DR-241's per-repo bound "
                    "(DEC-11) forbids a tracker.* op from hardcoding the "
                    "claude-klabauter tree or reaching across repos; writes must land "
                    "in the CONSUMING repo's own state/sovereign-tracker/."
                )


class TestExecutableLinesAttributeDocstringExclusion:
    """Direct unit tests for ``_executable_lines``' PEP-257 "attribute
    docstring" exclusion (Review: code-reviewer, Finding 3, 2026-08-05): the
    real-repo coverage via ``tracker_entities.py``'s ``EVENT_KINDS`` proves
    the fix works incidentally, but exercises none of the three shapes
    directly. The over-exclusion direction (case c below) is the dangerous
    one — sweeping a real executable statement out of the scan would
    silently WEAKEN the confinement check, so it gets its own assertion."""

    def test_bare_string_immediately_after_assign_is_excluded(self, tmp_path):
        # (a) An Assign immediately followed by a bare string Expr in the
        # same body — the PEP-257 attribute-docstring shape — is excluded.
        mod = tmp_path / "attr_docstring.py"
        mod.write_text(
            'EVENT_KINDS = ("a", "b")\n'
            '"""Doc string for EVENT_KINDS, mentions tracker_store."""\n',
            encoding="utf-8",
        )
        lines = _executable_lines(mod.read_text(encoding="utf-8"))
        executable_linenos = {lineno for lineno, _ in lines}
        assert 2 not in executable_linenos, (
            "an attribute docstring immediately following an Assign was not "
            "excluded from _executable_lines"
        )
        assert 1 in executable_linenos, "the Assign line itself must stay executable"

    def test_bare_string_not_following_assign_stays_executable(self, tmp_path):
        # (b) A bare string Expr NOT immediately preceded by an
        # Assign/AnnAssign is ordinary executable text, not a docstring —
        # must NOT be excluded.
        mod = tmp_path / "not_a_docstring.py"
        mod.write_text(
            "def f():\n"
            "    pass\n"
            '"tracker_store"\n',
            encoding="utf-8",
        )
        lines = _executable_lines(mod.read_text(encoding="utf-8"))
        executable_linenos = {lineno for lineno, _ in lines}
        assert 3 in executable_linenos, (
            "a bare string Expr not following an Assign/AnnAssign was "
            "wrongly excluded from _executable_lines as if it were an "
            "attribute docstring"
        )

    def test_real_statement_immediately_after_attribute_docstring_not_swept_in(
        self, tmp_path
    ):
        # (c) The dangerous over-exclusion direction: a real executable
        # statement immediately following an attribute docstring must NOT
        # be swept into the excluded set alongside it.
        mod = tmp_path / "statement_after_docstring.py"
        mod.write_text(
            'EVENT_KINDS = ("a", "b")\n'
            '"""Doc string for EVENT_KINDS."""\n'
            "tracker_store_reference = 1\n",
            encoding="utf-8",
        )
        lines = _executable_lines(mod.read_text(encoding="utf-8"))
        executable_linenos = {lineno for lineno, _ in lines}
        assert 2 not in executable_linenos, "the attribute docstring line must be excluded"
        assert 3 in executable_linenos, (
            "a real executable statement immediately after an attribute "
            "docstring was wrongly swept into the excluded set — this is "
            "the dangerous over-exclusion direction that would silently "
            "weaken the confinement check"
        )


class TestWriteTargetConfinementBiteTest:
    """Proves TestAffirmationEraBoundedRegistrationGuard's write-target
    confinement checks — the unconditional 'sovereign-tracker' literal ban
    and the AST-scope 'state/'/'archive/' literal ban — actually FIRE on a
    real violation, by running the identical shared check
    (``_confinement_check_for_file``) against synthetic offender fixtures
    under ``tmp_path``.

    Finding 2 (code-reviewer, 2026-07-28): a comment in the guard above used
    to CLAIM a bite-test proved this logic fires; no such test existed
    anywhere in the file. This class is that missing proof.
    """

    def test_sovereign_tracker_literal_violation_is_caught(self, tmp_path):
        offender = tmp_path / "offender_sovereign.py"
        offender.write_text(
            "from coordinator_core import tracker_store\n\n"
            "def do_write(repo_root):\n"
            '    target = repo_root / "state/sovereign-tracker/events"\n'
            "    tracker_store.append_event({}, repo_root=repo_root)\n",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError):
            _confinement_check_for_file(offender, "offender_sovereign.py")

    def test_state_literal_on_its_own_line_is_caught_via_ast_scope(self, tmp_path):
        # The reviewer's own worked example (Finding 1): a bare literal on
        # its own line, never co-located with a "tracker"-mentioning line —
        # a same-line-co-location check misses this; the AST-scope check,
        # which reaches every literal in the same enclosing function as a
        # tracker reference, must not.
        offender = tmp_path / "offender_state.py"
        offender.write_text(
            "from coordinator_core import tracker_store\n\n"
            "def do_write(repo_root):\n"
            '    target = "state/legacy"\n'
            "    tracker_store.append_event({}, repo_root=repo_root)\n",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError):
            _confinement_check_for_file(offender, "offender_state.py")

    def test_archive_literal_on_its_own_line_is_caught_via_ast_scope(self, tmp_path):
        # Reviewer's worked example verbatim: `target = "archive/legacy"` on
        # its own line, followed by a tracker call on the NEXT line.
        offender = tmp_path / "offender_archive.py"
        offender.write_text(
            "from coordinator_core import tracker_store\n\n"
            "def do_write(repo_root):\n"
            '    target = "archive/legacy"\n'
            "    tracker_store.append_event({}, repo_root=repo_root)\n",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError):
            _confinement_check_for_file(offender, "offender_archive.py")

    def test_clean_file_with_no_violations_passes(self, tmp_path):
        clean = tmp_path / "clean.py"
        clean.write_text(
            "from coordinator_core.tracker_store import EVENTS_DIR_RELPATH\n\n"
            "def do_write(repo_root):\n"
            "    target = repo_root / EVENTS_DIR_RELPATH\n"
            "    return target\n",
            encoding="utf-8",
        )
        _confinement_check_for_file(clean, "clean.py")  # must not raise

    def test_state_literal_outside_any_tracker_scope_is_not_flagged(self, tmp_path):
        # Mirrors boot_sweep.py's real shape: an unrelated function that
        # never mentions "tracker" may legitimately hand-build OTHER state/
        # subtree literals — this must NOT be flagged (false-positive guard,
        # the flip side of the two tests above).
        clean = tmp_path / "unrelated_state_literal.py"
        clean.write_text(
            "def unrelated_sweep(repo_root):\n"
            '    target = repo_root / "state/handoffs"\n'
            "    return target\n",
            encoding="utf-8",
        )
        _confinement_check_for_file(clean, "unrelated_state_literal.py")  # must not raise


class TestTopLevelWalkBiteTest:
    """Proves the sat-02 C6 widened WALK itself — not just the shared
    confinement-check logic ``TestWriteTargetConfinementBiteTest`` already
    proves — actually sees a top-level ``coordinator_core/*.py`` offender.

    Runs ``_tracker_store_referencer_offenders`` (the exact helper both
    real-repo tests above call) against a synthetic ``tmp_path`` tree shaped
    like a repo root with a ``coordinator_core/`` directory, so this is a
    genuine exercise of the widened walk rather than a reimplementation of
    it — mirrors ``TestWriteTargetConfinementBiteTest``'s own real-vs-
    synthetic sharing discipline (sat-01b Finding 2: a comment once CLAIMED
    a bite-test proved detection fires; none existed)."""

    def test_synthetic_top_level_offender_is_caught_by_the_walk(self, tmp_path):
        repo_root = tmp_path
        core_dir = repo_root / "coordinator_core"
        core_dir.mkdir()
        (core_dir / "offender_top_level.py").write_text(
            "from coordinator_core import tracker_store\n\n"
            "def do_write(repo_root):\n"
            "    return tracker_store.append_event({}, repo_root=repo_root)\n",
            encoding="utf-8",
        )
        offenders = _tracker_store_referencer_offenders(
            core_dir,
            repo_root,
            frozenset(),  # nothing allowlisted in this synthetic tree
            recursive=False,
            use_ast_check=True,
        )
        assert offenders == ["coordinator_core/offender_top_level.py"], (
            "the widened top-level walk failed to catch a synthetic "
            f"tracker_store-referencing offender module: {offenders!r}"
        )

    def test_docstring_only_mention_is_not_flagged_by_the_ast_walk(self, tmp_path):
        # Mirrors the real repo's ipc.py: a long-form "why" docstring names
        # tracker_store for narrative/backlink purposes, with no import and
        # no executable reference — this must NOT be flagged (false-
        # positive guard, the flip side of the bite test above, and the
        # exact reason the top-level scan uses the AST-aware check rather
        # than the ops/ walk's plain whole-file substring check).
        repo_root = tmp_path
        core_dir = repo_root / "coordinator_core"
        core_dir.mkdir()
        (core_dir / "narrative_only.py").write_text(
            '"""\n'
            "This module discusses tracker_store.append_event in its design\n"
            "history but neither imports nor calls it.\n"
            '"""\n\n'
            "def unrelated():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        offenders = _tracker_store_referencer_offenders(
            core_dir,
            repo_root,
            frozenset(),
            recursive=False,
            use_ast_check=True,
        )
        assert offenders == [], (
            "a docstring-only mention of tracker_store was wrongly flagged "
            f"by the AST-aware top-level walk: {offenders!r}"
        )

    def test_aliased_import_is_still_caught_by_the_ast_walk(self, tmp_path):
        # Review: code-reviewer, Finding 1, 2026-08-05 — an import aliased
        # via `as` (e.g. `from coordinator_core import tracker_store as ts`)
        # must not evade detection just because the locally-bound name no
        # longer contains "tracker_store". _references_tracker_store_in_code
        # must check the ORIGINAL imported name independently of any alias.
        repo_root = tmp_path
        core_dir = repo_root / "coordinator_core"
        core_dir.mkdir()
        (core_dir / "offender_aliased.py").write_text(
            "from coordinator_core import tracker_store as ts\n\n"
            "def do_write(repo_root):\n"
            "    return ts.append_event({}, repo_root=repo_root)\n",
            encoding="utf-8",
        )
        offenders = _tracker_store_referencer_offenders(
            core_dir,
            repo_root,
            frozenset(),
            recursive=False,
            use_ast_check=True,
        )
        assert offenders == ["coordinator_core/offender_aliased.py"], (
            "an aliased tracker_store import evaded the AST-aware top-level "
            f"walk: {offenders!r}"
        )


class TestAffirmationEraGuardCarriesBothRealTreeMethods:
    """Meta-test (Review: code-reviewer, Finding 2, 2026-08-05): asserts
    ``TestAffirmationEraBoundedRegistrationGuard`` still carries BOTH
    real-repo enforcement methods by name — the pre-existing ``ops/`` walk
    and the sat-02 C6 top-level walk. Without this, a future edit could
    silently delete one of those real-tree test methods while leaving the
    shared helper and the synthetic bite tests (``TestTopLevelWalkBiteTest``,
    ``TestWriteTargetConfinementBiteTest``) untouched — the suite would stay
    green with zero real-tree enforcement for that surface, exactly the
    "guard that passes because it inspects nothing" failure mode this whole
    chunk exists to rule out."""

    def test_both_real_tree_walk_methods_are_present(self):
        method_names = set(dir(TestAffirmationEraBoundedRegistrationGuard))
        assert "test_ops_tree_referencers_are_exact_match_allowlisted" in method_names, (
            "the real coordinator_core/ops/ walk test method is missing from "
            "TestAffirmationEraBoundedRegistrationGuard — real-tree "
            "enforcement of the ops/ surface has been silently dropped"
        )
        assert (
            "test_top_level_coordinator_core_referencers_are_exact_match_allowlisted"
            in method_names
        ), (
            "the real top-level coordinator_core/ walk test method is missing "
            "from TestAffirmationEraBoundedRegistrationGuard — real-tree "
            "enforcement of the sat-02 C6 top-level surface has been "
            "silently dropped"
        )


# ---------------------------------------------------------------------------
# AC8 — no POSIX-only assumption
# ---------------------------------------------------------------------------


class TestAC8CrossPlatformBehavior:
    def test_shard_path_uses_pathlib_not_string_concatenation(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        path = shard_path(repo, machine="some-machine")
        assert isinstance(path, Path)
        assert path == repo / EVENTS_DIR_RELPATH / "events.some-machine.jsonl"

    def test_shard_file_written_and_read_with_explicit_newline_handling(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)
        append_event(_event("evt-2", "2026-01-01T00:00:01Z"), repo_root=repo)

        raw = shard_path(repo).read_bytes()
        # No CRLF sequences — writes use "\n" explicitly (json.dumps + "\n"),
        # never a text-mode round trip that could translate line endings.
        assert b"\r\n" not in raw

        records = _read_raw_lines(shard_path(repo))
        assert len(records) == 2

    def test_max_sequence_absent_shard_is_zero_and_present_shard_matches_tail(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        assert max_sequence(repo_root=repo) == 0

        append_event(_event("evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)
        result = append_event(_event("evt-2", "2026-01-01T00:00:01Z"), repo_root=repo)

        assert max_sequence(repo_root=repo) == result["sequence"] == 2


# ---------------------------------------------------------------------------
# fold_observed_set — sat-01b C1
#
# Coverage requirements (per chunk spec C1, plan
# docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md):
#   AC1  — exactly ONE marker appended to the calling machine's own shard;
#          every peer shard byte- and mtime-unchanged.
#   AC2  — append_event is unmodified (proven by re-running AC1c/AC1h(c)).
#   AC3  — marker id shape, cross-machine non-collision, same-content
#          idempotent id.
#   AC6b — two consecutive folds with an unchanged observed_set leave
#          exactly ONE marker and raise nothing.
#   AC8 (partial) — marker absent from read_events, no projection-order
#          perturbation.
#   AC12 — cross-platform: no CRLF, pathlib-only path construction.
#   No-store case — opt-in by existence only.
# ---------------------------------------------------------------------------


class TestFoldObservedSetAC1OwnShardOnly:
    def test_fold_appends_one_marker_to_own_shard_peer_shards_untouched(
        self, tmp_path, monkeypatch
    ):
        repo = _make_git_repo(tmp_path / "repo")

        peer_path = _write_shard(
            repo,
            "peer-machine",
            [
                {**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-machine"},
                {**_event("peer-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-machine"},
            ],
        )
        peer_bytes_before = peer_path.read_bytes()
        peer_mtime_before = peer_path.stat().st_mtime_ns

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)

        assert marker is not None
        assert marker["kind"] == "observed_set_fold"
        assert marker["machine"] == "this-machine"

        own_shard = shard_path(repo, machine="this-machine")
        own_records = _read_raw_lines(own_shard)
        assert len(own_records) == 1
        assert own_records[0]["id"] == marker["id"]

        assert peer_path.read_bytes() == peer_bytes_before, "peer shard bytes changed"
        assert peer_path.stat().st_mtime_ns == peer_mtime_before, "peer shard mtime changed"


class TestFoldObservedSetAC2AppendEventUnmodified:
    def test_ac1c_peer_shard_untouched_still_passes(self, tmp_path, monkeypatch):
        # Re-run of TestAC1cWritesOnlyOwnShard's assertion, via the SAME
        # shared helper that test calls (Finding 4, code-reviewer,
        # 2026-07-28 — this used to be a hand-copied duplicate that could
        # drift from the original), to prove append_event's own-shard-only
        # write behavior is unchanged by C1.
        _run_ac1c_peer_shard_untouched_check(tmp_path, monkeypatch)

    def test_ac1h_c_append_event_reads_no_shard_but_its_own_still_passes(
        self, tmp_path, monkeypatch
    ):
        # Re-run of TestAC1hLogicalClockCarriedNotAuthoritative's third test,
        # via the SAME shared helper that test calls (Finding 4), to prove
        # append_event's no-peer-read guarantee is unchanged.
        _run_ac1h_c_no_peer_read_check(tmp_path, monkeypatch)


class TestFoldObservedSetAC3MarkerId:
    def test_marker_id_shape_and_digest(self, tmp_path, monkeypatch):
        import hashlib

        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-a-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None

        observed_set = marker["observed_set"]
        expected_digest = hashlib.sha256(
            json.dumps(observed_set, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        assert marker["id"] == f"this-machine-fold-{expected_digest}"

    def test_two_machines_folding_independently_never_collide(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-shared",
            [{**_event("shared-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-shared"}],
        )

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-a")
        marker_a = fold_observed_set(repo_root=repo)

        # machine-a's own shard is now a peer of machine-b's fold, so
        # machine-b observes a different set of peers than machine-a did —
        # confirming the ids differ is still meaningful even though the
        # inputs aren't literally identical.
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-b")
        marker_b = fold_observed_set(repo_root=repo)

        assert marker_a is not None and marker_b is not None
        assert marker_a["id"] != marker_b["id"]
        assert marker_a["id"].startswith("machine-a-fold-")
        assert marker_b["id"].startswith("machine-b-fold-")

    def test_same_machine_identical_observed_set_produces_identical_id(
        self, tmp_path, monkeypatch
    ):
        # This test proves the id FORMULA is stable by re-deriving it
        # independently (idempotency short-circuits a literal second fold,
        # so it can't observe two real markers side by side) — the "two
        # real folds produce one marker" proof itself lives in
        # TestFoldObservedSetAC6bIdempotentReFold, not here (Finding 7,
        # code-reviewer, 2026-07-28).
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-a-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker_1 = fold_observed_set(repo_root=repo)
        assert marker_1 is not None
        first_id = marker_1["id"]

        # Second fold is a no-op (AC6b) — verify the id THAT WOULD be
        # produced is identical by re-deriving it against the same peer
        # bytes independently, without a second append.
        peer_shard = shard_path(repo, machine="peer-a")
        event_ids = [r["id"] for r in _read_raw_lines(peer_shard)]
        digest = ts._prefix_digest(event_ids)
        observed_set = {
            "peer-a": {
                "sequence": max_sequence(repo_root=repo, machine="peer-a"),
                "prefix_digest": digest,
            }
        }
        import hashlib

        expected_digest = hashlib.sha256(
            json.dumps(observed_set, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        assert first_id == f"this-machine-fold-{expected_digest}"


class TestFoldObservedSetAC6bIdempotentReFold:
    def test_two_consecutive_folds_leave_exactly_one_marker(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-a-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        first = fold_observed_set(repo_root=repo)
        assert first is not None

        second = fold_observed_set(repo_root=repo)
        assert second is None, "unchanged observed_set must no-op, not raise or re-append"

        own_shard = shard_path(repo, machine="this-machine")
        markers = [r for r in _read_raw_lines(own_shard) if r.get("kind") == "observed_set_fold"]
        assert len(markers) == 1

    def test_second_fold_does_not_raise_trackerstoreerror(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-a-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        fold_observed_set(repo_root=repo)
        # No pytest.raises wrapper: a regression that relies on append_event's
        # own duplicate-id rejection would raise TrackerStoreError here.
        fold_observed_set(repo_root=repo)


class TestFoldObservedSetAC8ProjectionUnaffected:
    def test_marker_absent_from_read_events_and_order_unperturbed(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")
        pre_existing = append_event(
            _event("own-evt-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"),
            repo_root=repo,
        )
        _write_shard(
            repo,
            "peer-a",
            [
                {
                    **_event("peer-a-evt-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:02Z"),
                    "sequence": 1,
                    "machine": "peer-a",
                }
            ],
        )

        order_before = [e["id"] for e in read_events(repo_root=repo)]

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None

        result = read_events(repo_root=repo)
        result_ids = [e["id"] for e in result]

        assert marker["id"] not in result_ids, "marker must not participate in projection"
        assert pre_existing["id"] in result_ids
        assert result_ids == order_before, "fold perturbed pre-existing event order"


class TestFoldObservedSetAC12CrossPlatform:
    def test_marker_write_has_no_crlf_and_shard_path_is_pathlib(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-a-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None

        own_shard = shard_path(repo, machine="this-machine")
        assert isinstance(own_shard, Path)
        raw = own_shard.read_bytes()
        assert b"\r\n" not in raw


# ---------------------------------------------------------------------------
# C3 — bootstrap / fresh-clone case. Spec: docs/plans/2026-07-28-sat-01b-
# observed-set-fold-actuator.md § Design "Bootstrap", § Tasks C3.
#
# C1 already ships the bootstrap behavior in full: fold_observed_set appends
# a marker at first fold regardless of whether observed_set ends up concrete
# or legitimately empty, and an absent marker already resolves to
# OBSERVED_SET_UNKNOWN via resolve_observed_set_for_event (C2). These tests
# make that shape explicit under the "fresh clone" framing cockpit named,
# rather than adding new machinery — there is none to add.
# ---------------------------------------------------------------------------


class TestFoldObservedSetC3BootstrapFreshClone:
    def test_fresh_clone_first_fold_records_own_position_instead_of_inferring_it(self, tmp_path, monkeypatch):
        """A fresh clone has peer history but this machine has never folded
        and has no shard file at all. Proves the bootstrap semantics stated
        in fold_observed_set's docstring: the machine's own first fold
        records its position — the marker's placement in the own shard IS
        that record — rather than a naive empty-vector default reading a
        later own event as concurrent with all prior peer history."""
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event(f"peer-evt-{i}", f"2026-01-01T00:00:{i:02d}Z"), "sequence": i, "machine": "peer-a"}
                for i in range(1, 4)
            ],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "fresh-machine")

        own_shard = shard_path(repo, machine="fresh-machine")
        assert not own_shard.exists(), "fresh clone must have no own shard before its first fold"

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"]["peer-a"]["sequence"] == 3
        assert own_shard.exists()
        raw = _read_raw_lines(own_shard)
        assert len(raw) == 1 and raw[0]["id"] == marker["id"], (
            "the marker's placement in the own shard IS the recorded position"
        )

        own_event = append_event(_event("fresh-evt-1", "2026-01-01T00:01:00Z"), repo_root=repo)
        resolved = resolve_observed_set_for_event(own_event, repo_root=repo)
        assert resolved == marker["observed_set"], (
            "an event past the bootstrap fold resolves against the recorded "
            "position, never treated as predating history"
        )

    def test_bootstrap_with_zero_peer_events_is_legitimate_empty_not_absent(self, tmp_path, monkeypatch):
        """A machine that folds first, in a repo where no peer has appended
        anything yet, still records a marker — its observed_set is a
        legitimate EMPTY dict (AC6), never the absence a naive
        skip-if-nothing-to-observe implementation would produce, and never
        conflated with OBSERVED_SET_UNKNOWN."""
        repo = _make_git_repo(tmp_path / "repo")
        (repo / EVENTS_DIR_RELPATH).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "first-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"] == {}

        own_event = append_event(_event("first-evt-1", "2026-01-01T00:01:00Z"), repo_root=repo)
        resolved = resolve_observed_set_for_event(own_event, repo_root=repo)
        assert resolved == {}
        assert resolved is not OBSERVED_SET_UNKNOWN


class TestFoldObservedSetNoStoreOptIn:
    def test_fold_with_no_store_directory_returns_none_and_creates_nothing(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        assert not (repo / EVENTS_DIR_RELPATH).exists()

        result = fold_observed_set(repo_root=repo)

        assert result is None
        assert not (repo / EVENTS_DIR_RELPATH).exists(), (
            "fold_observed_set must not mint the store directory — opt-in by "
            "existence only"
        )


# ---------------------------------------------------------------------------
# C2 — resolve_observed_set / resolve_observed_set_for_event, the three-valued
# read-time validation. Spec: docs/plans/2026-07-28-sat-01b-observed-set-fold-
# actuator.md § Design ("Resolution, and the three-valued return", "The
# event→marker mapping"), § Acceptance Criteria AC4/AC5/AC5b/AC6/AC6c.
# ---------------------------------------------------------------------------


class TestResolveObservedSetC2aPerComponentResolution:
    """tmrg-03 C2a — cockpit's § RESOLUTION GRANULARITY IS PER COMPONENT
    (their § VALIDATOR CONTRACT (A12) item 5): one damaged peer component
    must not collapse the whole marker to OBSERVED_SET_UNKNOWN. A caller
    must still be able to order events against an intact peer, in the SAME
    returned mapping as the damaged peer's unknown.

    Spec backlink: docs/plans/2026-08-18-tmrg-03-ordering-contract-in-
    tracker-store.md § C2a.
    """

    def test_one_damaged_peer_and_one_intact_peer_resolve_independently(self, tmp_path):
        # peer-a: a genuine gap ([1, 2, 4] claiming 4) — damaged.
        # peer-b: a clean, contiguous shard — intact.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("a-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("a-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-a"},
                {**_event("a-evt-4", "2026-01-01T00:00:03Z"), "sequence": 4, "machine": "peer-a"},
            ],
        )
        _write_shard(
            repo,
            "peer-b",
            [
                {**_event("b-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-b"},
                {**_event("b-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-b"},
            ],
        )
        damaged_digest = ts._prefix_digest(["a-evt-1", "a-evt-2", "a-evt-4"])
        intact_digest = ts._prefix_digest(["b-evt-1", "b-evt-2"])
        marker = {
            "id": "this-machine-fold-abc123",
            "kind": "observed_set_fold",
            "observed_at": "2026-01-01T00:00:05Z",
            "applied_at": None,
            "observed_set": {
                "peer-a": {"sequence": 4, "prefix_digest": damaged_digest},
                "peer-b": {"sequence": 2, "prefix_digest": intact_digest},
            },
        }

        resolved = resolve_observed_set(marker, repo_root=repo)

        # The intact peer resolves concretely — a caller can still order
        # against it, in the SAME returned mapping as the damaged peer.
        assert resolved["peer-b"] == {"sequence": 2, "prefix_digest": intact_digest}
        assert resolved["peer-b"] is not OBSERVED_SET_UNKNOWN

        # The damaged peer resolves unknown — not a whole-marker collapse.
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN

        # AC3: unknown is distinguishable from a genuine {} directly and by
        # identity, never via truthiness — both are falsy by design.
        assert not resolved["peer-a"]
        assert resolved["peer-a"] != {}
        assert type(resolved["peer-a"]) is not dict

        assert set(resolved) == {"peer-a", "peer-b"}


class TestResolveObservedSetAC4PeerSequenceExceedsCurrentMax:
    def test_truncated_peer_shard_resolves_unknown(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event(f"peer-evt-{i}", f"2026-01-01T00:00:{i:02d}Z"), "sequence": i, "machine": "peer-a"}
                for i in range(1, 11)
            ],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"]["peer-a"]["sequence"] == 10

        # Truncate the peer shard: only the first 5 events survive — the
        # justifying bytes for events 6..10 have left the repository.
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event(f"peer-evt-{i}", f"2026-01-01T00:00:{i:02d}Z"), "sequence": i, "machine": "peer-a"}
                for i in range(1, 6)
            ],
        )
        assert max_sequence(repo_root=repo, machine="peer-a") == 5

        # Per-component resolution (tmrg-03 C2a): the return is a dict whose
        # peer-a VALUE is the sentinel, not the sentinel itself at top level.
        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN


class TestResolveObservedSetAC5MidRangeHoleDigestMismatch:
    def test_deleted_interior_line_resolves_unknown_via_digest_mismatch(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        records = [
            {**_event(f"peer-evt-{i}", f"2026-01-01T00:00:{i:02d}Z"), "sequence": i, "machine": "peer-a"}
            for i in range(1, 11)
        ]
        _write_shard(repo, "peer-a", records)
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"]["peer-a"]["sequence"] == 10

        # Delete the interior record at sequence 5. The TAIL (sequence 10)
        # is untouched, so max_sequence still reads 10 — no positional hole
        # is visible from the tail alone; only a full-prefix digest
        # recompute catches this.
        holed_records = [r for r in records if r["sequence"] != 5]
        _write_shard(repo, "peer-a", holed_records)
        assert max_sequence(repo_root=repo, machine="peer-a") == 10

        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN


class TestResolveObservedSetAC5bContentBoundRebaseCounterexample:
    def test_rebase_and_reappend_defeats_position_only_check(self, tmp_path, monkeypatch):
        """The critical-finding counterexample, constructed with REAL git
        operations — not simulated.

        1. Machine B appends events b-1..b-10 to its own shard via
           ``append_event``, ONE git commit per event (10 real commits).
        2. Machine A folds, recording a claim against B's shard at
           sequence 10 with a ``prefix_digest`` over ids b-1..b-10;
           committed.
        3. Real rebase reproduction: ``git checkout <sha-at-b-7> --
           <b's shard path>`` restores B's shard FILE to the exact blob it
           held right after event b-7 (i.e. before events 8-10 existed) —
           the same net effect on that one file a `git rebase` dropping
           B's last three commits would produce — followed by a real
           ``git commit`` of that restoration. This is the plan's own
           named fallback: "reproduce the same bytes through real git
           commands (branch, reset --hard, re-commit)" — here realized as
           a targeted historical-blob checkout + commit rather than a
           whole-tree ``reset --hard``, specifically so machine A's
           already-committed marker (and its own shard file) is left
           completely untouched by the reproduction, matching a real
           rebase's per-branch scope.
        4. B then appends THREE NEW events (b-new-8, b-new-9, b-new-10)
           via ``append_event`` — real commits again. ``append_event``'s
           tail-derived sequence assignment (``tracker_store.py`` derives
           sequence from the shard's own tail; it has no memory of what
           used to occupy those positions) re-assigns them sequence
           8, 9, 10. B's shard is now 1..10 with NO positional hole and
           ``max_sequence`` 10 — every position-only check passes.

        ``resolve_observed_set`` on A's marker MUST resolve to
        ``OBSERVED_SET_UNKNOWN`` via prefix-digest mismatch, never to a
        concrete trusted set.
        """
        repo = _make_git_repo(tmp_path / "repo")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-b")
        pre_truncation_sha = None
        for i in range(1, 11):
            append_event(_event(f"b-{i}", f"2026-01-01T00:00:{i:02d}Z"), repo_root=repo)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", f"chore: machine-b appends event {i}")
            if i == 7:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(repo),
                    capture_output=True,
                    check=True,
                    text=True,
                    creationflags=_NO_WINDOW,
                )
                pre_truncation_sha = result.stdout.strip()
        assert pre_truncation_sha is not None

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-a")
        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"]["machine-b"]["sequence"] == 10
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "chore: machine-a folds observed_set")

        # Real git: restore ONLY machine-b's shard file to the blob it held
        # at pre_truncation_sha (before events 8-10 existed), leaving every
        # other tracked file (including A's marker) at HEAD's content.
        b_shard_relpath = shard_path(repo, machine="machine-b").relative_to(repo).as_posix()
        _git(repo, "checkout", pre_truncation_sha, "--", b_shard_relpath)
        _git(repo, "commit", "-m", "chore: rebase drops machine-b's events 8-10")

        assert max_sequence(repo_root=repo, machine="machine-b") == 7

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-b")
        for i in range(8, 11):
            append_event(_event(f"b-new-{i}", f"2026-01-01T00:01:{i:02d}Z"), repo_root=repo)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", f"chore: machine-b re-appends new event at position {i}")

        # Position-only checks now pass: no hole, same max_sequence.
        assert max_sequence(repo_root=repo, machine="machine-b") == 10
        records = _read_raw_lines(shard_path(repo, machine="machine-b"))
        assert len(records) == 10
        assert [r["sequence"] for r in records] == list(range(1, 11))
        ids = [r["id"] for r in records]
        assert ids[7:] == ["b-new-8", "b-new-9", "b-new-10"], (
            "fixture must actually substitute the tail positions, not merely append"
        )

        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved == {"machine-b": OBSERVED_SET_UNKNOWN}, (
            "content-bound check must catch a position-preserving rebase-"
            "and-re-append substitution, never resolve to a trusted set"
        )
        assert resolved["machine-b"] is OBSERVED_SET_UNKNOWN


class TestResolveObservedSetAC6EmptyVsUnknownNeverConflated:
    def test_genuinely_empty_observed_set_resolves_to_concrete_empty_dict(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        # No peer shard exists at all — fold_observed_set produces a marker
        # with a genuinely empty observed_set (no peers to observe).
        (repo / EVENTS_DIR_RELPATH).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"] == {}

        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved == {}
        assert resolved is not OBSERVED_SET_UNKNOWN
        assert type(resolved) is dict, "empty-set must be a concrete dict, not the sentinel"

    def test_absent_marker_resolves_to_unknown_not_empty(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        # No fold has ever happened on this machine — the event predates
        # any marker.
        event = append_event(_event("own-evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)

        resolved = resolve_observed_set_for_event(event, repo_root=repo)
        assert resolved is OBSERVED_SET_UNKNOWN

    def test_empty_set_and_absent_marker_are_distinguishable_by_identity(self, tmp_path, monkeypatch):
        # Both values are falsy — deliberately — so this test proves the
        # API still distinguishes them, by identity/type, never by
        # truthiness (truthiness alone cannot tell them apart).
        repo = _make_git_repo(tmp_path / "repo")
        (repo / EVENTS_DIR_RELPATH).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        empty_result = resolve_observed_set(marker, repo_root=repo)

        # A synthetic event at sequence 0, on a machine whose only marker
        # (from the fold above) sits at sequence 1 — no marker with
        # sequence < 0 exists, forcing the "no marker found" branch.
        unknown_result = resolve_observed_set_for_event(
            {"machine": "this-machine", "sequence": 0}, repo_root=repo
        )

        assert not empty_result and not unknown_result, (
            "fixture must exercise the shared-falsiness hazard both values share"
        )
        assert empty_result is not unknown_result
        assert empty_result == {}
        assert unknown_result is OBSERVED_SET_UNKNOWN


class TestResolveObservedSetForEventAC6cEventMarkerMapping:
    def test_multi_marker_event_maps_to_most_recent_prior_marker(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "machine-a")

        # sequence 1 — predates any fold.
        e1 = append_event(_event("a-evt-1", "2026-01-01T00:00:00Z"), repo_root=repo)

        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )

        # sequence 2 — first marker.
        marker1 = fold_observed_set(repo_root=repo)
        assert marker1 is not None

        # sequence 3 — between the two markers.
        e2 = append_event(_event("a-evt-2", "2026-01-01T00:00:01Z"), repo_root=repo)

        # Change peer-a's shard so the second fold is not an idempotent no-op.
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("peer-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-a"},
            ],
        )

        # sequence 4 — second marker.
        marker2 = fold_observed_set(repo_root=repo)
        assert marker2 is not None
        assert marker2["id"] != marker1["id"]

        # sequence 5 — after both markers.
        e3 = append_event(_event("a-evt-3", "2026-01-01T00:00:02Z"), repo_root=repo)

        resolved_e1 = resolve_observed_set_for_event(e1, repo_root=repo)
        assert resolved_e1 is OBSERVED_SET_UNKNOWN, "event predating the first-ever fold must be unknown"

        resolved_e2 = resolve_observed_set_for_event(e2, repo_root=repo)
        assert resolved_e2 == marker1["observed_set"], "event between the two markers must map to the FIRST"

        resolved_e3 = resolve_observed_set_for_event(e3, repo_root=repo)
        assert resolved_e3 == marker2["observed_set"], "event after both markers must map to the SECOND"


class TestMarkerSelectionTiebreaksOnIdNotLinePosition:
    """A git text-merge can leave two markers at one ``sequence`` and can
    reorder lines without reordering applies, so marker selection must be
    total on ``(sequence, id)`` rather than resolving to whichever duplicate
    the shard happens to list first.

    Spec backlink: cross-repo/inbox/2026-08-18-example-cockpit-repo-em-tmrg-03-
    ordering-contract-and-prefix-closure-ownership.md § (e).
    """

    @staticmethod
    def _shard_with_markers_in_order(repo, low_first: bool) -> tuple[dict, dict, dict]:
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        _write_shard(
            repo,
            "peer-b",
            [{**_event("peer-evt-b1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-b"}],
        )

        low_marker = {
            "id": "machine-a-fold-aaaaaaaaaaaaaaaa",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 5,
            "observed_at": "2026-01-01T00:00:05Z",
            "applied_at": None,
            "observed_set": {
                "peer-a": {"sequence": 1, "prefix_digest": ts._prefix_digest(["peer-evt-1"])}
            },
        }
        high_marker = {
            **low_marker,
            "id": "machine-a-fold-zzzzzzzzzzzzzzzz",
            "observed_set": {
                "peer-b": {"sequence": 1, "prefix_digest": ts._prefix_digest(["peer-evt-b1"])}
            },
        }
        ordered = [low_marker, high_marker] if low_first else [high_marker, low_marker]
        event = {
            **_event("a-evt-later", "2026-01-01T00:00:06Z", applied_at="2026-01-01T00:00:06Z"),
            "machine": "machine-a",
            "sequence": 6,
        }
        _write_shard(repo, "machine-a", [*ordered, event])
        return low_marker, high_marker, event

    def test_duplicate_sequence_markers_resolve_to_the_higher_id(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        _low, high_marker, event = self._shard_with_markers_in_order(repo, low_first=True)

        assert resolve_observed_set_for_event(event, repo_root=repo) == high_marker["observed_set"]

    def test_selection_is_independent_of_line_order(self, tmp_path):
        repo_a = _make_git_repo(tmp_path / "repo-a")
        repo_b = _make_git_repo(tmp_path / "repo-b")
        _l1, high_marker, event_a = self._shard_with_markers_in_order(repo_a, low_first=True)
        _l2, _h2, event_b = self._shard_with_markers_in_order(repo_b, low_first=False)

        resolved_a = resolve_observed_set_for_event(event_a, repo_root=repo_a)
        resolved_b = resolve_observed_set_for_event(event_b, repo_root=repo_b)

        assert resolved_a == resolved_b == high_marker["observed_set"]


class TestResolveObservedSetMalformedMarkerRaises:
    def test_missing_observed_set_field_raises_trackerstoreerror(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        with pytest.raises(TrackerStoreError):
            resolve_observed_set({"id": "x-fold-abc", "kind": "observed_set_fold"}, repo_root=repo)

    def test_non_dict_component_raises_trackerstoreerror(self, tmp_path):
        # tracker_store.py:514-517 — a per-machine claim that isn't itself a
        # dict (Finding 5, code-reviewer, 2026-07-28: previously unexercised).
        repo = _make_git_repo(tmp_path / "repo")
        with pytest.raises(TrackerStoreError):
            resolve_observed_set(
                {
                    "id": "x-fold-abc",
                    "kind": "observed_set_fold",
                    "observed_set": {"peer-a": 42},
                },
                repo_root=repo,
            )

    def test_non_int_sequence_raises_trackerstoreerror(self, tmp_path):
        # tracker_store.py:520-524 — a claimed "sequence" that isn't an int
        # (Finding 5, code-reviewer, 2026-07-28: previously unexercised).
        repo = _make_git_repo(tmp_path / "repo")
        with pytest.raises(TrackerStoreError):
            resolve_observed_set(
                {
                    "id": "x-fold-abc",
                    "kind": "observed_set_fold",
                    "observed_set": {
                        "peer-a": {"sequence": "not-an-int", "prefix_digest": "abc123"}
                    },
                },
                repo_root=repo,
            )


class TestResolveObservedSetHappyPath:
    def test_fold_then_immediate_resolve_returns_concrete_set_unchanged(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")

        marker = fold_observed_set(repo_root=repo)
        assert marker is not None

        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved == marker["observed_set"]
        assert resolved is not OBSERVED_SET_UNKNOWN
        assert resolved is not marker["observed_set"], (
            "the all-concrete return must be a fresh dict, never the "
            "marker's own observed_set object by reference"
        )


class TestResolveObservedSetWellFormedness:
    def test_gapped_prefix_with_matching_digest_resolves_unknown(self, tmp_path):
        # AC1i — cockpit's A12 well-formedness predicate: sequences 1, 2, 4
        # (a hole at 3) with a marker claiming sequence 4 whose prefix_digest
        # matches the recomputed digest over ids [1, 2, 4] exactly. Before
        # C1a, the digest check alone was sufficient to return concrete —
        # this is the regression guard that a hole is caught even when the
        # digest recomputes identical.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("peer-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-a"},
                {**_event("peer-evt-4", "2026-01-01T00:00:03Z"), "sequence": 4, "machine": "peer-a"},
            ],
        )
        digest = ts._prefix_digest(["peer-evt-1", "peer-evt-2", "peer-evt-4"])
        marker = {
            "id": "this-machine-fold-abc123",
            "kind": "observed_set_fold",
            "observed_at": "2026-01-01T00:00:05Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 4, "prefix_digest": digest}},
        }

        resolved = resolve_observed_set(marker, repo_root=repo)

        assert resolved == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN

    def test_duplicate_at_tail_claim_at_duplicated_value_resolves_unknown(self, tmp_path):
        # cockpit's "duplicate-at-tail" conformance vector: shard sequences
        # [1, 2, 3, 3] — the git text-merge shape, two branches each
        # appending sequence = tail + 1 from their own tail. first_defect is
        # file position 4, but resolves_unknown_from is 3 (the DUPLICATED
        # VALUE, not the position) — a claim of 3 is already unresolvable.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("peer-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-a"},
                {**_event("peer-evt-3a", "2026-01-01T00:00:02Z"), "sequence": 3, "machine": "peer-a"},
                {**_event("peer-evt-3b", "2026-01-01T00:00:03Z"), "sequence": 3, "machine": "peer-a"},
            ],
        )
        digest = ts._prefix_digest(["peer-evt-1", "peer-evt-2", "peer-evt-3a", "peer-evt-3b"])
        marker = {
            "id": "this-machine-fold-def456",
            "kind": "observed_set_fold",
            "observed_at": "2026-01-01T00:00:05Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 3, "prefix_digest": digest}},
        }

        resolved = resolve_observed_set(marker, repo_root=repo)

        assert resolved == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN

    def test_duplicate_at_tail_claim_below_defect_resolves_concrete(self, tmp_path):
        # Point-of-failure scope (A12 item 3): the SAME defective shard as
        # above, but a claim of 2 — strictly below resolves_unknown_from (3)
        # — must still resolve concretely. A shard that goes bad late must
        # not retroactively invalidate its own good prefix.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("peer-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-a"},
                {**_event("peer-evt-3a", "2026-01-01T00:00:02Z"), "sequence": 3, "machine": "peer-a"},
                {**_event("peer-evt-3b", "2026-01-01T00:00:03Z"), "sequence": 3, "machine": "peer-a"},
            ],
        )
        digest = ts._prefix_digest(["peer-evt-1", "peer-evt-2"])
        marker = {
            "id": "this-machine-fold-def456",
            "kind": "observed_set_fold",
            "observed_at": "2026-01-01T00:00:05Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 2, "prefix_digest": digest}},
        }

        resolved = resolve_observed_set(marker, repo_root=repo)

        assert resolved == marker["observed_set"]
        assert resolved is not OBSERVED_SET_UNKNOWN

    def test_out_of_file_order_resolves_unknown(self, tmp_path):
        # cockpit's "out-of-file-order" conformance vector: the SET
        # {1, 2, 3, 4} is contiguous, but the shard's FILE order is
        # [1, 3, 2, 4] — exactly what a git text-merge produces (bytes
        # reordered without applies being reordered). A validator that
        # sorts before checking passes this and is wrong; the predicate
        # reads shard order as given. first_defect/resolves_unknown_from
        # are both 2 for this vector, so a claim of 2 must resolve unknown.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("peer-evt-seq1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("peer-evt-seq3", "2026-01-01T00:00:01Z"), "sequence": 3, "machine": "peer-a"},
                {**_event("peer-evt-seq2", "2026-01-01T00:00:02Z"), "sequence": 2, "machine": "peer-a"},
                {**_event("peer-evt-seq4", "2026-01-01T00:00:03Z"), "sequence": 4, "machine": "peer-a"},
            ],
        )
        digest = ts._prefix_digest(["peer-evt-seq1", "peer-evt-seq2"])
        marker = {
            "id": "this-machine-fold-ghi789",
            "kind": "observed_set_fold",
            "observed_at": "2026-01-01T00:00:05Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 2, "prefix_digest": digest}},
        }

        resolved = resolve_observed_set(marker, repo_root=repo)

        assert resolved == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN


class TestResolveObservedSetForEventPointOfFailureScope:
    """tmrg-03 C2b — AC2a, point-of-failure scope on the PER-EVENT path
    (cockpit's § VALIDATOR CONTRACT (A12) item 3), proven through
    ``resolve_observed_set_for_event`` itself rather than through
    ``resolve_observed_set`` directly. A test that only exercises
    ``resolve_observed_set`` does not prove ``resolve_observed_set_for_event``'s
    own marker-selection step preserves the scoping — this class walks the
    actual event path: two distinct own-machine events selecting two
    distinct markers, each with a distinct claim against the SAME
    defective peer shard.

    Mirrors cockpit's ``defect-late-in-long-shard`` conformance vector:
    peer-a sequences ``[1..12, 14]`` (13 is missing) — ``first_defect`` and
    ``resolves_unknown_from`` are both 13.

    Spec backlink: docs/plans/2026-08-18-tmrg-03-ordering-contract-in-
    tracker-store.md § C2b; docs/plans/2026-07-28-sat-01b-observed-set-fold-
    actuator.md § Design, "The event→marker mapping".
    """

    @staticmethod
    def _peer_ids_and_shard(repo) -> list[str]:
        peer_ids = [f"peer-evt-{i}" for i in range(1, 13)] + ["peer-evt-14"]
        records = [
            {**_event(peer_ids[i], f"2026-01-01T00:00:{i:02d}Z"), "sequence": i + 1, "machine": "peer-a"}
            for i in range(12)
        ]
        records.append(
            {**_event("peer-evt-14", "2026-01-01T00:00:13Z"), "sequence": 14, "machine": "peer-a"}
        )
        _write_shard(repo, "peer-a", records)
        return peer_ids

    def test_claim_strictly_below_defect_resolves_concrete_via_event_path(self, tmp_path):
        # A marker claiming sequence 12 — the last good position, one below
        # resolves_unknown_from (13) — must resolve concretely for the
        # event that maps to it. This is the assertion that actually
        # discriminates point-of-failure scope from the whole-marker
        # collapse the plan warns a weaker test would pass.
        repo = _make_git_repo(tmp_path / "repo")
        peer_ids = self._peer_ids_and_shard(repo)

        good_digest = ts._prefix_digest(peer_ids[:12])
        marker_low = {
            "id": "machine-a-fold-low",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:20Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 12, "prefix_digest": good_digest}},
        }
        event_low = {
            **_event("a-evt-low", "2026-01-01T00:00:21Z"),
            "machine": "machine-a",
            "sequence": 2,
        }
        _write_shard(repo, "machine-a", [marker_low, event_low])

        resolved = resolve_observed_set_for_event(event_low, repo_root=repo)

        assert resolved == {"peer-a": {"sequence": 12, "prefix_digest": good_digest}}
        assert resolved["peer-a"] is not OBSERVED_SET_UNKNOWN

    def test_claim_at_defect_resolves_unknown_via_event_path(self, tmp_path):
        # A marker claiming sequence 13 — AT resolves_unknown_from — must
        # resolve unknown for the event that maps to it, on the SAME
        # defective peer shard as the concrete case above.
        repo = _make_git_repo(tmp_path / "repo")
        self._peer_ids_and_shard(repo)

        # The digest is irrelevant here — well-formedness fails before the
        # digest recompute is even compared — but it is computed honestly
        # (over the 12 ids with sequence <= 13) rather than left deliberately
        # wrong, so this test cannot be accused of forcing unknown via a
        # digest mismatch instead of via the well-formedness gate.
        claim_digest = ts._prefix_digest([f"peer-evt-{i}" for i in range(1, 13)])
        marker_high = {
            "id": "machine-a-fold-high",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:20Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 13, "prefix_digest": claim_digest}},
        }
        event_high = {
            **_event("a-evt-high", "2026-01-01T00:00:21Z"),
            "machine": "machine-a",
            "sequence": 2,
        }
        _write_shard(repo, "machine-a", [marker_high, event_high])

        resolved = resolve_observed_set_for_event(event_high, repo_root=repo)

        assert resolved == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved["peer-a"] is OBSERVED_SET_UNKNOWN

    def test_two_events_same_defective_peer_scope_independently(self, tmp_path):
        # Both claims, both events, one shard, one test: two own-machine
        # events select two DIFFERENT markers (by sequence position) against
        # the SAME defective peer-a shard, and resolve oppositely per the
        # claim each marker carries — the clearest demonstration that scope
        # is per-claim, not per-shard.
        repo = _make_git_repo(tmp_path / "repo")
        peer_ids = self._peer_ids_and_shard(repo)

        low_digest = ts._prefix_digest(peer_ids[:12])
        marker_low = {
            "id": "machine-a-fold-low",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:20Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 12, "prefix_digest": low_digest}},
        }
        event_low = {
            **_event("a-evt-low", "2026-01-01T00:00:21Z"),
            "machine": "machine-a",
            "sequence": 2,
        }
        high_digest = ts._prefix_digest(peer_ids)
        marker_high = {
            "id": "machine-a-fold-high",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 3,
            "observed_at": "2026-01-01T00:00:22Z",
            "applied_at": None,
            "observed_set": {"peer-a": {"sequence": 14, "prefix_digest": high_digest}},
        }
        event_high = {
            **_event("a-evt-high", "2026-01-01T00:00:23Z"),
            "machine": "machine-a",
            "sequence": 4,
        }
        _write_shard(repo, "machine-a", [marker_low, event_low, marker_high, event_high])

        resolved_low = resolve_observed_set_for_event(event_low, repo_root=repo)
        resolved_high = resolve_observed_set_for_event(event_high, repo_root=repo)

        assert resolved_low == {"peer-a": {"sequence": 12, "prefix_digest": low_digest}}
        assert resolved_low["peer-a"] is not OBSERVED_SET_UNKNOWN
        assert resolved_high == {"peer-a": OBSERVED_SET_UNKNOWN}
        assert resolved_high["peer-a"] is OBSERVED_SET_UNKNOWN


# ---------------------------------------------------------------------------
# C3 — AC7: end-to-end property (c). "A persisted advance does not outlive
# the bytes that justified it." Real git operations (git revert), not
# simulated. Spec: docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md
# § Acceptance Criteria AC7, § Tasks C3.
# ---------------------------------------------------------------------------


class TestFoldObservedSetAC7EndToEndRevertProperty:
    def test_reverting_fold_commit_removes_the_advance(self, tmp_path, monkeypatch):
        """fold -> commit -> git revert the fold commit -> the advance is
        gone.

        Git commands actually run against the fixture repo, in order:
          git add -A && git commit -m "chore: peer-a events"
          git add -A && git commit -m "chore: this-machine folds observed_set"
          git rev-parse HEAD                          (captures the fold sha)
          git add -A && git commit -m "chore: this-machine appends own-evt-1"
          git revert --no-commit <fold-sha>
          # append-only JSONL means the fold's line sits at the tail of its
          # own commit and own-evt-1's later append is adjacent to that same
          # tail — git's 3-way merge flags that adjacency as a content
          # conflict even though the two edits don't logically overlap (a
          # well-known git limitation with append-only logs). Real git
          # conflict-resolution completes the revert deterministically:
          git add -A
          git revert --continue   (GIT_EDITOR=true)

        Before the revert: the marker resolves to a CONCRETE set, and
        own-evt-1 (appended after the fold) maps to that marker. After the
        revert: the marker line is gone from this machine's own shard, and
        own-evt-1 — which used to map to it — now resolves to
        OBSERVED_SET_UNKNOWN, because no marker with a lower sequence exists
        any more. Nothing stale survives the revert.
        """
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [
                {**_event(f"peer-evt-{i}", f"2026-01-01T00:00:{i:02d}Z"), "sequence": i, "machine": "peer-a"}
                for i in range(1, 4)
            ],
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "chore: peer-a events")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")
        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"]["peer-a"]["sequence"] == 3
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "chore: this-machine folds observed_set")
        fold_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            check=True,
            text=True,
            creationflags=_NO_WINDOW,
        ).stdout.strip()

        resolved_before = resolve_observed_set(marker, repo_root=repo)
        assert resolved_before == marker["observed_set"]
        assert resolved_before is not OBSERVED_SET_UNKNOWN

        own_event = append_event(_event("own-evt-1", "2026-01-01T00:01:00Z"), repo_root=repo)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "chore: this-machine appends own-evt-1")

        resolved_before_event = resolve_observed_set_for_event(own_event, repo_root=repo)
        assert resolved_before_event == marker["observed_set"], (
            "own-evt-1 must map to the fold's marker before the revert"
        )

        own_shard = shard_path(repo, machine="this-machine")
        revert_result = subprocess.run(
            ["git", "revert", "--no-commit", fold_sha],
            cwd=str(repo),
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
        # Finding 6 (code-reviewer, 2026-07-28): make visible which branch
        # actually fired rather than leaving it to chance — folded into the
        # final assertion messages below so a failure's output always names
        # it, without hard-asserting a specific returncode (whether git's
        # 3-way merge actually conflicts here is itself git-version/
        # strategy-dependent per the reviewer's own finding text; a hard
        # assert on that would risk flaking on an environment where it
        # doesn't, which is the exact hazard this finding flags elsewhere).
        conflict_branch_fired = revert_result.returncode != 0
        if conflict_branch_fired:
            raw_lines = own_shard.read_text(encoding="utf-8").splitlines()
            resolved_lines = [
                line
                for line in raw_lines
                if line.strip() not in ("<<<<<<< HEAD", "=======")
                and not line.startswith(">>>>>>>")
            ]
            resolved_lines = [
                line for line in resolved_lines if json.loads(line).get("id") != marker["id"]
            ]
            own_shard.write_text(
                "".join(line + "\n" for line in resolved_lines), encoding="utf-8"
            )
            _git(repo, "add", "-A")
            revert_env = dict(os.environ)
            revert_env["GIT_EDITOR"] = "true"
            subprocess.run(
                ["git", "revert", "--continue"],
                cwd=str(repo),
                env=revert_env,
                check=True,
                capture_output=True,
                creationflags=_NO_WINDOW,
            )
        else:
            _git(repo, "commit", "--no-edit")

        surviving_ids = {r.get("id") for r in _read_raw_lines(own_shard)}
        assert marker["id"] not in surviving_ids, (
            "the marker must not survive a revert of the commit that appended it "
            f"(conflict_branch_fired={conflict_branch_fired})"
        )
        assert "own-evt-1" in surviving_ids, (
            "the revert must remove only the fold commit's line, not later appends "
            f"(conflict_branch_fired={conflict_branch_fired})"
        )

        resolved_after_event = resolve_observed_set_for_event(own_event, repo_root=repo)
        assert resolved_after_event is OBSERVED_SET_UNKNOWN, (
            "an event that used to map to the reverted marker must now resolve "
            "to unknown — no stale advance may survive"
        )

    def test_reverting_peer_commit_leaves_surviving_marker_unknown(self, tmp_path, monkeypatch):
        """Mirror case: the marker itself SURVIVES, but the peer bytes it
        claimed are reverted — the marker must resolve to unknown, not to a
        trusted set, even though the marker record itself is untouched.

        Git commands actually run against the fixture repo, in order:
          git add -A && git commit -m "chore: peer-a appends 3 events"
          git rev-parse HEAD                          (captures peer's sha)
          git add -A && git commit -m "chore: this-machine folds observed_set"
          git revert --no-edit <peer-sha>
        """
        repo = _make_git_repo(tmp_path / "repo")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "peer-a")
        for i in range(1, 4):
            append_event(_event(f"peer-evt-{i}", f"2026-01-01T00:00:{i:02d}Z"), repo_root=repo)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "chore: peer-a appends 3 events")
        peer_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            check=True,
            text=True,
            creationflags=_NO_WINDOW,
        ).stdout.strip()

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "this-machine")
        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        assert marker["observed_set"]["peer-a"]["sequence"] == 3
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "chore: this-machine folds observed_set")

        resolved_before = resolve_observed_set(marker, repo_root=repo)
        assert resolved_before == marker["observed_set"]
        assert resolved_before is not OBSERVED_SET_UNKNOWN

        _git(repo, "revert", "--no-edit", peer_sha)

        assert max_sequence(repo_root=repo, machine="peer-a") == 0, (
            "peer-a's 3 events must be gone after reverting the commit that appended them"
        )

        resolved_after = resolve_observed_set(marker, repo_root=repo)
        assert resolved_after == {"peer-a": OBSERVED_SET_UNKNOWN}, (
            "a marker whose claimed peer bytes were reverted must resolve to "
            "unknown even though the marker record itself survives untouched"
        )
        assert resolved_after["peer-a"] is OBSERVED_SET_UNKNOWN


# ---------------------------------------------------------------------------
# append_events — C9a (chunk C9's store-suite third): AC1/AC3 batch primitive,
# AC2 atomicity negative control.
#
# Coverage requirements (per plan
# docs/plans/2026-08-11-sat-03-event-sourced-completion-core.md § C9):
#   AC1/AC3 — contiguous sequence run tail+1..tail+N; constant-clock batch
#             proves the chain-off-prior-event rule, not chain-off-original-
#             tail; intra-batch duplicate id and batch-vs-shard duplicate id
#             both write nothing.
#   AC2     — real negative control: a partial-raise inside _mutate leaves
#             the shard byte-identical to before; MutateAbort propagates as
#             a clean no-write abort; a successful call writes all N lines.
#             Exercised on both lock backends reachable from this host.
# ---------------------------------------------------------------------------


class TestAppendEventsAC1AC3ContiguousSequenceAndClock:
    def test_batch_assigns_contiguous_sequence_run_from_tail(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        append_event(_event("evt-seed", "2026-01-01T00:00:00Z"), repo_root=repo)

        batch = [
            _event(f"evt-batch-{i}", f"2026-01-01T00:01:{i:02d}Z") for i in range(5)
        ]
        assigned = append_events(batch, repo_root=repo)

        assert [e["sequence"] for e in assigned] == [2, 3, 4, 5, 6]
        assert max_sequence(repo_root=repo) == 6

        on_disk = _read_raw_lines(shard_path(repo))
        assert [r["sequence"] for r in on_disk] == [1, 2, 3, 4, 5, 6]

    def test_constant_clock_batch_chains_off_prior_event_not_original_tail(
        self, tmp_path, monkeypatch
    ):
        # The discriminating case: with the wall clock pinned CONSTANT, the
        # correct rule (event k chains off event k-1's own just-assigned
        # pair) still produces N strictly-increasing (wall_ms, counter)
        # pairs via the counter bump alone. The broken rule this test would
        # catch — chaining every event off the ORIGINAL shard tail — would
        # instead compute the identical (wall_ms, counter) pair for every
        # single batch member, a silent collision invisible to any
        # moving-clock happy-path test.
        repo = _make_git_repo(tmp_path / "repo")
        monkeypatch.setattr(ts, "_now_ms", lambda: 5_000_000)

        batch = [
            _event(f"evt-const-{i}", f"2026-01-01T00:02:{i:02d}Z") for i in range(4)
        ]
        assigned = append_events(batch, repo_root=repo)

        clocks = [e["logical_clock"] for e in assigned]
        tuples = [(c["wall_ms"], c["counter"]) for c in clocks]

        assert len(set(tuples)) == len(tuples), (
            f"(wall_ms, counter) pairs collided under a constant clock: {tuples} — "
            "this is exactly what chaining off the ORIGINAL tail instead of "
            "the prior batch event would produce"
        )
        assert tuples == sorted(tuples), "clock pairs not strictly increasing"
        assert all(b > a for a, b in zip(tuples, tuples[1:])), (
            "clock pairs not STRICTLY increasing across the batch"
        )
        # All at the same pinned wall_ms; only the counter distinguishes them.
        assert all(wall_ms == 5_000_000 for wall_ms, _ in tuples)
        assert [c for _, c in tuples] == [0, 1, 2, 3]

    def test_empty_batch_is_a_noop_returning_empty_list(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        assert append_events([], repo_root=repo) == []
        assert not shard_path(repo).exists()


class TestAppendEventsAC3DuplicateIdsWriteNothing:
    def test_intra_batch_duplicate_id_raises_and_writes_nothing(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        batch = [
            _event("evt-dup", "2026-01-01T00:00:00Z"),
            _event("evt-dup", "2026-01-01T00:00:01Z"),
        ]
        with pytest.raises(TrackerStoreDuplicateIdError):
            append_events(batch, repo_root=repo)

        assert not shard_path(repo).exists()

    def test_batch_id_colliding_with_existing_shard_id_raises_and_writes_nothing(
        self, tmp_path
    ):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-existing", "2026-01-01T00:00:00Z"), repo_root=repo)
        before = shard_path(repo).read_bytes()

        batch = [
            _event("evt-new", "2026-01-01T00:00:01Z"),
            _event("evt-existing", "2026-01-01T00:00:02Z"),
        ]
        with pytest.raises(TrackerStoreDuplicateIdError):
            append_events(batch, repo_root=repo)

        assert shard_path(repo).read_bytes() == before, (
            "a batch colliding with an existing shard id must write nothing"
        )


class TestAppendEventsAC2AtomicityNegativeControl:
    """The negative control: force a raise partway through _mutate and prove
    the shard is byte-identical to before, on every lock backend reachable
    from this host. A happy-path-only test proves nothing about atomicity —
    see this class's own test names for what each one forces."""

    def test_successful_call_writes_all_n_lines(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        batch = [_event(f"evt-ok-{i}", f"2026-01-01T00:03:{i:02d}Z") for i in range(4)]
        append_events(batch, repo_root=repo)
        assert len(_read_raw_lines(shard_path(repo))) == 4

    def test_raise_partway_through_mutate_leaves_shard_byte_identical(
        self, tmp_path, monkeypatch
    ):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-seed", "2026-01-01T00:00:00Z"), repo_root=repo)
        before = shard_path(repo).read_bytes()

        _orig_locked_rmw = ts.locked_rmw

        def _boom_locked_rmw(target, mutate, **kwargs):
            def _boom_mutate(old_text):
                # Exercise the real assignment/serialization path partway,
                # then blow up before locked_rmw's caller ever sees new
                # text to write — the negative control.
                mutate(old_text)
                raise RuntimeError("simulated partial failure inside _mutate")

            return _orig_locked_rmw(target, _boom_mutate, **kwargs)

        monkeypatch.setattr(ts, "locked_rmw", _boom_locked_rmw)

        batch = [_event(f"evt-boom-{i}", f"2026-01-01T00:04:{i:02d}Z") for i in range(3)]
        with pytest.raises(RuntimeError, match="simulated partial failure"):
            append_events(batch, repo_root=repo)

        after = shard_path(repo).read_bytes()
        assert after == before, "shard bytes changed despite a raise inside _mutate"

    def test_mutate_abort_propagates_as_clean_no_write_abort(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-seed", "2026-01-01T00:00:00Z"), repo_root=repo)
        before = shard_path(repo).read_bytes()

        _orig_locked_rmw = ts.locked_rmw

        def _abort_locked_rmw(target, mutate, **kwargs):
            def _abort_mutate(old_text):
                mutate(old_text)
                raise MutateAbort("simulated clean abort")

            return _orig_locked_rmw(target, _abort_mutate, **kwargs)

        monkeypatch.setattr(ts, "locked_rmw", _abort_locked_rmw)

        batch = [_event("evt-abort-1", "2026-01-01T00:05:00Z")]
        with pytest.raises(MutateAbort):
            append_events(batch, repo_root=repo)

        after = shard_path(repo).read_bytes()
        assert after == before, "shard bytes changed despite a MutateAbort"

    @pytest.mark.skipif(
        not _FCNTL_AVAILABLE, reason="POSIX fcntl.flock backend not reachable on this host"
    )
    def test_atomicity_on_posix_fcntl_backend(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-seed", "2026-01-01T00:00:00Z"), repo_root=repo)
        before = shard_path(repo).read_bytes()

        from coordinator_core import locked_write as lw

        assert lw._FCNTL_AVAILABLE, "POSIX backend must be the active locked_rmw backend here"

        _orig_locked_rmw = ts.locked_rmw

        def _boom_locked_rmw(target, mutate, **kwargs):
            def _boom_mutate(old_text):
                mutate(old_text)
                raise RuntimeError("simulated partial failure (posix backend)")

            return _orig_locked_rmw(target, _boom_mutate, **kwargs)

        monkeypatch.setattr(ts, "locked_rmw", _boom_locked_rmw)

        batch = [_event("evt-posix-1", "2026-01-01T00:06:00Z")]
        with pytest.raises(RuntimeError, match="simulated partial failure"):
            append_events(batch, repo_root=repo)

        assert shard_path(repo).read_bytes() == before

    @pytest.mark.skipif(
        not _MSVCRT_AVAILABLE, reason="Windows msvcrt.locking backend not reachable on this host"
    )
    def test_atomicity_on_windows_msvcrt_backend(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-seed", "2026-01-01T00:00:00Z"), repo_root=repo)
        before = shard_path(repo).read_bytes()

        from coordinator_core import locked_write as lw

        assert lw._MSVCRT_AVAILABLE, "Windows backend must be the active locked_rmw backend here"

        _orig_locked_rmw = ts.locked_rmw

        def _boom_locked_rmw(target, mutate, **kwargs):
            def _boom_mutate(old_text):
                mutate(old_text)
                raise RuntimeError("simulated partial failure (msvcrt backend)")

            return _orig_locked_rmw(target, _boom_mutate, **kwargs)

        monkeypatch.setattr(ts, "locked_rmw", _boom_locked_rmw)

        batch = [_event("evt-msvcrt-1", "2026-01-01T00:07:00Z")]
        with pytest.raises(RuntimeError, match="simulated partial failure"):
            append_events(batch, repo_root=repo)

        assert shard_path(repo).read_bytes() == before

    @pytest.mark.skipif(
        not _MSVCRT_AVAILABLE, reason="Windows msvcrt.locking backend not reachable on this host"
    )
    def test_msvcrt_lock_is_mandatory_blocks_a_second_fds_read(self, tmp_path, monkeypatch):
        """Backend-differentiated regression, per the plan's C9 note: Windows'
        msvcrt.locking is a MANDATORY byte-range lock — unlike POSIX flock,
        it also blocks a second, independently-opened fd's plain read() into
        the locked byte range, not just a competing locking() call. This is
        the actual risk `test_atomicity_on_windows_msvcrt_backend` (above) was
        meant to distinguish from the POSIX leg but only checked backend
        identity for; this test exercises the mandatory-lock behavior itself
        by opening a second fd to the same lock sidecar mid-``_mutate`` and
        asserting the read is blocked while the msvcrt lock is held.
        """
        from coordinator_core import locked_write as lw

        repo = _make_git_repo(tmp_path / "repo")
        append_event(_event("evt-seed", "2026-01-01T00:00:00Z"), repo_root=repo)

        target = shard_path(repo)
        lock_path = lw._lock_dir(repo) / f"{lw._lock_key(target)}.lock"

        _orig_locked_rmw = ts.locked_rmw
        observed: dict = {}

        def _probing_locked_rmw(target, mutate, **kwargs):
            def _probing_mutate(old_text):
                # The outer locked_rmw call already holds the msvcrt lock on
                # lock_path's byte 0 at this point. Open a second, independent
                # fd to the same lock file and try to read byte 0 from it —
                # a mandatory lock blocks this; an advisory one would not.
                second_fd = os.open(str(lock_path), os.O_RDONLY)
                try:
                    os.lseek(second_fd, 0, os.SEEK_SET)
                    try:
                        os.read(second_fd, 1)
                        observed["blocked"] = False
                    except OSError:
                        observed["blocked"] = True
                finally:
                    os.close(second_fd)
                return mutate(old_text)

            return _orig_locked_rmw(target, _probing_mutate, **kwargs)

        monkeypatch.setattr(ts, "locked_rmw", _probing_locked_rmw)

        batch = [_event("evt-msvcrt-mandatory-1", "2026-01-01T00:08:00Z")]
        append_events(batch, repo_root=repo)

        assert observed.get("blocked") is True, (
            "a second fd's read() into the locked byte range was not blocked — "
            "msvcrt.locking should be a mandatory lock, not merely advisory"
        )


# ---------------------------------------------------------------------------
# tmrg-03 C3 — compare_observed_set_vectors / compare_events_causal_order
# ---------------------------------------------------------------------------


def _concrete(sequence: int, event_ids: list[str]) -> dict:
    """Build a concrete ``{sequence, prefix_digest}`` component the same way
    ``fold_observed_set``/``resolve_observed_set`` do — via the pinned
    ``_prefix_digest`` over *event_ids*."""
    return {"sequence": sequence, "prefix_digest": ts._prefix_digest(event_ids)}


class TestCompareObservedSetVectorsA14Table:
    """One test per row of cockpit's § MIXED-TYPE DOMINATION COMPARE (A14)
    table, plus the whole-vector-unknown case (a bare ``OBSERVED_SET_UNKNOWN``
    side — no marker was ever in effect)."""

    def test_key_absent_on_one_side_compares_below_present_no_digest_test(self):
        vector_a = {"peer-a": _concrete(3, ["e1", "e2", "e3"])}
        vector_b: dict = {}
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_A_DOMINATES
        assert compare_observed_set_vectors(vector_b, vector_a) == CAUSAL_ORDER_B_DOMINATES

    def test_either_side_unknown_component_propagates_to_indeterminate(self):
        vector_a = {"peer-a": OBSERVED_SET_UNKNOWN, "peer-b": _concrete(1, ["e1"])}
        vector_b = {"peer-a": _concrete(1, ["e1"]), "peer-b": _concrete(1, ["e1"])}
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_INDETERMINATE
        assert compare_observed_set_vectors(vector_b, vector_a) == CAUSAL_ORDER_INDETERMINATE

    def test_unknown_component_is_never_outvoted_by_a_decided_component(self):
        # peer-a: A's component is unknown against B's concrete claim (undecided).
        # peer-b: A strictly dominates B (a decided "gt"). The unknown must win
        # the whole-axis answer regardless of the decided component elsewhere.
        vector_a = {
            "peer-a": OBSERVED_SET_UNKNOWN,
            "peer-b": _concrete(5, ["e1", "e2", "e3", "e4", "e5"]),
        }
        vector_b = {
            "peer-a": _concrete(2, ["p1", "p2"]),
            "peer-b": _concrete(1, ["e1"]),
        }
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_INDETERMINATE

    def test_both_present_sequence_differs_orders_on_sequence_digests_ignored(self):
        # Same sequence-order outcome under two UNRELATED digests — proves
        # digests carry no signal when sequence alone already decides.
        vector_a = {"peer-a": _concrete(5, ["a", "b", "c", "d", "e"])}
        vector_b = {"peer-a": _concrete(3, ["x", "y", "z"])}
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_A_DOMINATES

    def test_both_present_sequence_equal_digest_equal_is_equal(self):
        vector_a = {"peer-a": _concrete(4, ["e1", "e2", "e3", "e4"])}
        vector_b = {"peer-a": _concrete(4, ["e1", "e2", "e3", "e4"])}
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_EQUAL

    def test_both_present_sequence_equal_digest_differs_resolves_unknown(self):
        # The row that earns the digest's participation: same claimed
        # sequence, divergent bytes (a rebase-and-re-append shape) — must
        # resolve unknown/indeterminate, never equal and never a trusted
        # dominates.
        vector_a = {"peer-a": _concrete(3, ["e1", "e2", "e3"])}
        vector_b = {"peer-a": _concrete(3, ["r1", "r2", "r3"])}
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_INDETERMINATE

    def test_whole_vector_bare_unknown_sentinel_is_indeterminate(self):
        vector_b = {"peer-a": _concrete(1, ["e1"])}
        assert compare_observed_set_vectors(OBSERVED_SET_UNKNOWN, vector_b) == CAUSAL_ORDER_INDETERMINATE
        assert compare_observed_set_vectors(vector_b, OBSERVED_SET_UNKNOWN) == CAUSAL_ORDER_INDETERMINATE
        assert (
            compare_observed_set_vectors(OBSERVED_SET_UNKNOWN, OBSERVED_SET_UNKNOWN)
            == CAUSAL_ORDER_INDETERMINATE
        )

    def test_both_empty_vectors_are_equal(self):
        assert compare_observed_set_vectors({}, {}) == CAUSAL_ORDER_EQUAL

    def test_genuine_antichain_is_concurrent_not_indeterminate(self):
        # peer-a: A ahead of B. peer-b: B ahead of A. Neither dominates, and
        # nothing is unknown — a DECIDED concurrent finding, distinct from
        # the missing-information indeterminate case.
        vector_a = {
            "peer-a": _concrete(3, ["a1", "a2", "a3"]),
            "peer-b": _concrete(1, ["b1"]),
        }
        vector_b = {
            "peer-a": _concrete(1, ["a1"]),
            "peer-b": _concrete(2, ["b1", "b2"]),
        }
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_CONCURRENT
        assert compare_observed_set_vectors(vector_b, vector_a) == CAUSAL_ORDER_CONCURRENT

    def test_one_unknown_component_and_one_absent_component_is_indeterminate(self):
        # peer-a: A's component is unknown. peer-b: absent from A entirely
        # (present only on B). Neither is the "both present" case — proves
        # the unknown-first check still wins even when the OTHER key in the
        # same vector pair is deciding via the absent row, not a concrete
        # value.
        vector_a = {"peer-a": OBSERVED_SET_UNKNOWN}
        vector_b = {"peer-a": _concrete(1, ["e1"]), "peer-b": _concrete(1, ["e1"])}
        assert compare_observed_set_vectors(vector_a, vector_b) == CAUSAL_ORDER_INDETERMINATE
        assert compare_observed_set_vectors(vector_b, vector_a) == CAUSAL_ORDER_INDETERMINATE


class TestCompareObservedSetComponentDegenerateBranches:
    """The two rows _compare_observed_set_component's own docstring names as
    "not exercised in practice" — both sides absent, and a present
    sequence-0 component against an absent side. Calls the private helper
    directly since ``compare_observed_set_vectors`` only ever invokes it for
    a key present on at least one side, so these shapes cannot be reached
    through the public entrypoint at all."""

    def test_both_sides_absent_is_eq(self):
        assert (
            ts._compare_observed_set_component(ts._ABSENT, ts._ABSENT)
            == "eq"
        )

    def test_present_sequence_zero_against_absent_is_eq_no_digest_test(self):
        zero_component = {"sequence": 0, "prefix_digest": "irrelevant-never-read"}
        assert (
            ts._compare_observed_set_component(zero_component, ts._ABSENT)
            == "eq"
        )
        assert (
            ts._compare_observed_set_component(ts._ABSENT, zero_component)
            == "eq"
        )


class TestCompareEventsCausalOrderFallback:
    """The event-level entrypoint: causal vector decides when it can, the
    stable ``(machine, id)`` key decides everything the vector compare
    proves incomparable — asserted stable and deterministic, and never
    touching ``applied_at`` or a vector's ``sequence`` component."""

    def test_dominating_vector_decides_direction_over_key_order(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")

        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )

        early_marker = {
            "id": "machine-a-fold-1111111111111111",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:01Z",
            "applied_at": None,
            "observed_set": {
                "peer-a": {"sequence": 1, "prefix_digest": ts._prefix_digest(["peer-evt-1"])}
            },
        }
        # event_z's own key ("machine-a", "z-evt") sorts AFTER event_a's own
        # key ("machine-a", "a-evt") lexicographically, but event_z carries
        # NO marker at all (predates any fold) while event_a's marker
        # observed peer-a — so the vector compare must decide via
        # indeterminate-vs-decided, not the reverse of what the key alone
        # would say. Use two events that both DO have a marker in effect,
        # with peer-a growing between them, to prove domination overrides
        # any key-order intuition.
        applied = {
            **_event("z-evt", "2026-01-01T00:00:03Z", applied_at="2026-01-01T00:00:03Z"),
            "machine": "machine-a",
            "sequence": 2,
        }
        _write_shard(repo, "machine-a", [early_marker, applied])

        _write_shard(
            repo,
            "peer-a",
            [
                {**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"},
                {**_event("peer-evt-2", "2026-01-01T00:00:01Z"), "sequence": 2, "machine": "peer-a"},
            ],
        )
        later_marker = {
            "id": "machine-b-fold-2222222222222222",
            "kind": "observed_set_fold",
            "machine": "machine-b",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:04Z",
            "applied_at": None,
            "observed_set": {
                "peer-a": {
                    "sequence": 2,
                    "prefix_digest": ts._prefix_digest(["peer-evt-1", "peer-evt-2"]),
                }
            },
        }
        b_applied = {
            **_event("a-evt", "2026-01-01T00:00:05Z", applied_at="2026-01-01T00:00:05Z"),
            "machine": "machine-b",
            "sequence": 2,
        }
        _write_shard(repo, "machine-b", [later_marker, b_applied])

        # b_applied's marker observed peer-a at sequence 2, strictly beyond
        # applied's marker (peer-a at sequence 1) — b_applied dominates.
        # b_applied's OWN key ("machine-b", "a-evt") sorts BEFORE applied's
        # key ("machine-a", "z-evt") lexicographically on machine alone —
        # proving domination, not key order, decided this pair.
        assert compare_events_causal_order(applied, b_applied, repo_root=repo) == -1
        assert compare_events_causal_order(b_applied, applied, repo_root=repo) == 1

    def test_incomparable_pair_falls_through_to_stable_machine_id_key(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        # Neither event has a marker in effect (no fold has happened) — both
        # vectors are the bare OBSERVED_SET_UNKNOWN sentinel, indeterminate,
        # so order must come from the (machine, id) fallback only.
        _write_shard(
            repo,
            "machine-a",
            [{**_event("evt-a", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:00Z"), "sequence": 1, "machine": "machine-a"}],
        )
        _write_shard(
            repo,
            "machine-b",
            [{**_event("evt-b", "2026-01-01T00:00:01Z", applied_at="2026-01-01T00:00:01Z"), "sequence": 1, "machine": "machine-b"}],
        )
        event_a = {"machine": "machine-a", "sequence": 1, "id": "evt-a"}
        event_b = {"machine": "machine-b", "sequence": 1, "id": "evt-b"}

        result_ab = compare_events_causal_order(event_a, event_b, repo_root=repo)
        result_ba = compare_events_causal_order(event_b, event_a, repo_root=repo)
        assert result_ab == -1, "machine-a sorts before machine-b lexicographically"
        assert result_ba == 1
        # Deterministic across repeated calls.
        assert compare_events_causal_order(event_a, event_b, repo_root=repo) == result_ab

    def test_same_event_compared_with_itself_is_zero(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        event = {"machine": "machine-a", "sequence": 1, "id": "evt-a"}
        assert compare_events_causal_order(event, event, repo_root=repo) == 0

    def test_equal_vectors_fall_through_to_key_not_reported_as_decided(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "peer-a",
            [{**_event("peer-evt-1", "2026-01-01T00:00:00Z"), "sequence": 1, "machine": "peer-a"}],
        )
        marker_a = {
            "id": "machine-a-fold-3333333333333333",
            "kind": "observed_set_fold",
            "machine": "machine-a",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:01Z",
            "applied_at": None,
            "observed_set": {
                "peer-a": {"sequence": 1, "prefix_digest": ts._prefix_digest(["peer-evt-1"])}
            },
        }
        event_a = {
            **_event("a-evt", "2026-01-01T00:00:02Z", applied_at="2026-01-01T00:00:02Z"),
            "machine": "machine-a",
            "sequence": 2,
        }
        _write_shard(repo, "machine-a", [marker_a, event_a])

        marker_b = {
            "id": "machine-b-fold-4444444444444444",
            "kind": "observed_set_fold",
            "machine": "machine-b",
            "sequence": 1,
            "observed_at": "2026-01-01T00:00:01Z",
            "applied_at": None,
            "observed_set": {
                "peer-a": {"sequence": 1, "prefix_digest": ts._prefix_digest(["peer-evt-1"])}
            },
        }
        event_b = {
            **_event("b-evt", "2026-01-01T00:00:02Z", applied_at="2026-01-01T00:00:02Z"),
            "machine": "machine-b",
            "sequence": 2,
        }
        _write_shard(repo, "machine-b", [marker_b, event_b])

        # Both events' peer-only vectors are identical ({peer-a: seq 1,
        # same digest}) — the vector compare proves EQUAL, which must still
        # fall through to the stable key rather than being treated as a
        # decided direction.
        assert (
            compare_observed_set_vectors(
                resolve_observed_set_for_event(event_a, repo_root=repo),
                resolve_observed_set_for_event(event_b, repo_root=repo),
            )
            == CAUSAL_ORDER_EQUAL
        )
        assert compare_events_causal_order(event_a, event_b, repo_root=repo) == -1
        assert compare_events_causal_order(event_b, event_a, repo_root=repo) == 1

    def test_missing_id_on_fallback_key_raises_trackerstoreerror(self, tmp_path):
        # Both events resolve to the bare unknown sentinel (no marker ever
        # existed), so the compare falls through to the (machine, id)
        # fallback key — where event_b is missing "id" entirely. Must raise
        # a named TrackerStoreError, never an incidental TypeError from
        # comparing a tuple containing None against one containing a str,
        # and never silently fabricate a default identity.
        repo = _make_git_repo(tmp_path / "repo")
        event_a = {"machine": "machine-a", "sequence": 1, "id": "evt-a"}
        event_b = {"machine": "machine-b", "sequence": 1}
        with pytest.raises(ts.TrackerStoreError):
            compare_events_causal_order(event_a, event_b, repo_root=repo)

    def test_missing_machine_on_fallback_key_raises_trackerstoreerror(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        event_a = {"machine": "machine-a", "sequence": 1, "id": "evt-a"}
        event_b = {"sequence": 1, "id": "evt-b"}
        with pytest.raises(ts.TrackerStoreError):
            compare_events_causal_order(event_a, event_b, repo_root=repo)


class TestSelfComponentUnionC4:
    """tmrg-03 C4 — ``resolve_observed_set_union_for_event``: cockpit's
    worked example (§ SELF-COMPONENT RECONCILIATION (A10)) for two
    same-machine applied events sharing one fold marker, own shard made
    contiguous, plus the sibling case where the discriminating slot is
    genuinely absent (§ SELF-SHARD APPLICABILITY, DEC-5)."""

    @staticmethod
    def _build_own_shard(repo, *, materialize_14: bool):
        """Machine ``machine-m`` folds once (marker ``F`` at own-shard
        sequence 12, observing no peers), then two applied transitions on
        the same ``(item_id, axis)`` share that one fold window: ``E1`` at
        own-shard sequence 13, ``E2`` at own-shard sequence 15. Sequence 14
        is either a genuinely materialized unrelated/different-axis event
        (own shard contiguous 1..15 — the case that discriminates the
        marker-placement bug) or genuinely absent (own shard defective from
        position 14 — the sibling case that must resolve indeterminate, not
        antichain 1)."""
        records = [
            {
                **_event(
                    f"m-evt-{i}",
                    f"2026-01-01T00:{i:02d}:00Z",
                    applied_at=f"2026-01-01T00:{i:02d}:00Z",
                ),
                "sequence": i,
                "machine": "machine-m",
            }
            for i in range(1, 12)
        ]
        records.append(
            {
                "id": "machine-m-fold-aaaaaaaaaaaaaaaa",
                "kind": "observed_set_fold",
                "machine": "machine-m",
                "sequence": 12,
                "observed_at": "2026-01-01T00:12:00Z",
                "applied_at": None,
                "observed_set": {},
            }
        )
        e1 = {
            **_event("evt-a1", "2026-01-01T00:13:00Z", applied_at="2026-01-01T00:13:00Z"),
            "machine": "machine-m",
            "sequence": 13,
            "item_id": "item-x",
            "axis": "qa",
        }
        records.append(e1)
        if materialize_14:
            records.append(
                {
                    **_event(
                        "evt-other", "2026-01-01T00:14:00Z", applied_at="2026-01-01T00:14:00Z"
                    ),
                    "machine": "machine-m",
                    "sequence": 14,
                    "item_id": "item-y",
                    "axis": "docs",
                }
            )
        e2 = {
            **_event("evt-a2", "2026-01-01T00:15:00Z", applied_at="2026-01-01T00:15:00Z"),
            "machine": "machine-m",
            "sequence": 15,
            "item_id": "item-x",
            "axis": "qa",
        }
        records.append(e2)
        _write_shard(repo, "machine-m", records)
        return e1, e2

    def test_slot_materialized_e2_dominates_e1_no_false_conflict(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        e1, e2 = self._build_own_shard(repo, materialize_14=True)

        union_e1 = resolve_observed_set_union_for_event(e1, repo_root=repo)
        union_e2 = resolve_observed_set_union_for_event(e2, repo_root=repo)

        # The bug this chunk exists to prevent: if the self-component were
        # taken from the marker's own placement rather than each event's
        # own record, both e1 and e2 would carry the SAME self-component
        # (F's own placement) and the axis would be falsely reported
        # CAUSAL_ORDER_CONCURRENT. Under the per-event rule the later event
        # strictly dominates — a decided antichain of cardinality 1, E2
        # alone.
        assert compare_observed_set_vectors(union_e1, union_e2) == CAUSAL_ORDER_B_DOMINATES
        assert compare_observed_set_vectors(union_e2, union_e1) == CAUSAL_ORDER_A_DOMINATES
        assert compare_events_causal_order(e1, e2, repo_root=repo) == -1
        assert compare_events_causal_order(e2, e1, repo_root=repo) == 1

    def test_slot_absent_resolves_indeterminate_not_dominates_or_conflicted(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        e1, e2 = self._build_own_shard(repo, materialize_14=False)

        union_e1 = resolve_observed_set_union_for_event(e1, repo_root=repo)
        union_e2 = resolve_observed_set_union_for_event(e2, repo_root=repo)

        # e2's OWN sequence (15) is at/after its own shard's first defect
        # (the genuine hole at 14) — its self-component must resolve
        # OBSERVED_SET_UNKNOWN, never a trusted antichain-1 (that would be
        # trusting a defective own shard) and never CAUSAL_ORDER_CONCURRENT
        # (that would collapse unknown into a false positive).
        assert union_e2["machine-m"] is OBSERVED_SET_UNKNOWN
        assert union_e1["machine-m"] is not OBSERVED_SET_UNKNOWN
        assert compare_observed_set_vectors(union_e1, union_e2) == CAUSAL_ORDER_INDETERMINATE
        assert compare_observed_set_vectors(union_e2, union_e1) == CAUSAL_ORDER_INDETERMINATE

    def test_union_is_indeterminate_when_no_marker_ever_existed(self, tmp_path):
        # resolve_observed_set_for_event's own no-marker case (bare
        # OBSERVED_SET_UNKNOWN sentinel) must propagate through the union
        # unchanged — there is no peer payload to union a self-component
        # onto.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "machine-solo",
            [
                {
                    **_event(
                        "solo-evt", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:00Z"
                    ),
                    "machine": "machine-solo",
                    "sequence": 1,
                }
            ],
        )
        event = {"machine": "machine-solo", "sequence": 1, "id": "solo-evt"}
        assert resolve_observed_set_union_for_event(event, repo_root=repo) is OBSERVED_SET_UNKNOWN


class TestCompareEventsCausalOrderAC8ReadEventsUnaffected:
    """AC8: this comparator must not perturb ``read_events``' own contract —
    a negative check that it takes no *sort*, *filter*, or *repo_root
    default* parameter overlapping ``read_events``'s signature."""

    def test_read_events_signature_and_sort_key_unchanged(self, tmp_path):
        import inspect

        repo = _make_git_repo(tmp_path / "repo")
        applied = append_event(
            _event("evt-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:00Z"),
            repo_root=repo,
        )
        marker = fold_observed_set(repo_root=repo)
        assert marker is None or marker is not None  # fold is a no-op with no peers; either is fine

        events = read_events(repo_root=repo)
        assert events == [applied], "read_events output unaffected by the comparator's existence"

        sig = inspect.signature(read_events)
        assert list(sig.parameters) == ["repo_root"], "read_events must not gain a new parameter"


# ---------------------------------------------------------------------------
# tmrg-03 C1b — check-time conformance harness against cockpit's live oracle
# ---------------------------------------------------------------------------
#
# TEST-ONLY: this is the one place in the repo allowed to resolve a peer
# clone. tracker_store.py's runtime path (``_well_formedness`` itself, C1a)
# must never do this — see the module docstring on ``_well_formedness`` and
# CLAUDE.md's install-surface-completeness rule. On a machine with no
# cockpit clone (or an cockpit clone missing the fixture), this SKIPS
# loudly with a named reason; it must never hard-fail (a fresh clean clone
# of claude-klabauter has no reason to have example-cockpit-repo checked out) and must
# never silently pass.


_COCKPIT_SKIP_REASON = (
    "cockpit conformance fixture unresolvable: registry key "
    "'repos.example_cockpit_repo' did not resolve a clone with "
    "docs/reference/tracker-well-formedness-vectors.json — "
    "skipping the live conformance check (not a failure; "
    "claude-klabauter's suite must not depend on a peer repo being present on this box)"
)


def _cockpit_conformance_vectors_path():
    """Resolve cockpit's live conformance fixture via the machine-local
    registry (the same direct-tomllib ``registry_get`` seam
    ``doe_root_pointer.py`` binds ``repos.doe_claude`` reads to — reused
    here rather than authoring a second resolver). Returns the resolved
    ``Path`` or ``None`` if the clone or the fixture file can't be found.
    """
    from coordinator_core.machine_resolver import registry_get

    cockpit_root = registry_get("repos.example_cockpit_repo")
    if not cockpit_root:
        return None
    path = Path(cockpit_root) / "docs" / "reference" / "tracker-well-formedness-vectors.json"
    if not path.is_file():
        return None
    return path


def _require_conformance_vectors_path():
    """Resolve the fixture or skip loudly with the named reason — the one
    call site both the real conformance test and the skip-path proof test
    share, so the proof test exercises the actual skip, not a re-statement
    of it."""
    fixture_path = _cockpit_conformance_vectors_path()
    if fixture_path is None:
        pytest.skip(_COCKPIT_SKIP_REASON)
    return fixture_path


class TestWellFormednessConformanceAgainstCockpitOracle:
    """C1b: ``ts._well_formedness`` (C1a) must agree with cockpit's A12
    oracle on every vector in their live fixture. DEC-1/DR-210: read live
    off their clone at check time, never co-vendored here — a copy would
    drift in lockstep with our pinned implementation and this check would
    keep reporting conformant against a stale contract."""

    @pytest.mark.real_home
    def test_agrees_with_every_conformance_vector(self):
        fixture_path = _require_conformance_vectors_path()
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert fixture["predicate"] == "well_formedness", (
            f"conformance fixture predicate changed shape: {fixture['predicate']!r} "
            "(expected 'well_formedness' — contract drift, not a vector mismatch)"
        )
        assert fixture["version"] == 1, (
            f"conformance fixture version changed: {fixture['version']!r} "
            "(expected version 1 — this harness was built against version 1; "
            "a version bump needs its own look, not a silent re-run)"
        )

        vectors = fixture["vectors"]
        assert vectors, "conformance fixture has no vectors — fixture is empty or malformed"

        for vector in vectors:
            name = vector["name"]
            sequences = vector["sequences"]  # file order, as given — never sorted
            first_defect, resolves_unknown_from = ts._well_formedness(sequences)
            assert first_defect == vector["first_defect"], (
                f"vector {name!r}: first_defect mismatch — claude-klabauter returned "
                f"{first_defect!r}, cockpit's oracle says {vector['first_defect']!r}"
            )
            assert resolves_unknown_from == vector["resolves_unknown_from"], (
                f"vector {name!r}: resolves_unknown_from mismatch — claude-klabauter returned "
                f"{resolves_unknown_from!r}, cockpit's oracle says "
                f"{vector['resolves_unknown_from']!r}"
            )

    def test_skips_loudly_when_clone_unresolvable(self, monkeypatch):
        # Proves the skip path itself: point resolution at a machine with no
        # cockpit clone registered and confirm the SAME helper the real
        # conformance test calls actually raises pytest's skip exception,
        # with its named reason — never a hard fail, never a silent pass.
        import coordinator_core.machine_resolver as machine_resolver

        monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
        assert _cockpit_conformance_vectors_path() is None, (
            "resolution helper must return None when the registry key doesn't resolve"
        )

        with pytest.raises(pytest.skip.Exception) as exc_info:
            _require_conformance_vectors_path()
        assert "repos.example_cockpit_repo" in str(exc_info.value)


class TestAC4NoVendoredPredicateOrRuntimeCrossRepoRead:
    """AC4's other half, not asserted anywhere above: "No copy of
    cockpit's predicate exists in this repo ... [runtime] fails loud when
    it cannot [resolve their clone]." C1b proves the consumption path
    (test-only, skip-loudly). This class proves the two things C1b's own
    passing does not: that ``tracker_store.py``'s RUNTIME path performs no
    cross-repo read at all (the machine-resolver call lives only in the
    test file, never in the module under test), and that cockpit's fixture
    is not co-vendored into this tree — the drift-blind shape DEC-1 and
    Anti-scope both refuse (a local copy would drift in lockstep with our
    pinned predicate and this check would keep reporting conformant
    against a stale contract)."""

    @staticmethod
    def _imports_cross_repo_resolver(tree: "ast.AST") -> "list[str]":
        # Walks alias names (not just the dotted module path), matching the
        # existing precedent in this file (`_mentions_tracker`,
        # `_references_tracker_store_in_code` above): `import machine_resolver`
        # and `from x import y as z` both bind a local name that a
        # module-path-only check would miss (Review: slice-E integration,
        # 2026-08-18 — `from coordinator_core import machine_resolver` has
        # module="coordinator_core" and would evade a `node.module` check
        # entirely; only the alias itself names `machine_resolver`).
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.add(node.module)
                for alias in node.names:
                    imported_names.add(alias.name)
                    imported_names.add(alias.asname or alias.name)
        return sorted(name for name in imported_names if "machine_resolver" in name)

    @staticmethod
    def _registry_lookup_calls(tree: "ast.AST") -> "list[str]":
        # Belt-and-suspenders on the same claim, at the AST-call level rather
        # than the import level. Checks BOTH call shapes a `registry_get`
        # reference can take — a bare `Name` (`registry_get(...)`) and an
        # `Attribute` access (`machine_resolver.registry_get(...)` or an
        # aliased `mr.registry_get(...)`) — since a Name-only check lets any
        # attribute-style call through undetected (Review: slice-E
        # integration, 2026-08-18).
        offending_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and "registry_get" in func.id:
                offending_calls.append(ast.dump(func))
            elif isinstance(func, ast.Attribute) and "registry_get" in func.attr:
                offending_calls.append(ast.dump(func))
        return offending_calls

    def test_tracker_store_module_imports_no_cross_repo_resolver(self):
        # tracker_store.py's own top-level imports must not include
        # machine_resolver (or any cross-repo resolution machinery) — if
        # they did, _well_formedness's runtime callers (resolve_observed_set,
        # resolve_observed_set_for_event) would risk a cross-repo dependency
        # on a path that runs at session-boot cadence, on a machine that may
        # have no cockpit clone at all.
        source = ts.__file__ and Path(ts.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = self._imports_cross_repo_resolver(tree)

        assert offenders == [], (
            f"tracker_store.py imports cross-repo resolution machinery: {offenders!r} "
            "— the well-formedness predicate must run entirely in-process"
        )

    def test_well_formedness_and_resolvers_call_no_registry_lookup(self):
        # Belt-and-suspenders on the same claim, at the AST-call level rather
        # than the import level: no call anywhere in the module named
        # registry_get / machine_resolver, so a runtime path could not reach
        # cross-repo resolution even via a deferred/local import.
        source = Path(ts.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offending_calls = self._registry_lookup_calls(tree)
        assert offending_calls == [], (
            f"tracker_store.py calls registry resolution at runtime: {offending_calls!r}"
        )

    def test_import_check_catches_from_coordinator_core_import_machine_resolver(self):
        # Proves the tightened import guard actually has teeth: a synthetic
        # module using the exact bypass shape the review flagged —
        # `from coordinator_core import machine_resolver`, whose `node.module`
        # is "coordinator_core" and would evade a module-path-only check —
        # must still be caught via the alias walk.
        synthetic = "from coordinator_core import machine_resolver\n"
        tree = ast.parse(synthetic)
        assert self._imports_cross_repo_resolver(tree) == ["machine_resolver"]

    def test_call_check_catches_attribute_style_registry_get(self):
        # Proves the tightened call guard has teeth against both
        # attribute-call bypass shapes the review flagged: an unaliased
        # `machine_resolver.registry_get(...)` and an aliased
        # `mr.registry_get(...)`, neither of which is an `ast.Name` callee.
        synthetic = (
            "machine_resolver.registry_get('repos.example_cockpit_repo')\n"
            "mr.registry_get('repos.example_cockpit_repo')\n"
        )
        tree = ast.parse(synthetic)
        offenders = self._registry_lookup_calls(tree)
        assert len(offenders) == 2

    def test_no_vendored_copy_of_cockpits_conformance_fixture_in_this_repo(self):
        # DEC-1 / Anti-scope: the fixture is read live off cockpit's clone
        # at check-time only (see C1b above), never co-vendored. A file
        # named after their fixture anywhere under coordinator_core/ or
        # docs/ in THIS repo would be exactly the drift-blind shape refused.
        repo_root = Path(ts.__file__).resolve().parents[1]
        hits = [
            p
            for base in (repo_root / "coordinator_core", repo_root / "docs")
            if base.exists()
            for p in base.rglob("tracker-well-formedness-vectors.json")
        ]
        assert hits == [], (
            f"cockpit's conformance fixture is co-vendored into this repo: {hits!r} "
            "— DEC-1/Anti-scope require it be read live off their clone, never copied here"
        )


# ---------------------------------------------------------------------------
# C7 (sat-06) — rotation-on-close, per docs/plans/2026-08-18-sat-06-cockpit-
# consumption-seam.md § RULING 2026-08-20 "C7 is rotation-on-close, not
# partition-on-write". append_event/append_events are untouched; rotate_month
# is a standalone maintenance step, and read_events/fold_observed_set/
# resolve_observed_set are taught to merge the flat live shard with any
# rotated <YYYY-MM>/events.<slug>.jsonl partitions.
# ---------------------------------------------------------------------------


class TestRotateMonthRelocatesClosedMonth:
    def test_rotated_month_moves_out_of_live_shard_into_month_dir(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [
                {**_event("evt-1", "2026-06-01T00:00:00Z"), "sequence": 1},
                {**_event("evt-2", "2026-07-15T00:00:00Z"), "sequence": 2},
                {**_event("evt-3", "2026-07-20T00:00:00Z"), "sequence": 3},
            ],
        )

        moved = rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert moved == 2

        live_ids = [r["id"] for r in _read_raw_lines(shard_path(repo, machine="host-a"))]
        assert live_ids == ["evt-1"]

        rotated_path = repo / EVENTS_DIR_RELPATH / "2026-07" / "events.host-a.jsonl"
        rotated_ids = [r["id"] for r in _read_raw_lines(rotated_path)]
        assert rotated_ids == ["evt-2", "evt-3"]

    def test_rotate_month_is_idempotent(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [{**_event("evt-1", "2026-07-15T00:00:00Z"), "sequence": 1}],
        )

        first = rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert first == 1

        second = rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert second == 0

        rotated_path = repo / EVENTS_DIR_RELPATH / "2026-07" / "events.host-a.jsonl"
        rotated_ids = [r["id"] for r in _read_raw_lines(rotated_path)]
        assert rotated_ids == ["evt-1"], "second rotation must not duplicate the moved line"

    def test_rotate_month_skips_already_rotated_id_on_replayed_run(self, tmp_path):
        # Simulates the crash window rotate_month's own docstring names:
        # the rotated file already holds the line (a prior run reached
        # that write) but the live shard was never trimmed.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [{**_event("evt-1", "2026-07-15T00:00:00Z"), "sequence": 1}],
        )
        rotated_dir = repo / EVENTS_DIR_RELPATH / "2026-07"
        rotated_dir.mkdir(parents=True)
        _write_shard(repo, "host-a", [{**_event("evt-1", "2026-07-15T00:00:00Z"), "sequence": 1}])
        rotated_path = rotated_dir / "events.host-a.jsonl"
        rotated_path.write_text(
            (repo / EVENTS_DIR_RELPATH / "events.host-a.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        moved = rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert moved == 1

        rotated_ids = [r["id"] for r in _read_raw_lines(rotated_path)]
        assert rotated_ids == ["evt-1"], "replayed rotation must not duplicate an already-rotated id"

        live_ids = [
            r["id"]
            for r in _read_raw_lines(repo / EVENTS_DIR_RELPATH / "events.host-a.jsonl")
        ]
        assert live_ids == []

    def test_rotate_month_no_op_when_month_has_no_events(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [{**_event("evt-1", "2026-06-01T00:00:00Z"), "sequence": 1}],
        )
        moved = rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert moved == 0
        assert not (repo / EVENTS_DIR_RELPATH / "2026-07").exists()

    def test_rotate_month_absent_shard_is_a_no_op(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        (repo / EVENTS_DIR_RELPATH).mkdir(parents=True)
        moved = rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert moved == 0

    def test_shard_path_and_events_dir_relpath_unchanged_by_rotation(self, tmp_path):
        # The ruling's own hard constraint: shard_path/EVENTS_DIR_RELPATH
        # never point at a partitioned layout, even after rotation.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [{**_event("evt-1", "2026-07-15T00:00:00Z"), "sequence": 1}],
        )
        rotate_month(repo_root=repo, month="2026-07", machine="host-a")
        assert shard_path(repo, machine="host-a") == repo / EVENTS_DIR_RELPATH / "events.host-a.jsonl"
        assert EVENTS_DIR_RELPATH == "state/sovereign-tracker"


class TestReadEventsMergesFlatAndPartitionedLayouts:
    def test_rotated_month_still_readable_through_read_events(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [
                {**_event("evt-1", "2026-06-01T00:00:00Z", applied_at="2026-06-01T00:00:00Z"), "sequence": 1},
                {**_event("evt-2", "2026-07-15T00:00:00Z", applied_at="2026-07-15T00:00:00Z"), "sequence": 2},
            ],
        )
        rotate_month(repo_root=repo, month="2026-06", machine="host-a")

        ids = [r["id"] for r in read_events(repo_root=repo)]
        assert ids == ["evt-1", "evt-2"], "rotated-out month must stay readable"

    def test_mixed_flat_and_partitioned_corpus_reads_in_chronological_order(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        # host-a: fully flat, no rotation.
        _write_shard(
            repo,
            "host-a",
            [
                {**_event("a-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:02Z"), "sequence": 1},
            ],
        )
        # host-b: one rotated month plus a live remainder.
        _write_shard(
            repo,
            "host-b",
            [
                {**_event("b-1", "2026-06-01T00:00:00Z", applied_at="2026-01-01T00:00:01Z"), "sequence": 1},
                {**_event("b-2", "2026-07-01T00:00:00Z", applied_at="2026-01-01T00:00:03Z"), "sequence": 2},
            ],
        )
        rotate_month(repo_root=repo, month="2026-06", machine="host-b")

        ids = [r["id"] for r in read_events(repo_root=repo)]
        assert ids == ["b-1", "a-1", "b-2"], "cross-shard applied_at order must survive partitioning"

    def test_read_events_untouched_when_no_rotation_has_happened(self, tmp_path):
        # Regression guard: a fully-flat corpus (the common case today)
        # must read identically to before this chunk.
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-a",
            [{**_event("evt-1", "2026-01-01T00:00:00Z", applied_at="2026-01-01T00:00:00Z"), "sequence": 1}],
        )
        ids = [r["id"] for r in read_events(repo_root=repo)]
        assert ids == ["evt-1"]


class TestFoldObservedSetReadsAcrossRotatedPeerShards:
    def test_fold_observed_set_vector_covers_rotated_and_live_peer_bytes(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-peer",
            [
                {**_event("p-1", "2026-06-01T00:00:00Z"), "sequence": 1},
                {**_event("p-2", "2026-07-01T00:00:00Z"), "sequence": 2},
            ],
        )
        rotate_month(repo_root=repo, month="2026-06", machine="host-peer")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "host-self")
        marker = fold_observed_set(repo_root=repo)
        assert marker is not None
        component = marker["observed_set"]["host-peer"]
        assert component["sequence"] == 2

        # The digest must cover BOTH events (rotated + live), not just the
        # live remainder — recompute it the same way the peer's full
        # ordered history would.
        from coordinator_core.tracker_store import _prefix_digest

        assert component["prefix_digest"] == _prefix_digest(["p-1", "p-2"])

    def test_resolve_observed_set_validates_claim_spanning_rotated_and_live(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-peer",
            [
                {**_event("p-1", "2026-06-01T00:00:00Z"), "sequence": 1},
                {**_event("p-2", "2026-07-01T00:00:00Z"), "sequence": 2},
            ],
        )
        rotate_month(repo_root=repo, month="2026-06", machine="host-peer")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "host-self")
        marker = fold_observed_set(repo_root=repo)
        assert marker is not None

        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved["host-peer"] != OBSERVED_SET_UNKNOWN
        assert resolved["host-peer"]["sequence"] == 2

    def test_resolve_observed_set_detects_tampering_across_rotation(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path / "repo")
        _write_shard(
            repo,
            "host-peer",
            [
                {**_event("p-1", "2026-06-01T00:00:00Z"), "sequence": 1},
                {**_event("p-2", "2026-07-01T00:00:00Z"), "sequence": 2},
            ],
        )
        rotate_month(repo_root=repo, month="2026-06", machine="host-peer")

        monkeypatch.setattr(ts, "machine_slug", lambda *a, **kw: "host-self")
        marker = fold_observed_set(repo_root=repo)
        assert marker is not None

        # Tamper with the ROTATED partition's content after the fold.
        rotated_path = repo / EVENTS_DIR_RELPATH / "2026-06" / "events.host-peer.jsonl"
        _write_shard(repo, "host-peer-tmp", [{**_event("p-1-rewritten", "2026-06-01T00:00:00Z"), "sequence": 1}])
        rotated_path.write_text(
            (repo / EVENTS_DIR_RELPATH / "events.host-peer-tmp.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (repo / EVENTS_DIR_RELPATH / "events.host-peer-tmp.jsonl").unlink()

        resolved = resolve_observed_set(marker, repo_root=repo)
        assert resolved["host-peer"] is OBSERVED_SET_UNKNOWN, (
            "a digest mismatch inside the rotated partition must still resolve unknown"
        )
