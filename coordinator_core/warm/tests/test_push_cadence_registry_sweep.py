"""Registry-union sweep and the one-row-per-sweep telemetry (plan 2026-10-10-push-cadence-sweeps-registry-repos, C2).

Real tmp_path git repos with a local bare remote; the registry reader and the telemetry
directory are the only things faked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._memo_resolver import RegistryReadError
from coordinator_core.warm import push_cadence, telemetry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    svc = tmp_path / "svc"
    monkeypatch.setattr(telemetry, "svc_dir", lambda engine_root=None: svc)
    yield
    push_cadence.reset_cadence_for_test()


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo_ahead(tmp_path: Path, name: str, *, remote: "Path | None" = None) -> Path:
    """A repo on a day branch, one commit ahead of its upstream. A `remote`
    that does not exist makes the later push fail."""
    bare = tmp_path / f"{name}-bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/day"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/day"], repo)
    (repo / "b.txt").write_text("more", encoding="utf-8")
    _git(["add", "--", "b.txt"], repo)
    _git(["commit", "-q", "-m", "unpushed"], repo)
    if remote is not None:
        _git(["remote", "set-url", "origin", str(remote)], repo)
    return repo


def _remote_head(repo: Path) -> str:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=str(repo), capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    return subprocess.run(
        ["git", "rev-parse", "work/day"], cwd=url, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _local_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _registry(monkeypatch, repos: dict) -> None:
    monkeypatch.setattr(push_cadence, "read_registry_repos", lambda: {k: str(v) for k, v in repos.items()})


def _rows() -> list:
    path = telemetry.push_sweep_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_idle_tick_with_empty_served_set_pushes_registry_repo(tmp_path, monkeypatch):
    repo = _repo_ahead(tmp_path, "reg")
    _registry(monkeypatch, {"reg": repo})
    assert _remote_head(repo) != _local_head(repo)

    push_cadence.on_idle_tick(
        served_repos=lambda: push_cadence.registry_sweep_repos([]), clock=lambda: 0.0
    )
    ran = push_cadence.on_idle_tick(
        served_repos=lambda: push_cadence.registry_sweep_repos([]),
        clock=lambda: 601.0,
        interval_secs=600.0,
    )

    assert ran is True
    assert _remote_head(repo) == _local_head(repo)
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["pushed"] == [str(repo)]


def test_registry_union_orders_served_first_and_dedups(tmp_path, monkeypatch):
    served = tmp_path / "served"
    other = tmp_path / "other"
    served.mkdir()
    other.mkdir()
    _registry(monkeypatch, {"a": other, "b": served})

    assert push_cadence.registry_sweep_repos([served]) == [served, other]


def test_missing_registry_path_is_dropped(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    _registry(monkeypatch, {"gone": tmp_path / "nope", "live": live})

    assert push_cadence.registry_sweep_repos([]) == [live]


def test_registry_read_error_leaves_served_set(tmp_path, monkeypatch):
    served = tmp_path / "served"
    served.mkdir()

    def _boom():
        raise RegistryReadError("unreadable")

    monkeypatch.setattr(push_cadence, "read_registry_repos", _boom)

    assert push_cadence.registry_sweep_repos([served]) == [served]


def test_each_sweep_appends_one_row_naming_pushed_and_failed(tmp_path, monkeypatch):
    good = _repo_ahead(tmp_path, "good")
    bad = _repo_ahead(tmp_path, "bad", remote=tmp_path / "missing-remote.git")
    clean = _repo_ahead(tmp_path, "clean")
    _git(["push", "-q", "origin", "work/day"], clean)

    push_cadence.sweep_repos([good, bad, clean], clock=lambda: 0.0)
    rows = _rows()

    assert len(rows) == 1
    assert rows[0]["pushed"] == [str(good)]
    assert rows[0]["failed"] == [str(bad)]
    assert rows[0]["skipped"] == [str(clean)]

    push_cadence.sweep_repos([clean], clock=lambda: 0.0)
    assert len(_rows()) == 2


def test_ceiling_cut_repos_count_as_skipped(tmp_path, monkeypatch):
    first = _repo_ahead(tmp_path, "first")
    second = _repo_ahead(tmp_path, "second")
    ticks = iter([0.0, 0.0, 100.0])
    clock = lambda: next(ticks, 100.0)  # noqa: E731

    push_cadence.sweep_repos(
        [first, second], total_ceiling_secs=8.0, per_repo_budget_secs=5.0, clock=clock
    )

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["pushed"] == [str(first)]
    assert rows[0]["skipped"] == [str(second)]
    assert rows[0]["failed"] == []
    assert _remote_head(second) != _local_head(second)
