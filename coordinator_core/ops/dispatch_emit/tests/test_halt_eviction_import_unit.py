"""Eviction treats a run's import-linked own paths as one unit: a refusal of either side keeps both,
so the tree never holds half of a cross-file edit. Driven through `plan_tree_eviction(...).apply()`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halt_tree_check
from coordinator_core.ops.dispatch_emit.halted_record_eviction import plan_tree_eviction

REC = "state/warp/run-x"
RUN = "r1"
HEAD = {"a.py": "import b\n", "b.py": "X = 1\n", "c.py": "Y = 1\n"}
RUN_BYTES = {"a.py": "import b\nA = b.X\n", "b.py": "X = 1\nZ = 2\n", "c.py": "Y = 1\nW = 3\n"}


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: set[str]):
    """HEAD holds HEAD; the tree holds RUN_BYTES. A `foreign` path's run baseline is already its tree
    bytes, so none of its hunks is the run's."""
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
    common = resolve_git_common_dir(root)
    baseline = {
        rel: git_objects.write_object(common, b"blob", (RUN_BYTES if rel in foreign else HEAD)[rel].encode())
        for rel in HEAD
    }
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": baseline}), encoding="utf-8"
    )
    for rel, text in RUN_BYTES.items():
        (root / rel).write_bytes(text.encode())
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {})
    return root


def _evict(root: Path, paths: list[str]):
    ev = plan_tree_eviction(root, REC, paths, set(paths), paths, {}, [], run_id=RUN)
    assert ev is not None
    ev.apply()
    return ev


def _bytes(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode()


def test_importer_refusal_keeps_the_imported_file(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign={"a.py"})
    ev = _evict(root, ["a.py", "b.py"])
    assert _bytes(root, "a.py") == RUN_BYTES["a.py"]
    assert _bytes(root, "b.py") == RUN_BYTES["b.py"]
    assert "import-linked to a.py" in ev.refused["b.py"]
    assert ev.evicted == []
    assert "A = b.X" in _bytes(root, "a.py") and "X = 1" in _bytes(root, "b.py")


def test_imported_refusal_keeps_the_importer(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign={"b.py"})
    ev = _evict(root, ["a.py", "b.py"])
    assert _bytes(root, "a.py") == RUN_BYTES["a.py"]
    assert _bytes(root, "b.py") == RUN_BYTES["b.py"]
    assert "import-linked to b.py" in ev.refused["a.py"]
    assert ev.evicted == []


def test_linked_pair_without_refusal_is_reset_together(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign=set())
    ev = _evict(root, ["a.py", "b.py"])
    assert _bytes(root, "a.py") == HEAD["a.py"] and _bytes(root, "b.py") == HEAD["b.py"]
    assert ev.refused == {}


def test_unlinked_pair_is_judged_independently(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign={"b.py"})
    ev = _evict(root, ["b.py", "c.py"])
    assert _bytes(root, "b.py") == RUN_BYTES["b.py"]
    assert _bytes(root, "c.py") == HEAD["c.py"]
    assert list(ev.refused) == ["b.py"]
    assert "import-linked" not in ev.refused["b.py"]


def _plan(root: Path, parked: list[str], changed: set[str]):
    return plan_tree_eviction(root, REC, parked, changed, parked, {}, [], run_id=RUN)


def _evicted_index(root: Path) -> dict:
    path = root / REC / "evicted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_module_with_a_dirty_importer_outside_the_parked_set_is_kept(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign=set())
    ev = _plan(root, ["b.py", "c.py"], {"a.py", "b.py", "c.py"})
    assert ev is not None
    ev.apply()
    assert _bytes(root, "b.py") == RUN_BYTES["b.py"]
    assert _bytes(root, "c.py") == HEAD["c.py"]
    assert set(_evicted_index(root)) == {"c.py"}
    assert ev.evicted == ["c.py"]


def test_outside_importer_keeps_the_whole_import_unit(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign=set())
    (root / "x.py").write_bytes(b"import a\n")
    ev = _plan(root, ["a.py", "b.py"], {"x.py", "a.py", "b.py"})
    assert ev is None
    assert _bytes(root, "a.py") == RUN_BYTES["a.py"] and _bytes(root, "b.py") == RUN_BYTES["b.py"]
    assert _evicted_index(root) == {}


def test_unrelated_dirty_outside_file_keeps_nothing(tmp_path, monkeypatch) -> None:
    root = _world(tmp_path, monkeypatch, foreign=set())
    (root / "x.py").write_bytes(b"import unrelated\n")
    ev = _plan(root, ["b.py"], {"x.py", "b.py"})
    assert ev is not None
    ev.apply()
    assert _bytes(root, "b.py") == HEAD["b.py"]


def test_clean_importer_outside_the_changed_set_keeps_nothing(tmp_path, monkeypatch) -> None:
    # a.py names b but is not dirty (not in `changed`): HEAD's b still satisfies it, so b parks.
    root = _world(tmp_path, monkeypatch, foreign=set())
    ev = _plan(root, ["b.py"], {"b.py"})
    assert ev is not None
    ev.apply()
    assert _bytes(root, "b.py") == HEAD["b.py"]


def test_no_units_leaves_siblings_reset_as_before(tmp_path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import halt_tree_evict

    root = _world(tmp_path, monkeypatch, foreign={"a.py"})
    refused: dict = {}
    halt_tree_evict.evict(
        root, root / REC, ["a.py", "b.py"], {p: HEAD[p].encode() for p in ("a.py", "b.py")},
        run_id=RUN, refused=refused,
    )
    assert list(refused) == ["a.py"]
    assert _bytes(root, "b.py") == HEAD["b.py"]
