"""unproduced_hunks reads a run's invariants (own artifacts, manifest, baseline, kept) once per landing."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import git_dir, git_state
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit import run_artifact_ownership as rao

ME = "ask-20261025T000000-aaaaaa"
N = 50


@pytest.fixture
def counted(tmp_path, monkeypatch):
    calls = {"own": 0, "manifest_reads": 0, "kept": 0}
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {})
    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data)

    real_own, real_read, real_kept = rao.own_artifact_paths, hha._read_json, kept_work.kept_fingerprints

    def own(root, run):
        calls["own"] += 1
        return real_own(root, run)

    def read(path):
        if path.name == "manifest.json":
            calls["manifest_reads"] += 1
        return real_read(path)

    def kept(record_dir):
        calls["kept"] += 1
        return real_kept(record_dir)

    monkeypatch.setattr(rao, "own_artifact_paths", own)
    monkeypatch.setattr(hha, "_read_json", read)
    monkeypatch.setattr(kept_work, "kept_fingerprints", kept)
    return calls


def _manifest(root, *, writes=(), baseline=None):
    rdir = root / "state" / "warp" / ME
    rdir.mkdir(parents=True, exist_ok=True)
    rows = [{"writes": list(writes)}] if writes else []
    (rdir / "manifest.json").write_text(json.dumps({"rows": rows, "write_baseline": baseline or {}}), encoding="utf-8")


def _own_artifacts(root, n):
    rels = [f"state/warp/{ME}/note-{i}.md" for i in range(n)]
    for rel in rels:
        (root / rel).write_text("x\n", encoding="utf-8")
    return rels


def test_fifty_run_own_artifacts_read_the_run_invariants_once(tmp_path, counted):
    _manifest(tmp_path)
    paths = _own_artifacts(tmp_path, N)
    assert hp.unproduced_hunks(tmp_path, ME, paths, own_plan=None) == []
    assert counted == {"own": 1, "manifest_reads": 1, "kept": 1}


def test_fifty_declared_paths_read_the_manifest_once(tmp_path, counted):
    paths = [f"src/mod_{i}.py" for i in range(N)]
    _manifest(tmp_path, writes=paths, baseline={"unrelated.py": None})
    (tmp_path / "src").mkdir()
    for rel in paths:
        (tmp_path / rel).write_text("x\n", encoding="utf-8")
    advisories: list = []
    assert hp.unproduced_hunks(tmp_path, ME, paths, own_plan=None, advisories=advisories) == []
    assert len(advisories) == N  # declared, unbaselined: attributable to the run, not a finding
    assert counted["manifest_reads"] == 1 and counted["own"] == 1 and counted["kept"] == 1


def test_one_invariants_value_serves_every_call_of_a_landing(tmp_path, counted):
    _manifest(tmp_path)
    paths = _own_artifacts(tmp_path, N)
    inv = hp.run_invariants(tmp_path, ME)
    for chunk in (paths[:10], paths[10:30], paths[30:]):
        assert hp.unproduced_hunks(tmp_path, ME, chunk, own_plan=None, invariants=inv) == []
    assert counted == {"own": 1, "manifest_reads": 1, "kept": 1}


def test_an_invariants_scope_serves_calls_that_pass_nothing(tmp_path, counted):
    _manifest(tmp_path)
    paths = _own_artifacts(tmp_path, N)
    with hp.invariants_scope(tmp_path, ME):
        hp.unproduced_hunks(tmp_path, ME, paths[:25], own_plan=None)
        hp.unproduced_hunks(tmp_path, ME, paths[25:], own_plan=None)
    assert counted["own"] == 1 and counted["manifest_reads"] == 1
    hp.unproduced_hunks(tmp_path, ME, paths[:1], own_plan=None)
    assert counted["own"] == 2  # nothing outlives the scope


def test_a_path_the_run_neither_baselined_declared_nor_owns_still_refuses(tmp_path, counted):
    _manifest(tmp_path)
    paths = _own_artifacts(tmp_path, 3)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "peer.py").write_text("peer\n", encoding="utf-8")
    inv = hp.run_invariants(tmp_path, ME)
    found = hp.unproduced_hunks(tmp_path, ME, [*paths, "src/peer.py"], own_plan=None, invariants=inv)
    assert [f.path for f in found] == ["src/peer.py"]
    assert found[0].reason == hp.FOREIGN_HUNK_UNPRODUCED
