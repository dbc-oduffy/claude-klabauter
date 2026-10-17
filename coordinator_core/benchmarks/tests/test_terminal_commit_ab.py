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


def test_spawn_verb_extraction():
    assert ab.spawn_verb(["git", "-C", "x", "--no-optional-locks", "status", "--porcelain"]) == ("git", "status")
    assert ab.spawn_verb(["git", "hash-object", "-w", "--stdin-paths"]) == ("git", "hash-object")
    assert ab.spawn_verb(["/usr/bin/python3", "/p/close_job_noop.py", "run"]) == ("python3", "close_job_noop.py")
    assert ab.spawn_verb(["py\\python.exe", "-S", "-E", "p\\close_job_noop.py"]) == ("python", "close_job_noop.py")


def test_spawns_flag_parses_without_baseline():
    args = ab.parse_args(["--spawns"])
    assert args.spawns and args.baseline is None
    assert ab.parse_args(["--spawns", "--at", "f" * 40]).at == "f" * 40


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_spawns_probe_lists_the_close_job_with_its_site(tmp_path):
    result = ab.run_probe(_REPO, tmp_path)
    assert result["spawn_total"] == len(result["spawns"]) >= 1
    assert all(set(s) == {"exe", "verb", "site", "phase"} and s["phase"] == "call" for s in result["spawns"])
    assert any(
        s["site"] in {
            "coordinator_core.ops.ceremony.detached_spawn:spawn_detached",
            "coordinator_core.ops.dispatch_emit.terminal_commit:_launch_close_job",
        }
        for s in result["spawns"]
    )


_CANNED_IMPORTTIME = """\
import time: self [us] | cumulative | imported package
import time:       100 |        100 |   encodings
import time:        50 |         50 |     leaf_a
import time:        30 |         30 |     leaf_b
import time:        10 |         90 |   pkg_one
import time:       200 |        200 |   pkg_two
some unrelated stderr line
"""


def test_parse_importtime_returns_outermost_with_cumulative_and_module_counts():
    rows = ab.parse_importtime(_CANNED_IMPORTTIME)
    assert [r["name"] for r in rows] == ["encodings", "pkg_one", "pkg_two"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["pkg_one"]["cum_us"] == 90 and by_name["pkg_one"]["modules"] == 3
    assert by_name["pkg_two"]["modules"] == 1


def test_net_attribution_nets_setup_and_ranks():
    full = ab.parse_importtime(_CANNED_IMPORTTIME)
    setup = [{"name": "encodings", "cum_us": 100, "modules": 1}, {"name": "pkg_two", "cum_us": 50, "modules": 1}]
    rows = ab.net_attribution(full, setup)
    assert [r["name"] for r in rows] == ["pkg_two", "pkg_one"]
    assert rows[0]["net_ms"] == 0.15


def test_probe_flags_parse():
    args = ab.parse_args(["--importers", "a.b", "c.d", "--attribute", "--top", "7", "--at", "f" * 40])
    assert args.importers == ["a.b", "c.d"] and args.attribute and args.top == 7 and args.at == "f" * 40
    with pytest.raises(SystemExit):
        ab.parse_args(["--at", "f" * 40])


def test_main_refuses_repo_out_dir_for_at(monkeypatch):
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    with pytest.raises(ValueError, match="inside the repo root"):
        ab.main(["--at", "0" * 40, "--importers", "asyncio", "--out-dir", str(_REPO / "scratch" / "ab-refused")])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_importers_reports_a_requester_for_a_module_the_path_loads(tmp_path):
    target = "coordinator_core.ops.ceremony.commit_v2"
    result = ab.run_probe(_REPO, tmp_path, targets=[target])
    hits = result["importers"][target]
    assert hits and all(h["requester"].count(":") == 1 and h["requests"] >= 1 for h in hits)
    assert result["asyncio_importers"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_attribute_ranks_outermost_imports_with_triggers(tmp_path):
    rows = ab.run_attribute(_REPO, tmp_path, 5)
    assert 0 < len(rows) <= 5
    assert [r["net_ms"] for r in rows] == sorted((r["net_ms"] for r in rows), reverse=True)
    assert all("trigger" in r and r["modules"] >= 1 for r in rows)
