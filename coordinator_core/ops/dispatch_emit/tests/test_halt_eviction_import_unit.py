"""A run's import-linked own paths are parked together and the tree keeps every byte: no import unit can
be half reset because nothing is reset. Driven through `plan_tree_eviction(...).apply()`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_check
from coordinator_core.ops.dispatch_emit.halted_record_eviction import EVICTED_PARKED, plan_tree_eviction

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REC = "state/warp/run-x"
HEAD = {"a.py": "import b\n", "b.py": "X = 1\n", "c.py": "Y = 1\n"}
RUN_BYTES = {"a.py": "import b\nA = b.X\n", "b.py": "X = 1\nZ = 2\n", "c.py": "Y = 1\nW = 3\n"}


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    for rel, text in HEAD.items():
        (root / rel).write_bytes(text.encode())
    subprocess.run(["git", "add", *HEAD], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "head"],
        cwd=root, check=True,
    )
    for rel, text in RUN_BYTES.items():
        (root / rel).write_bytes(text.encode())
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {})
    return root


def _bytes(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode()


def test_linked_pair_and_a_dirty_outside_importer_are_parked_and_the_tree_is_untouched(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch)
    (root / "x.py").write_bytes(b"import a\n")
    withheld: dict = {}
    ev = plan_tree_eviction(
        root, REC, ["a.py", "b.py", "c.py"], {"x.py", "a.py", "b.py", "c.py"}, ["a.py", "b.py", "c.py"],
        withheld, [],
    )
    assert ev is not None
    ev.apply()
    assert ev.evicted == ["a.py", "b.py", "c.py"]
    assert {withheld[p] for p in ev.evicted} == {EVICTED_PARKED}
    for rel, text in RUN_BYTES.items():
        assert _bytes(root, rel) == text
    index = json.loads((root / REC / "evicted.json").read_text(encoding="utf-8"))
    assert set(index) == {"a.py", "b.py", "c.py"}
    assert all(entry["head"] == entry["blob"] for entry in index.values())


def test_body_only_edit_is_parked_without_a_peer_import_scan(tmp_path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import worktree_snapshot

    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worktree_snapshot, "changed_python_paths",
        lambda _root: pytest.fail("parking reads no peer status"),
    )
    ev = plan_tree_eviction(root, REC, ["b.py"], {"b.py"}, ["b.py"], {}, [])
    assert ev is not None
    ev.apply()
    assert _bytes(root, "b.py") == RUN_BYTES["b.py"]
