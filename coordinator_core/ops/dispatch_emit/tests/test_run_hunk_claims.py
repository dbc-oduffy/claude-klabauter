"""run_hunk_claims.own_hunks: disjoint, overlapping, adjacent, unattributed, plus the zero-spawn pin.

A claimant's baseline is the file it staged against; its delta is worktree minus that baseline."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import git_dir, git_objects
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

PATH = "src/target.py"
ME = "ask-20261025T000000-aaaaaa"
SENIOR = "ask-20261024T000000-bbbbbb"
JUNIOR = "ask-20261026T000000-cccccc"


def _text(edits: dict[int, str]) -> str:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11))


@pytest.fixture
def world(tmp_path, monkeypatch):
    blobs: dict[str, bytes] = {}
    baselines: dict[str, dict] = {}

    def no_spawn(*a, **k):
        raise AssertionError("own_hunks must not spawn")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(hha, "_baseline", lambda root, run: baselines.get(run))

    class World:
        root = tmp_path

        @staticmethod
        def tree(edits):
            (tmp_path / "src").mkdir(exist_ok=True)
            (tmp_path / PATH).write_bytes(_text(edits).encode())

        @staticmethod
        def baseline(run, edits):
            sha = f"{len(blobs):040x}"
            blobs[sha] = _text(edits).encode()
            baselines[run] = {PATH: sha}

    return World


def _h(line, old_len=1, new_len=1):
    return rhc.Hunk(PATH, line, old_len, line, new_len)


def test_disjoint_hunks_each_run_owns_its_own(world):
    world.tree({2: "A", 9: "B"})
    world.baseline(ME, {2: "A"})  # my delta: line 9
    world.baseline(SENIOR, {9: "B"})  # holder delta: line 2
    split = rhc.own_hunks(world.root, ME, PATH, [_h(2), _h(9)], {SENIOR: True})
    assert split.own == [_h(9)]
    assert split.overlapping == {}
    assert split.unattributed == []


def test_overlapping_senior_hunk_holds_and_names_the_holder(world):
    world.tree({3: "m3", 4: "s4"})
    world.baseline(ME, {4: "s4"})  # my delta: line 3
    world.baseline(SENIOR, {3: "m3"})  # holder delta: line 4
    mine, theirs = rhc.Hunk(PATH, 3, 2, 3, 1), _h(4)  # my hunk replaces HEAD lines 3-4
    split = rhc.own_hunks(world.root, ME, PATH, [mine, theirs], {SENIOR: True})
    assert split.own == [mine]
    assert split.overlapping == {SENIOR: [theirs]}


def test_junior_overlap_is_excluded_and_never_holds(world):
    world.tree({3: "m3", 4: "j4"})
    world.baseline(ME, {4: "j4"})
    world.baseline(JUNIOR, {3: "m3"})
    mine, theirs = rhc.Hunk(PATH, 3, 2, 3, 1), _h(4)
    split = rhc.own_hunks(world.root, ME, PATH, [mine, theirs], {JUNIOR: False})
    assert split.own == [mine]
    assert split.overlapping == {} and split.unattributed == []


def test_adjacent_hunks_do_not_overlap(world):
    world.tree({3: "m3", 4: "s4"})
    world.baseline(ME, {4: "s4"})
    world.baseline(SENIOR, {3: "m3"})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(3), _h(4)], {SENIOR: True})
    assert split.own == [_h(3)]
    assert split.overlapping == {}


def test_equal_baselines_leave_every_hunk_unattributed(world):
    world.tree({2: "A", 9: "B"})
    world.baseline(ME, {})
    world.baseline(SENIOR, {})
    hunks = [_h(2), _h(9)]
    split = rhc.own_hunks(world.root, ME, PATH, hunks, {SENIOR: True})
    assert split.unattributed == hunks
    assert split.own == [] and split.overlapping == {}


def test_hunk_no_delta_explains_is_unattributed(world):
    world.tree({2: "A"})
    world.baseline(ME, {2: "A"})
    world.baseline(SENIOR, {2: "A"})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(2)], {SENIOR: True})
    assert split.unattributed == [_h(2)] and split.own == []


def test_holder_without_baseline_attributes_nothing(world):
    world.tree({2: "A", 9: "B"})
    world.baseline(ME, {})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(2), _h(9)], {SENIOR: True})
    assert split.own == [_h(2), _h(9)]  # only my delta explains them


def test_landing_run_without_baseline_owns_nothing(world):
    world.tree({2: "A"})
    world.baseline(SENIOR, {})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(2)], {SENIOR: True})
    assert split.own == []
    assert split.overlapping == {}  # unattributed to me, nothing of mine to clash with


def test_error_reads_as_unattributed(world, monkeypatch):
    def boom(root, run):
        raise RuntimeError("boom")

    monkeypatch.setattr(hha, "_baseline", boom)
    split = rhc.own_hunks(world.root, ME, PATH, [_h(2)], {SENIOR: True})
    assert split.unattributed == [_h(2)] and split.own == []


def _spread_pair(lines=5200):
    """A land_run.py-sized, blank-rich file edited at its first and last line: the trimmed middle is the whole file."""
    old = [b"\n" if i % 7 == 0 else f"    v{i % 3}\n".encode() for i in range(lines)]
    new = list(old)
    new[0], new[-1] = b"head\n", b"tail\n"
    return old, new


def test_edits_at_both_ends_of_a_large_file_are_one_changed_span_under_the_bar():
    import time

    old, new = _spread_pair()
    start = time.process_time()
    spans = [rhc._delta_spans(old, new) for _claimant in (ME, SENIOR)]  # two claimants hold the path
    spent = time.process_time() - start
    assert spent < 0.002, spent  # measured ~0.05ms; DR-344 bar is 500ms; process time, never wall clock
    assert spans[0] == spans[1] == [(2, 2 * len(new))]
    lo, hi = spans[0][0]
    assert lo <= 2 * 1 and hi >= 2 * len(new) - 1  # both edits fall inside it: any two claimants conflict


def test_a_large_middle_is_one_replace_and_a_small_middle_is_still_matched():
    old, new = _spread_pair()
    assert rhc.changed_opcodes(old, new) == [("replace", 0, len(old), 0, len(new))]
    small_old, small_new = _spread_pair(40)
    assert [op[0] for op in rhc.changed_opcodes(small_old, small_new)] == ["replace", "replace"]


def _worst_pair(middle):
    """Identical repeated lines (the quadratic case), edits at both ends, trimmed middle of `middle` lines
    on each side, with three lines replaced inside it."""
    old = [b"head\n", *([b"    x\n"] * (middle - 2)), b"tail\n"]
    new = list(old)
    new[0], new[-1] = b"HEAD\n", b"TAIL\n"
    new[middle // 3:middle // 3 + 3] = [b"ins\n"] * 3
    return old, new


def test_the_match_bound_matches_at_the_bound_and_stops_just_over_it_under_the_bar():
    import time

    at_old, at_new = _worst_pair(rhc.MAX_MATCHED_MIDDLE)
    assert not rhc.exceeds_match_bound(at_old, at_new)
    start = time.process_time()
    ops = [rhc.changed_opcodes(at_old, at_new) for _claimant in (ME, SENIOR)]  # two claimants
    spent = time.process_time() - start
    assert spent < 0.02, spent  # measured ~5ms; DR-344 bar is 500ms; process time, never wall clock
    assert len(ops[0]) > 1  # matched, not the coarse span

    over_old, over_new = _worst_pair(rhc.MAX_MATCHED_MIDDLE + 1)
    assert rhc.exceeds_match_bound(over_old, over_new)
    assert rhc.changed_opcodes(over_old, over_new) == [("replace", 0, len(over_old), 0, len(over_new))]


def _numbered(n, edits=()):
    return "".join(f"{'EDIT' if i in edits else 'line'}{i}\n" for i in range(1, n + 1))


def _pre_baseline_world(world, monkeypatch, n):
    """HEAD has no edits; my baseline already holds line n//2 edited (a peer's or earlier hunk); I then
    edited the first and last lines. Hunks in HEAD coordinates: first, middle (pre-baseline), last."""
    mid = n // 2
    blobs = {"b" * 40: _numbered(n, {mid}).encode()}
    from coordinator_core.git import git_dir, git_objects

    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: world.root / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(hha, "_baseline", lambda root, run: {PATH: "b" * 40} if run == ME else None)
    (world.root / "src").mkdir(exist_ok=True)
    (world.root / PATH).write_bytes(_numbered(n, {1, mid, n}).encode())
    return [_h(1), _h(mid), _h(n)], mid


def test_over_bound_single_claimant_keeps_hunks_it_cannot_attribute_held(world, monkeypatch):
    n = rhc.MAX_MATCHED_MIDDLE + 50
    hunks, _mid = _pre_baseline_world(world, monkeypatch, n)
    split = rhc.own_hunks(world.root, ME, PATH, hunks, {})
    assert split.own == []
    assert split.unattributed == hunks


def test_over_bound_senior_holds_the_interior_hunk_the_lander_also_edited(world, monkeypatch):
    n = rhc.MAX_MATCHED_MIDDLE + 50
    mid = n // 2
    blobs = {"1" * 40: _numbered(n, {mid}).encode(), "2" * 40: _numbered(n, {1, n}).encode()}
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(hha, "_baseline", lambda root, run: {PATH: "1" * 40 if run == SENIOR else "2" * 40})
    (world.root / "src").mkdir(exist_ok=True)
    (world.root / PATH).write_bytes(_numbered(n, {1, mid, n}).encode())
    split = rhc.own_hunks(world.root, ME, PATH, [_h(mid)], {SENIOR: True})
    assert split.own == []  # two owners: held, never landed
    assert split.unattributed == [_h(mid)]


def test_under_bound_single_claimant_owns_its_edits_but_not_the_pre_baseline_hunk(world, monkeypatch):
    n = 100
    hunks, mid = _pre_baseline_world(world, monkeypatch, n)
    split = rhc.own_hunks(world.root, ME, PATH, hunks, {})
    assert split.own == [_h(1), _h(n)]
    assert split.unattributed == [_h(mid)]
