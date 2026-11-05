"""Unit tests for the pure residue_adoption decisions."""

from __future__ import annotations

from coordinator_core.ops.session.residue_adoption import (
    adoption_groups,
    candidate_ancestor_dirs,
    classify_unclaimed,
    is_sole_live_em,
)


def test_sole_only_when_live_is_exactly_caller():
    assert is_sole_live_em("a", {"a"}) is True
    assert is_sole_live_em("a", frozenset(["a"])) is True


def test_not_sole_when_live_empty_none_or_shared():
    assert is_sole_live_em("a", None) is False
    assert is_sole_live_em("a", set()) is False
    assert is_sole_live_em("a", {"a", "b"}) is False
    assert is_sole_live_em("a", {"b"}) is False
    assert is_sole_live_em("", {""}) is False


def test_ancestor_dirs_floor_at_two_segments():
    assert candidate_ancestor_dirs("state/warp/run/x.md") == [
        "state/warp/run",
        "state/warp",
    ]
    assert candidate_ancestor_dirs("state/x.md") == []
    assert candidate_ancestor_dirs("x.md") == []


def test_sibling_in_same_dir_is_adoptable():
    res = classify_unclaimed(["state/warp/old/new.md"], ["state/warp/old/a.md"])
    assert dict(res["adoptable"]) == {"state/warp/old": ["state/warp/old/new.md"]}
    assert res["gitignore_candidates"] == []


def test_new_run_dir_is_adoptable_not_candidate():
    unclaimed = ["state/warp/new-run/x.md", "state/warp/new-run/y.md"]
    res = classify_unclaimed(unclaimed, ["state/warp/old-run/a.md"])
    assert dict(res["adoptable"]) == {"state/warp": unclaimed}
    assert res["gitignore_candidates"] == []


def test_new_queue_grind_evidence_dir_is_adoptable():
    res = classify_unclaimed(
        ["state/queue-grind/new-id/evidence/e.md"],
        ["state/queue-grind/old-id/evidence/e.md"],
    )
    assert list(res["adoptable"]) == ["state/queue-grind"]
    assert res["gitignore_candidates"] == []


def test_class_without_tracked_file_is_candidate_and_not_adoptable():
    res = classify_unclaimed(
        ["scratch/foo/a.txt", "scratch/foo/b.txt"], ["state/warp/old/a.md"]
    )
    assert dict(res["adoptable"]) == {}
    assert res["gitignore_candidates"] == ["scratch/foo"]


def test_two_segment_floor_blocks_top_level_tracked_file():
    res = classify_unclaimed(["state/new/x.md"], ["state/tracked.md"])
    assert dict(res["adoptable"]) == {}
    assert res["gitignore_candidates"] == ["state/new"]


def test_root_file_is_candidate_labelled_repo_root():
    res = classify_unclaimed(["stray.txt"], ["README.md", "a/b/c.md"])
    assert res["gitignore_candidates"] == ["(repo root)"]


def test_mixed_and_dedup():
    res = classify_unclaimed(
        ["a/b/new.txt", "a/b/new.txt", "z/y/new.txt", "z/y/other.txt"],
        ["a/b/old.txt"],
    )
    assert dict(res["adoptable"]) == {"a/b": ["a/b/new.txt"]}
    assert res["gitignore_candidates"] == ["z/y"]


def test_adoption_groups_one_per_class():
    adoptable = {"a/b": ["a/b/1.md", "a/b/2.md"], "c/d": ["c/d/3.md"]}
    groups = adoption_groups(adoptable, "abcdef123")
    assert [g["paths"] for g in groups] == [["a/b/1.md", "a/b/2.md"], ["c/d/3.md"]]
    assert groups[0]["message"].startswith("auto-commit: 2 file(s) (session abcdef, a/b)")
    assert "  - a/b/1.md" in groups[0]["message"]
    assert set(groups[0]) == {"paths", "message"}


def test_adoption_groups_empty():
    assert adoption_groups({}) == []
    assert adoption_groups({"a/b": []}) == []
