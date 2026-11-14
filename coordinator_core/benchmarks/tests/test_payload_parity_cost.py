"""Tests for the payload parity cold/warm cost harness (`benchmarks/payload_parity_cost.py`)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import payload_parity_cost as ppc
from coordinator_core.win_portability import no_console_creationflags

_CALLEE = "def carry(items):\n    return items\n"
_CALLEE_OLD = "def carry(items, *, equivalence_map=None):\n    return items\n"
_CALLER = (
    "from coordinator_core.ops.deliverable_carry import carry\n\n"
    "def run(rows, mapping):\n    return carry(rows, equivalence_map=mapping)\n"
)
_CALLEE_REL = "coordinator_core/ops/deliverable_carry.py"
_CALLER_REL = "coordinator_core/baton_assemble/__init__.py"


def _write_tree(root: Path, callee: str) -> None:
    for rel, text in ((_CALLEE_REL, callee), (_CALLER_REL, _CALLER), ("coordinator_core/__init__.py", ""),
                      ("coordinator_core/ops/__init__.py", "")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    return proc.stdout.strip()


@pytest.fixture()
def two_commit_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write_tree(repo, _CALLEE_OLD)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    first = _git(repo, "rev-parse", "HEAD")
    _write_tree(repo, _CALLEE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    return repo, first


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        ppc.main(["--help"])
    assert exc.value.code == 0
    assert "--dest-ref" in capsys.readouterr().out


def test_measure_pair_reports_cold_warm_and_the_violation(tmp_path):
    dest, staging = tmp_path / "dest", tmp_path / "staging"
    _write_tree(dest, _CALLEE_OLD)
    _write_tree(staging, _CALLEE)
    memo = tmp_path / "memo"
    memo.mkdir()
    (memo / "stale").write_text("x", encoding="utf-8")

    result = ppc.measure_pair(dest, staging, [_CALLEE_REL, _CALLER_REL], memo_dir=memo)

    assert not (memo / "stale").exists(), "cold run must start from an emptied memo dir"
    assert result["cold_cpu_s"] >= 0 and result["warm_cpu_s"] >= 0
    assert result["violations_match"] is True
    assert [(v["call_file"], v["lineno"]) for v in result["violations"]] == [(_CALLER_REL, 4)]
    assert "equivalence_map" in result["violations"][0]["message"]
    assert {r["module"] for r in result["per_file"]} == {"coordinator_core.baton_assemble", "coordinator_core.ops.deliverable_carry"}
    assert result["checked_files"]["cold"] == 2


def test_measure_pair_per_file_follows_the_runner_order_and_top_limit(tmp_path):
    def runner(dest, staging, changed, *, rel_root=""):
        return {"cpu_s": 3.0, "setup_cpu_s": 0.5, "per_file": [("a", 1.0), ("b", 2.0), ("c", 0.5)],
                "violations": [], "checked_calls": 0, "abstained_calls": 0}

    result = ppc.measure_pair(tmp_path, tmp_path, [], runner=runner, top=2)
    assert [r["module"] for r in result["per_file"]] == ["b", "a"]
    assert result["cold_cpu_s"] == 3.0


def test_per_file_probe_restores_the_gate_and_attributes_each_checked_file(tmp_path, monkeypatch):
    from coordinator_core.percolate import payload_parity as pp

    monkeypatch.setattr(pp, "_memo_dir", lambda: tmp_path / "memo")
    dest, staging = tmp_path / "dest", tmp_path / "staging"
    _write_tree(dest, _CALLEE_OLD)
    _write_tree(staging, _CALLEE)
    before = (pp._parse_module, pp._parse_staged, pp._FileImportMap)
    run = ppc.run_once(dest, staging, [_CALLEE_REL, _CALLER_REL])
    assert (pp._parse_module, pp._parse_staged, pp._FileImportMap) == before
    spans = dict(run["per_file"])
    assert sum(spans.values()) + run["setup_cpu_s"] == pytest.approx(run["cpu_s"], abs=1e-3)
    assert [m for m, _ in run["per_file"]] == ["coordinator_core.baton_assemble", "coordinator_core.ops.deliverable_carry"]
    assert all(cpu >= 0 for _m, cpu in run["per_file"])
    assert run["setup_cpu_s"] >= 0


def test_changed_files_and_materialise_over_a_real_git_pair(two_commit_repo, tmp_path):
    repo, first = two_commit_repo
    assert ppc.changed_py_files(repo, first, "HEAD") == [_CALLEE_REL]
    out = tmp_path / "out"
    count = ppc.materialise(repo, "HEAD", out)
    assert count == 4
    assert (out / _CALLEE_REL).read_text(encoding="utf-8") == _CALLEE
    assert not (out / "README.md").exists()


def test_bench_end_to_end_over_a_real_git_pair_finds_the_violation(two_commit_repo):
    repo, first = two_commit_repo
    result = ppc.bench(repo, first, "HEAD")
    assert result["changed_py_files"] == 1
    assert result["dest_py_files"] == result["staging_py_files"] == 4
    assert result["token_index_present"] is False
    assert result["violations_match"] is True
    assert [v["lineno"] for v in result["violations"]] == [4]
    assert not (ppc.bench_root_for(repo)).exists(), "materialised trees are removed after the run"
    json.dumps(result)


def test_unresolvable_ref_is_a_typed_refusal(two_commit_repo, tmp_path):
    repo, _ = two_commit_repo
    with pytest.raises(ppc.BenchRefusal, match="does not resolve"):
        ppc.materialise(repo, "no-such-ref", tmp_path / "x")


def _provenance(tmp_path, repo: Path, rows: dict) -> Path:
    path = tmp_path / "publish-provenance.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def test_default_dest_ref_takes_the_newest_published_sha_for_this_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    prov = _provenance(tmp_path, repo, {
        "a": {"published": True, "published_at": "2026-11-14T06:00:00+00:00", "toplevels": {str(repo): "new"}},
        "b": {"published": False, "last_published": {"at": "2026-11-13T00:00:00+00:00", "toplevels": {str(repo): "old"}}},
        "c": {"published": True, "published_at": "2026-11-15T00:00:00+00:00", "toplevels": {str(tmp_path / "other"): "elsewhere"}},
    })
    assert ppc.default_dest_ref(repo, prov) == "new"


def test_default_dest_ref_refuses_naming_the_key_when_nothing_matches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    prov = _provenance(tmp_path, repo, {"a": {"published": True, "published_at": "x", "toplevels": {str(tmp_path / "o"): "s"}}})
    with pytest.raises(ppc.BenchRefusal, match="publish-provenance.json"):
        ppc.default_dest_ref(repo, prov)
    with pytest.raises(ppc.BenchRefusal, match="publish-provenance.json"):
        ppc.default_dest_ref(repo, tmp_path / "absent.json")
