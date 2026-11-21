"""`--real-record` replays a land_run up to the commit leg and commits nothing."""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_run_leg_budget_profile as prof
from coordinator_core.benchmarks.land_run_slow_disk_profile import _bind_session, build_land_fixture


pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _env_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COORDINATOR_INVOCATION_ORIGIN", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "x")


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


def _landing(tmp_path: Path, stop: bool) -> tuple:
    prof.declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        root, base = build_land_fixture(tmp_path, mode="clean", tracked=20, chain=3, stack=stack)
        before = _head(root)
        rows = prof.profile_land(root, base, attribute=False, stop_before_commit=stop)
        return before, _head(root), rows[0]


def test_stop_before_commit_takes_no_commit(tmp_path: Path) -> None:
    before, after, whole = _landing(tmp_path, stop=True)
    assert after == before
    assert whole.attribution["commit_leg_reached"] is True
    assert whole.attribution["outcome"] == "stopped_before_commit"
    assert any(leg["to"] == prof.LEG_COMMIT for leg in whole.attribution["legs"])


def test_without_the_stop_the_same_landing_commits(tmp_path: Path) -> None:
    before, after, whole = _landing(tmp_path, stop=False)
    assert whole.attribution["commit_leg_reached"] is True
    assert after != before


def test_halted_shape_fixture_lands_seven_py_and_a_doc_past_peer_dirt(tmp_path: Path) -> None:
    prof.declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        shape = prof.build_halted_shape_fixture(tmp_path, stack)
        root = shape["root"]
        assert len([p for p in shape["paths"] if p.endswith(".py")]) == 7
        assert all(p.startswith("coordinator_core/ops/dispatch_emit/") for p in shape["paths"] if p.endswith(".py"))
        assert [p for p in shape["paths"] if p.endswith(".md")] == [prof.HALTED_SHAPE_DOC]
        before = _head(root)
        rows = prof.profile_land(root, shape["base"], attribute=False, legs=True)
        whole = rows[0]
        assert whole.attribution["commit_leg_reached"] is True
        assert any(r.axis.startswith("leg:land_run._integrity") for r in rows)
        assert _head(root) != before
        listing = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(root), capture_output=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode().split()
        assert set(shape["paths"]) <= set(listing)
        assert not set(shape["peer_dirty"]) & set(listing)


def test_seed_parse_verdicts_fills_the_cache_for_dirty_py_only(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    root = tmp_path / "r"
    root.mkdir()

    def git(*a) -> None:
        subprocess.run(["git", *a], cwd=str(root), capture_output=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    git("init", "-q")
    (root / "clean.py").write_bytes(b"x = 1\n")
    git("add", "clean.py")
    git("-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q", "-m", "c")
    (root / "dirty.py").write_bytes(b"y = 2\n")
    (root / "notes.md").write_text("n\n")
    assert prof.seed_parse_verdicts(root) == 1
    assert li._verdict_path(root, b"y = 2\n").is_file()
    assert not li._verdict_path(root, b"x = 1\n").is_file()
