"""tree_census: bucketing, precedence, unbucketed count, one git spawn, shape validity."""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.architecture_survey import census_shape, tree_census


def _repo(tmp_path: Path, paths: list[str]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel in paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _by_id(payload: dict) -> dict[str, list[str]]:
    return {b["bucket_id"]: [f["path"] for f in b["files"]] for b in payload["censusShapedResults"]}


def test_bucketing_nested_precedence_and_unbucketed(tmp_path):
    repo = _repo(tmp_path, ["a/x.py", "a/sub/y.py", "b/z.py", "README.md", "ab/w.py"])
    buckets = [
        {"bucketId": "A", "dirs": ["a"]},
        {"bucketId": "Sub", "dirs": ["a/sub/", "other"]},
        {"bucketId": "Empty", "dirs": ["nothing"]},
        {"bucketId": "B", "dirs": ["b"]},
    ]
    payload = tree_census.build(repo, buckets)
    got = _by_id(payload)
    assert got["A"] == ["a/x.py"]
    assert got["Sub"] == ["a/sub/y.py"]
    assert got["B"] == ["b/z.py"]
    assert got["Empty"] == []
    assert payload["unbucketedCount"] == 2
    assert payload["source"] == "tree"
    assert payload["chunkTablePath"] is None
    assert payload["oversizedSignalAvailable"] is False
    assert payload["oversizedCount"] == 0
    assert all(f["loc"] is None and f["oversized"] is False
               for b in payload["censusShapedResults"] for f in b["files"])
    assert census_shape.validate(payload) == []


def test_multiple_dirs_longest_across_buckets_wins(tmp_path):
    repo = _repo(tmp_path, ["a/x.py", "a/deep/y.py"])
    buckets = [
        {"bucketId": "Wide", "dirs": ["a", "zzz"]},
        {"bucketId": "Deep", "dirs": ["a/deep"]},
    ]
    got = _by_id(tree_census.build(repo, buckets))
    assert got["Wide"] == ["a/x.py"]
    assert got["Deep"] == ["a/deep/y.py"]


def test_root_prefix_catches_rest_and_tie_goes_to_first(tmp_path):
    repo = _repo(tmp_path, ["a/x.py", "top.md"])
    buckets = [
        {"bucketId": "First", "dirs": ["a"]},
        {"bucketId": "Second", "dirs": ["a"]},
        {"bucketId": "Root", "dirs": ["."]},
    ]
    got = _by_id(tree_census.build(repo, buckets))
    assert got["First"] == ["a/x.py"]
    assert got["Second"] == []
    assert got["Root"] == ["top.md"]


def test_over_tier_bucket_splits_into_suffixed_sub_buckets_under_tier(tmp_path):
    repo = _repo(tmp_path, ["big/a.txt", "big/b.txt", "big/c.txt", "small/s.txt"])
    for name in ("a", "b", "c"):
        (repo / "big" / f"{name}.txt").write_bytes(b"x" * 200_000)  # 50k tokens each
    buckets = [{"bucketId": "Big", "dirs": ["big"]}, {"bucketId": "Small", "dirs": ["small"]}]
    payload = tree_census.build(repo, buckets)
    by_id = {b["bucket_id"]: b for b in payload["censusShapedResults"]}
    assert set(by_id) == {"Big-s1", "Big-s2", "Small"}
    assert [f["path"] for f in by_id["Big-s1"]["files"]] == ["big/a.txt", "big/b.txt"]
    assert [f["path"] for f in by_id["Big-s2"]["files"]] == ["big/c.txt"]
    assert all(by_id[k]["owner"] == "Big" for k in ("Big-s1", "Big-s2"))
    assert all(b["projected_tokens"] <= 100_000 for b in by_id.values())
    assert "owner" not in by_id["Small"]
    assert census_shape.validate(payload) == []


def test_bucket_exactly_at_tier_is_not_split(tmp_path):
    repo = _repo(tmp_path, ["a/x.txt"])
    (repo / "a" / "x.txt").write_bytes(b"x" * 400_000)
    got = _by_id(tree_census.build(repo, [{"bucketId": "A", "dirs": ["a"]}]))
    assert list(got) == ["A"]


def test_split_buckets_feed_the_estimate_under_tier(tmp_path):
    from coordinator_core.architecture_survey import refresh_cost

    repo = _repo(tmp_path, ["big/a.txt", "big/b.txt"])
    for name in ("a", "b"):
        (repo / "big" / f"{name}.txt").write_bytes(b"x" * 300_000)
    payload = tree_census.build(repo, [{"bucketId": "Big", "dirs": ["big"]}])
    projected = {b["bucket_id"]: b["projected_tokens"] for b in payload["censusShapedResults"]}
    got = refresh_cost.estimate(2, len(projected), census_skipped=True, projected_tokens_by_system=projected)
    assert got["phases"]["analysis"]["per_agent_tokens"] <= 100_000
    assert got["phases"]["analysis"]["agents"] == 2


def _count_git_spawns(monkeypatch) -> list:
    calls: list = []
    real = tree_census.run_git

    def spy(cmd, *a, **kw):
        calls.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(tree_census, "run_git", spy)
    return calls


def test_exactly_one_git_spawn_for_one_and_many_files(tmp_path, monkeypatch):
    small = _repo(tmp_path / "small", ["a/one.py"])
    big = _repo(tmp_path / "big", [f"a/f{i}.py" for i in range(500)])
    buckets = [{"bucketId": "A", "dirs": ["a"]}]
    calls = _count_git_spawns(monkeypatch)
    assert len(_by_id(tree_census.build(small, buckets))["A"]) == 1
    assert len(calls) == 1
    calls.clear()
    assert len(_by_id(tree_census.build(big, buckets))["A"]) == 500
    assert len(calls) == 1
    assert calls[0][-2:] == ["ls-files", "-z"]
