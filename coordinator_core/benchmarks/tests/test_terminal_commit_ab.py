"""Tests for the terminal-commit A/B driver: argument parsing, interleave coverage, out-dir
refusal, baseline extraction and the `--count-replaces` / `--asyncio-importers` probe.

Negative-spec:
    - Takes no timing measurement; `run_ab` is exercised with fake sides.
    - Spawning cases carry their own `spawns_process` + `cadence` marks; the rest stay fast.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_ab as ab
from coordinator_core.git.git_state import head_sha

_REPO = Path(__file__).resolve().parents[3]


def test_rounds_default_and_parse():
    assert ab.parse_args(["--baseline", "abc"]).rounds == 3
    assert ab.parse_args(["--baseline", "abc", "--rounds", "5"]).rounds == 5


@pytest.mark.parametrize("bad", ["0", "-1", "x"])
def test_rounds_rejects_non_positive(bad):
    with pytest.raises(SystemExit):
        ab.parse_args(["--baseline", "abc", "--rounds", bad])


def test_requires_baseline_or_a_probe_flag():
    with pytest.raises(SystemExit):
        ab.parse_args([])
    assert ab.parse_args(["--count-replaces"]).baseline is None


def test_every_round_draws_both_sides_once(monkeypatch):
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    sides = [
        ab.Side(name, name, None, lambda n=i: {"net_ms": 100.0 + n, "net_procs": 4.0})
        for i, name in enumerate("AB")
    ]
    report = ab.run_ab(sides, rounds=6, rng=random.Random(7))
    order = report["order"]
    assert len(order) == 12
    for at in range(0, 12, 2):
        assert sorted(order[at : at + 2]) == ["A", "B"]
    assert report["sides"]["A"]["nets_ms"] == [100.0] * 6
    assert report["sides"]["B"]["min_ms"] == 101.0
    assert report["sides"]["B"]["median_ms"] == 101.0


def test_out_dir_inside_repo_is_refused(tmp_path):
    with pytest.raises(ValueError, match="inside the repo root"):
        ab.check_out_dir(_REPO / "scratch" / "x")
    with pytest.raises(ValueError):
        ab.check_out_dir(_REPO)
    assert ab.check_out_dir(tmp_path / "ok") == (tmp_path / "ok").resolve()


def test_main_refuses_repo_out_dir(monkeypatch):
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    with pytest.raises(ValueError, match="inside the repo root"):
        ab.main(["--baseline", "0" * 40, "--out-dir", str(_REPO / "scratch" / "ab-refused")])


def test_baseline_extraction_lands_under_the_given_dir(tmp_path):
    sha = head_sha(_REPO)
    assert sha
    dest = ab.materialise_baseline(_REPO, sha, tmp_path)
    full_path = dest / "coordinator_core" / "benchmarks" / "terminal_commit_full_path.py"
    assert full_path.is_file() and full_path.stat().st_size > 0
    assert tmp_path in full_path.parents


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_git_archive_fallback_matches_object_walk(tmp_path):
    sha = head_sha(_REPO)
    walked = tmp_path / "walk"
    archived = tmp_path / "archive"
    walked.mkdir()
    archived.mkdir()
    ab._extract_via_git_objects(_REPO, sha, walked)
    ab._extract_via_git_archive(_REPO, sha, archived)
    rel = "coordinator_core/benchmarks/terminal_commit_full_path.py"
    # git archive applies .gitattributes eol conversion; the object walk is the raw blob.
    assert (walked / rel).read_bytes() == (archived / rel).read_bytes().replace(b"\r\n", b"\n")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_probe_counts_replaces_and_lists_asyncio_importers(tmp_path):
    result = ab.run_probe(_REPO, tmp_path)
    assert isinstance(result["replace_total"], int) and result["replace_total"] > 0
    assert sum(result["replace_sites"].values()) == result["replace_total"]
    assert isinstance(result["asyncio_importers"], list)
    for hit in result["asyncio_importers"]:
        assert hit["phase"] in {"setup", "call"} and hit["requests"] >= 1
