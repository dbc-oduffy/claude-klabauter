"""The plan-blitz land step commits exactly the trail files it names, in one commit_v2 call."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.roadmap import blitz_land as bl

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("fire-*.mjs\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _trail(root: Path) -> dict:
    run = root / "state" / "plan-blitz" / "20261015T000000Z"
    slot = run / "wave-0-abc"
    slot.mkdir(parents=True)
    (slot / "b-1.planning-report.md").write_text("---\nplan: x\n---\n", encoding="utf-8")
    (slot / "b-1.sizing.yaml").write_text("a: 1\n", encoding="utf-8")
    (run / "wave-0.gate-report.json").write_text("{}\n", encoding="utf-8")
    (run / "fire-0-1.mjs").write_text("// ignored\n", encoding="utf-8")
    return {"waveIndex": 0, "trailSlotDir": str(slot)}


def _status(root: Path) -> list[str]:
    return [l for l in _git(root, "status", "--porcelain", "--", "state/plan-blitz").splitlines() if l]


def test_land_trail_leaves_the_waves_trail_clean_in_one_commit(repo):
    wave = _trail(repo)
    before = int(_git(repo, "rev-list", "--count", "HEAD"))

    out = bl.land_trail(repo, wave)

    assert out["error"] is None and out["committed"], out
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == before + 1
    assert _status(repo) == []
    assert "fire-0-1.mjs" not in _git(repo, "ls-files", "state/plan-blitz")
    assert len(_git(repo, "ls-files", "state/plan-blitz").split()) == 3


def test_a_peer_held_trail_path_is_skipped_and_stays_dirty(repo, monkeypatch):
    wave = _trail(repo)
    held = "state/plan-blitz/20261015T000000Z/wave-0.gate-report.json"

    def fake_peer(root, paths, peer_holds=None, **_kw):
        peer_holds.append((held, "peer-sid"))
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake_peer)

    out = bl.land_trail(repo, wave)

    assert out["committed"], out
    assert out["skipped_peer_held"] == [held]
    assert held not in out["paths"]
    assert [l[3:] for l in _status(repo)] == [held]


def test_commit_receives_explicit_file_paths_never_a_directory(repo, monkeypatch):
    wave = _trail(repo)
    seen = {}

    def fake_handler(params, repo_root=None):
        seen.update(params)
        return {"committed": True, "sha": "abc"}

    monkeypatch.setattr(commit_v2, "_handler", fake_handler)

    out = bl.land_trail(repo, wave)

    assert out["sha"] == "abc"
    assert seen["paths"] and all((repo / p).is_file() for p in seen["paths"])
    assert all(p.startswith("state/plan-blitz/20261015T000000Z/") for p in seen["paths"])


def test_a_wave_with_no_trail_commits_nothing(repo):
    out = bl.land_trail(repo, {"waveIndex": 0})
    assert out["committed"] is False and out["paths"] == []


def test_a_commit_failure_is_reported_not_raised(repo, monkeypatch):
    wave = _trail(repo)

    def boom(params, repo_root=None):
        raise RuntimeError("no")

    monkeypatch.setattr(commit_v2, "_handler", boom)

    out = bl.land_trail(repo, wave)

    assert out["committed"] is False and "RuntimeError" in out["error"]
