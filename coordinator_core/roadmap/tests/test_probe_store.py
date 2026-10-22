"""Tests for coordinator_core.roadmap.probe_store; every path is under tmp_path."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.roadmap import probe_store
from coordinator_core.roadmap.probe_store import (
    GLOBAL_INPUTS,
    ProbeStore,
    engine_key,
    input_paths,
    plan_sha,
    store_path,
)

REL = "docs/plans/p.md"
REPLY = {"plan": REL, "verdict": "PREPPED", "process_ms": 9}


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / REL).write_text("plan", encoding="utf-8")
    return tmp_path


def _old(path: Path) -> None:
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))


def _ident(root: Path, rel: str):
    return probe_store._stat_identity(str(root / rel))


def _seed(root: Path, **legs):
    inp = root / "sizing.yaml"
    inp.write_text("a: 1", encoding="utf-8")
    _old(inp)
    store = ProbeStore.load(root)
    args = dict(plan_sha="s", upgrade=True, head="h", engine_key="e")
    args.update(legs)
    store.put(REL, args["plan_sha"], args["upgrade"], args["head"], args["engine_key"],
              {"sizing.yaml": _ident(root, "sizing.yaml"), "absent.md": None}, REPLY)
    store.save()
    return inp


def _get(root: Path, **legs):
    args = dict(plan_sha="s", upgrade=True, head="h", engine_key="e")
    args.update(legs)
    return ProbeStore.load(root).get(REL, args["plan_sha"], args["upgrade"], args["head"],
                                     args["engine_key"])


def test_round_trip_drops_process_ms(tmp_path):
    root = _root(tmp_path)
    _seed(root)
    assert store_path(root).is_file()
    assert _get(root) == {"plan": REL, "verdict": "PREPPED"}


@pytest.mark.parametrize("leg,value", [
    ("plan_sha", "other"), ("upgrade", False), ("head", "h2"), ("engine_key", "e2"),
    ("engine_key", None),
])
def test_any_changed_leg_misses(tmp_path, leg, value):
    root = _root(tmp_path)
    _seed(root)
    assert _get(root, **{leg: value}) is None


def test_changed_created_or_deleted_input_misses(tmp_path):
    root = _root(tmp_path)
    inp = _seed(root)
    inp.write_text("a: 22", encoding="utf-8")
    _old(inp)
    assert _get(root) is None

    (tmp_path / "b").mkdir()
    root2 = _root(tmp_path / "b")
    _seed(root2)
    (root2 / "absent.md").write_text("x", encoding="utf-8")
    assert _get(root2) is None

    (tmp_path / "c").mkdir()
    root3 = _root(tmp_path / "c")
    inp3 = _seed(root3)
    inp3.unlink()
    assert _get(root3) is None


def test_racy_mtime_misses(tmp_path):
    root = _root(tmp_path)
    store = ProbeStore.load(root)
    inp = root / "fresh.yaml"
    inp.write_text("a", encoding="utf-8")
    store.put(REL, "s", True, "h", "e", {"fresh.yaml": _ident(root, "fresh.yaml")}, REPLY)
    store.save()
    assert _get(root) is None
    _old(inp)
    store2 = ProbeStore.load(root)
    store2.put(REL, "s", True, "h", "e", {"fresh.yaml": _ident(root, "fresh.yaml")}, REPLY)
    store2.save()
    assert _get(root) == {"plan": REL, "verdict": "PREPPED"}


@pytest.mark.parametrize("content", [None, "{not json", '{"schema": 99, "records": {}}',
                                     '{"schema": 1, "records": []}', "[]"])
def test_missing_corrupt_or_other_schema_loads_empty(tmp_path, content):
    root = _root(tmp_path)
    if content is not None:
        path = store_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    assert ProbeStore.load(root).get(REL, "s", True, "h", "e") is None


def test_save_without_change_writes_nothing(tmp_path):
    root = _root(tmp_path)
    ProbeStore.load(root).save()
    assert not store_path(root).exists()
    _seed(root)
    before = store_path(root).stat().st_mtime_ns
    ProbeStore.load(root).save()
    assert store_path(root).stat().st_mtime_ns == before


def test_save_to_unwritable_directory_raises_nothing(tmp_path):
    root = _root(tmp_path)
    store = ProbeStore.load(root)
    store.put(REL, "s", True, "h", "e", {}, REPLY)
    blocker = store_path(root).parent
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("a file where the cache dir belongs", encoding="utf-8")
    store.save()


def test_save_drops_records_of_plans_no_longer_present(tmp_path):
    root = _root(tmp_path)
    store = ProbeStore.load(root)
    store.put(REL, "s", True, "h", "e", {}, REPLY)
    store.put("docs/plans/gone.md", "s", True, "h", "e", {}, REPLY)
    store.save()
    reloaded = ProbeStore.load(root)
    assert reloaded.get(REL, "s", True, "h", "e") is not None
    assert reloaded.get("docs/plans/gone.md", "s", True, "h", "e") is None


def test_other_repo_root_misses(tmp_path):
    root = _root(tmp_path)
    _seed(root)
    store = ProbeStore.load(root)
    store._root = tmp_path / "elsewhere"
    assert store.get(REL, "s", True, "h", "e") is None


def test_plan_sha_is_sha256_of_bytes():
    assert plan_sha(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _fake_engine(tmp_path: Path, monkeypatch) -> Path:
    """A tiny engine checkout: probe -> mod_a -> (function-level) mod_b; `outside.py` is unreached."""
    eng = tmp_path / "eng"
    (eng / ".git").mkdir(parents=True)
    files = {
        "coordinator_core/__init__.py": "",
        "coordinator_core/ops/__init__.py": "",
        "coordinator_core/ops/plan_mise_prep_probe.py":
            "from coordinator_core import mod_a\n",
        "coordinator_core/mod_a.py":
            "def f():\n    from coordinator_core import mod_b\n    return mod_b\n",
        "coordinator_core/mod_b.py": "x = 1\n",
        "coordinator_core/outside.py": "y = 2\n",
        "coordinator_core/frontmatter/schemas/plan.schema.json": "{}",
        "coordinator/bin/mise-prep-upgrade.py": "import json\n",
    }
    for rel, text in files.items():
        path = eng / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _old(path)
    monkeypatch.setattr(probe_store, "ENGINE_CHECKOUT_ROOT", eng)
    return eng


def test_engine_key_none_when_a_closure_file_is_newer_than_import(tmp_path, monkeypatch):
    eng = _fake_engine(tmp_path, monkeypatch)
    key = engine_key("abc")
    assert key is not None and key.startswith("abc:")
    assert engine_key("abc") == key
    future = probe_store._IMPORTED_AT_NS + 10_000_000_000
    os.utime(eng / "coordinator_core" / "mod_a.py", ns=(future, future))
    assert engine_key("abc") is None


def test_engine_key_equal_across_calls_with_an_unrelated_import_between(monkeypatch):
    first = engine_key("abc")
    monkeypatch.delitem(sys.modules, "coordinator_core.roadmap.mise_residue", raising=False)
    import coordinator_core.roadmap.mise_residue  # noqa: F401
    assert engine_key("abc") == first


@pytest.mark.spawns_process
def test_engine_key_is_the_same_in_a_fresh_process():
    import subprocess

    here = engine_key("abc")
    if here is None:
        pytest.skip("a closure file changed since this process imported probe_store")
    out = subprocess.run(
        [sys.executable, "-c",
         "from coordinator_core.roadmap.probe_store import engine_key; print(engine_key('abc'))"],
        cwd=str(ENGINE_CHECKOUT_ROOT), capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.stdout.strip() == here, out.stderr


def test_a_warm_engine_key_costs_under_20ms_of_process_time():
    import time

    first = time.process_time()
    key = engine_key("abc")
    cold_ms = (time.process_time() - first) * 1000
    if key is None:
        pytest.skip("a closure file changed since this process imported probe_store")
    start = time.process_time()
    assert engine_key("abc") == key
    warm_ms = (time.process_time() - start) * 1000
    files = probe_store._closure(os.path.normpath(str(ENGINE_CHECKOUT_ROOT)))
    print(f"engine_key closure={len(files)} files cold={cold_ms:.1f}ms warm={warm_ms:.1f}ms")
    assert warm_ms < 20


def test_a_fresh_process_reuses_the_persisted_import_graph(tmp_path, monkeypatch):
    eng = _fake_engine(tmp_path, monkeypatch)
    root = os.path.normpath(str(eng))
    first = probe_store._closure(root)
    probe_store._IMPORT_PARSE_CACHE.clear()
    probe_store._GRAPH_SEEDED.discard(root)

    def _no_parse(*_a):
        raise AssertionError("re-parsed an unchanged file")

    monkeypatch.setattr(probe_store, "_import_targets", _no_parse)
    assert probe_store._closure(root) == first


def test_engine_key_changes_on_a_closure_file_edit_and_ignores_one_outside(tmp_path, monkeypatch):
    eng = _fake_engine(tmp_path, monkeypatch)
    key = engine_key("abc")
    outside = eng / "coordinator_core" / "outside.py"
    outside.write_text("y = 22\n", encoding="utf-8")
    _old(outside)
    assert engine_key("abc") == key
    deep = eng / "coordinator_core" / "mod_b.py"
    os.utime(deep, ns=(2_000_000_000, 2_000_000_000))
    assert engine_key("abc") != key


def test_a_function_level_import_pulls_its_target_into_the_closure(tmp_path, monkeypatch):
    eng = _fake_engine(tmp_path, monkeypatch)
    files = probe_store._closure(os.path.normpath(str(eng)))
    rels = {os.path.relpath(f, eng).replace(os.sep, "/") for f in files}
    assert "coordinator_core/mod_b.py" in rels
    assert "coordinator_core/outside.py" not in rels


def test_engine_key_changes_with_engine_sha(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch)
    assert engine_key("a") is not None
    assert engine_key("a") != engine_key("b")


def test_input_paths_names_sizing_edge_target_writes_dir_and_globals(tmp_path):
    root = _root(tmp_path)
    fm = {"derived_from": "state/sizings/s.yaml", "status": "approved", "title": "A plan title"}
    rows = [{
        "id": "C1",
        "depends_on_plan": [{"plan": "docs/plans/other.md", "chunk": "C2"}],
        "writes": ["coordinator_core/roadmap/", "coordinator_core/x/*.py", "../escape.md", "/abs.md"],
        "body": "multi\nline prose with spaces.md",
    }]
    got = input_paths(fm, rows, root)
    assert "state/sizings/s.yaml" in got
    assert "docs/plans/other.md" in got
    assert "coordinator_core/roadmap" in got
    assert "coordinator_core/x" in got
    assert "archive/specs" in got
    assert set(GLOBAL_INPUTS) <= set(got)
    assert len(got) == len(set(got))
    assert not any(p.startswith(("..", "/")) or "escape" in p or "abs.md" in p for p in got)
    assert "approved" not in got and "A plan title" not in got


def test_input_paths_adds_top_level_dirs_when_a_first_segment_is_absent(tmp_path):
    root = _root(tmp_path)
    (root / "state").mkdir()
    got = input_paths({}, [{"writes": ["cross-repo/inbox/x.md"]}], root)
    assert "state" in got and "docs" in got
    quiet = input_paths({}, [{"writes": ["docs/plans/x.md"]}], root)
    assert "state" not in quiet


def test_input_paths_names_the_memo_receipt_of_a_memo_row(tmp_path):
    root = _root(tmp_path)
    got = input_paths({}, [{"id": "C1", "receipt": "topic-x"}], root)
    assert any(p.endswith("sent/topic-x.md") for p in got)


READ_SITE_RE = re.compile(r"\.(read_text|read_bytes|iterdir|is_dir|is_file|exists)\(")


def _count_read_sites(path: Path, skip: str = "") -> int:
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines()
        if READ_SITE_RE.search(line) and not line.lstrip().startswith("#")
        and not (skip and skip in line)
    )


def test_read_site_pin_prep_gate_and_upgrader():
    """C30 read-site pin: a new read site in prep_gate.py or mise-prep-upgrade.py must be
    checked against probe_store.input_paths (and the audit in its docstring) before this
    count moves, or the store vouches for a verdict it cannot see the inputs of."""
    gate = ENGINE_CHECKOUT_ROOT / "coordinator_core" / "roadmap" / "prep_gate.py"
    upgrader = ENGINE_CHECKOUT_ROOT / "coordinator" / "bin" / "mise-prep-upgrade.py"
    assert _count_read_sites(gate, skip="_UPGRADE_SCRIPT") == 8
    assert _count_read_sites(upgrader) == 7
