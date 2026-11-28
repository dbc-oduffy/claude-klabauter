"""The clean-overlay run holder is a run_scratch scratch claim: present while a verify's pytest runs, gone when it returns or raises, and a dead worker's holder is swept by the next claim."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import run_scratch
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

pytestmark = [pytest.mark.complete]

DEAD_PID = 2**22 + 12345


@pytest.fixture()
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "scratch-root"
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_SCRATCH_ROOT", str(root))
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_BUILD_ROOT", str(tmp_path / "build-root"))
    return root


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "work" / "myrepo"
    path.mkdir(parents=True)
    return path


def _stub_body(monkeypatch: pytest.MonkeyPatch, run) -> None:
    monkeypatch.setattr(cov, "_advance", lambda *a, **k: 0)
    monkeypatch.setattr(cov, "_mirror_closure", lambda mroot: None)
    monkeypatch.setattr(cov, "_pytest_argv", lambda *a, **k: ["pytest"])
    monkeypatch.setattr(cov, "_pytest_env", lambda *a, **k: {})
    monkeypatch.setattr(cov, "_run", run)


def _base_failing(repo: Path, tmp_path: Path) -> frozenset:
    return cov._base_failing_body(tmp_path / "dest", tmp_path / "mroot", repo, "run-1", "a" * 40, ["t.py::x"], None)


def _kind_dir(scratch: Path) -> Path:
    return scratch / "myrepo" / cov.RUN_HOLDER_KIND


def test_run_holder_is_a_live_scratch_claim_during_the_run_and_gone_after(scratch, repo, tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def run(argv, dest, *a, **k):
        holders = list(_kind_dir(scratch).iterdir())
        seen["holders"] = holders
        mark = run_scratch._read_mark(holders[0])
        seen["owner"] = mark["pid"]
        seen["peer"] = run_scratch.claim("myrepo", cov.RUN_HOLDER_KIND, "peer")
        seen["swept_live"] = not holders[0].exists()
        return SimpleNamespace(stdout="", stderr="")

    _stub_body(monkeypatch, run)
    _base_failing(repo, tmp_path)
    import os

    assert len(seen["holders"]) == 1 and seen["owner"] == os.getpid()
    assert not seen["swept_live"]
    assert not seen["holders"][0].exists()
    assert seen["peer"].exists()


def test_run_holder_is_gone_when_the_run_raises(scratch, repo, tmp_path, monkeypatch) -> None:
    def run(*a, **k):
        raise OSError("launch failed")

    _stub_body(monkeypatch, run)
    assert _base_failing(repo, tmp_path) == frozenset()
    assert list(_kind_dir(scratch).iterdir()) == []


def test_a_dead_workers_holder_is_swept_by_the_next_claim(scratch, repo) -> None:
    dead = run_scratch.claim("myrepo", cov.RUN_HOLDER_KIND, "left-behind", pid=DEAD_PID)
    live = run_scratch.claim("myrepo", cov.RUN_HOLDER_KIND, "mine")
    holder, basetemp, tmp = cov._claim_run_dirs(repo, "run-2")
    assert not dead.exists()
    assert live.exists() and holder.exists() and basetemp.is_dir() and tmp.is_dir()
    assert holder.parent == _kind_dir(scratch)
    run_scratch.finish(holder)
    assert not holder.exists()


def test_a_slug_with_path_characters_still_claims(scratch, repo) -> None:
    holder, _, _ = cov._claim_run_dirs(repo, "ask-1:run/2")
    try:
        assert holder.parent == _kind_dir(scratch)
    finally:
        run_scratch.finish(holder)
