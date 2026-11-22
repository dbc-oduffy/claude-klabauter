"""A publish child killed mid-run leaves pid-tagged staging dirs and a `.deleting-<pid>` shadow tree;
the next publish run's stale sweep and the round's shadow reap leave neither.

Run: python -m pytest coordinator/bin/tests/test_publish_killed_run_staging_reaped.py -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent

_CHILD = r"""
import importlib.util, sys
from pathlib import Path

from coordinator_core.percolate.shadow_store import ShadowStore

spec = importlib.util.spec_from_file_location("publish_killed_child", sys.argv[1])
publish = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = publish
spec.loader.exec_module(publish)
dest = Path(sys.argv[2])
staging = publish._create_publish_staging_dir(dest)
(staging / "payload.txt").write_text("row bytes", encoding="utf-8")

store = ShadowStore(toplevel=dest, root=Path(sys.argv[3]))
entry = store.entry_path("a" * 64)
(entry / "tree").mkdir(parents=True)
(entry / "tree" / "f.txt").write_text("shadow", encoding="utf-8")
assert store._discard(entry)
print("ready", flush=True)
sys.stdin.read()
"""


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_killed_run_reaped_under_test", "publish.py")


@pytest.fixture(autouse=True)
def _clean_minted():
    publish._MINTED_STAGING_DIRS.clear()
    yield
    publish._MINTED_STAGING_DIRS.clear()


@pytest.fixture
def killed_child(tmp_path):
    dest = tmp_path / "mirror" / "coordinator_core"
    dest.mkdir(parents=True)
    store_root = tmp_path / "store"
    store_root.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(_BIN_DIR / "publish.py"), str(dest), str(store_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_REPO_ROOT),
        env={**os.environ,"PYTHONPATH": str(_REPO_ROOT)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        line = proc.stdout.readline()
        assert line.strip() == "ready", proc.stderr.read()
        proc.kill()
    finally:
        proc.kill()
        proc.wait(timeout=30)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
    return dest, store_root, proc.pid


def _staging(dest: Path) -> "list[Path]":
    return sorted(dest.parent.glob(f".{dest.name}.publish-staging-*"))


def _doomed(store_root: Path) -> "list[Path]":
    return sorted(p for p in store_root.iterdir() if ".deleting-" in p.name)


def test_killed_child_leaves_pid_tagged_dirs_before_the_next_run(killed_child):
    dest, store_root, pid = killed_child

    staged = _staging(dest)
    doomed = _doomed(store_root)
    assert len(staged) == 1 and f".publish-staging-p{pid}-" in staged[0].name
    assert len(doomed) == 1 and doomed[0].name.endswith(f".deleting-{pid}")


def test_next_publish_run_and_round_reap_leave_neither(killed_child, monkeypatch):
    dest, store_root, _pid = killed_child
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: None)

    def next_run(_argv):
        publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())
        return 0

    monkeypatch.setattr(publish, "_main", next_run)
    assert publish.main([]) == 0
    assert _staging(dest) == [], "the killed child's staging dir must be swept by the next run"

    from coordinator_core.percolate import shadow_store

    monkeypatch.setattr(shadow_store, "ROOT_OVERRIDE", store_root)
    percolate_round = _load("percolate_round_killed_run_under_test", "percolate-round.py")
    percolate_round._reap_shadow_store(str(dest))

    assert _doomed(store_root) == [], "the killed child's .deleting tree must be reaped by the round"


def test_a_live_owners_staging_dir_survives_the_next_run(tmp_path):
    dest = tmp_path / "mirror" / "coordinator_core"
    dest.mkdir(parents=True)

    live = dest.parent / f".{dest.name}.publish-staging-p{os.getppid()}-abcd1234"
    live.mkdir()

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert live.is_dir()
