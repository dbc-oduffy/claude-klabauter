"""The publisher-chain export archives a path subset and reuses publish.py's engine export.

Process time and spawn count are pinned on the real tree at HEAD; the per-sha engine export
is warmed by publish.py's own `_export_engine_at_ref`, the export the chain must reuse.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BIN_DIR.parents[1]
_BUDGET_SECS = 0.5


def _load(name: str, script: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rnd():
    return _load("percolate_round_chain_budget", "percolate-round.py")


@pytest.fixture(autouse=True)
def _managed_temp_under_tmp_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def _spawned(monkeypatch) -> list:
    argvs: list = []
    real = subprocess.run

    def spy(args, *a, **k):
        argvs.append(list(args))
        return real(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    return argvs


def _git_args(argv: list) -> list:
    """The git subcommand and its arguments, past `git -C <repo>`."""
    return argv[3:]


def _process_secs() -> float:
    """This process and its reaped children, user+system (children read 0 on Windows)."""
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_chain_export_on_the_real_tree_is_a_pathspec_subset_under_the_bar(rnd, tmp_path, monkeypatch):
    sha = _head(_REPO_ROOT)
    publish = _load("publish_chain_budget_engine", "publish.py")
    engine = publish._export_engine_at_ref(_REPO_ROOT, sha)  # the shared export, warmed outside the timer
    argvs = _spawned(monkeypatch)

    start = _process_secs()
    dest = rnd._export_chain_at_sha(_REPO_ROOT, sha, tmp_path / "chain")
    elapsed = _process_secs() - start

    assert elapsed < _BUDGET_SECS, f"chain export took {elapsed:.3f}s process time"
    subcommands = [_git_args(a) for a in argvs]
    assert [a[0] for a in subcommands] == ["archive", "rev-parse"], subcommands
    archive = subcommands[0]
    assert archive[archive.index("--") + 1:] == list(rnd._CHAIN_EXPORT_PATHS)
    assert (dest / "coordinator_core").resolve() == (engine / "coordinator_core").resolve()
    assert (dest / "coordinator" / "lib").resolve() == (engine / "coordinator" / "lib").resolve()
    for script in ("publish.py", "percolate-gate.py", "percolate-push.py", "percolate-parse-dryrun.py"):
        assert (dest / "coordinator" / "bin" / script).is_file()
    assert (dest / "setup" / "publish-targets.portable").is_file()
    assert not (dest / "docs").exists() and not (dest / "state").exists()


def test_cold_engine_adds_only_publishs_own_two_spawns(rnd, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for rel in (*rnd._CHAIN_EXPORT_PATHS[:5], "coordinator/bin/lib/h.py", "setup/s.txt",
                "coordinator_core/percolate/e.py", "coordinator/lib/percolate/t.py"):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")
    (repo / "coordinator_core" / "temp_layout.py").write_bytes(
        (_REPO_ROOT / "coordinator_core" / "temp_layout.py").read_bytes()
    )
    (repo / "coordinator/bin/publish.py").write_bytes((_BIN_DIR / "publish.py").read_bytes())
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "init")):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)
    sha = _head(repo)
    argvs = _spawned(monkeypatch)

    dest = rnd._export_chain_at_sha(repo, sha, tmp_path / "chain")
    assert [_git_args(a)[0] for a in argvs] == ["archive", "rev-parse", "archive"]

    argvs.clear()
    assert rnd._export_chain_at_sha(repo, sha, tmp_path / "chain") == dest
    assert argvs == []


def test_engine_overlap_is_the_one_publish_exports(rnd):
    publish = _load("publish_chain_budget_paths", "publish.py")
    assert set(rnd._ENGINE_LINKED_PATHS) == set(publish._SOURCE_REF_IMPORTED_PATHS)
    assert not set(rnd._CHAIN_EXPORT_PATHS) & set(rnd._ENGINE_LINKED_PATHS)
    assert all(not p.startswith(("coordinator/lib", "coordinator_core/")) or p == "coordinator_core/temp_layout.py"
               for p in rnd._CHAIN_EXPORT_PATHS)
