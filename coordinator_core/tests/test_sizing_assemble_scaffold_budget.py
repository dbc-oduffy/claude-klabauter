"""`_scaffold_missing` stays in-engine: no doc-new exec-load, no spawn, under the 500 ms bar."""
from __future__ import annotations

import importlib.util
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core import sizing_assemble as sa
from coordinator_core.bin_lib_binding import exec_module_bin_bound

_REPO = Path(__file__).resolve().parents[2]
_DOC_NEW = _REPO / "coordinator" / "bin" / "coordinator-doc-new.py"


def _doc_new_module():
    spec = importlib.util.spec_from_file_location("doc_new_oracle_for_scaffold_budget", _DOC_NEW)
    mod = importlib.util.module_from_spec(spec)
    exec_module_bin_bound(spec.loader, mod, str(_DOC_NEW.parent))
    return mod


@pytest.fixture
def declared(monkeypatch):
    seen: list[Path] = []
    import coordinator_core.session.declared_writes as dw

    monkeypatch.setattr(dw, "declare_write", lambda p, *a, **k: seen.append(Path(p)))
    return seen


def test_scaffold_missing_default_path_is_under_bar_and_spawns_nothing(tmp_path, monkeypatch, declared):
    def _no_spawn(*a, **k):
        raise AssertionError("scaffold spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    target = tmp_path / "state" / "sizings" / "2026-11-18-x.yaml"
    t0 = time.process_time()
    sa._scaffold_missing(tmp_path, target, {"intent": "do the thing"}, None, None)
    elapsed_ms = (time.process_time() - t0) * 1000
    assert elapsed_ms < 500, elapsed_ms
    assert target.read_text(encoding="utf-8").startswith("schema: sizing-object\n")
    assert declared == [target]


def test_scaffold_missing_does_not_load_doc_new(tmp_path, monkeypatch, declared):
    import coordinator_core.bin_lib_binding as bl

    def _boom(*a, **k):
        raise AssertionError("doc-new exec-loaded")

    monkeypatch.setattr(bl, "exec_module_bin_bound", _boom)
    sa._scaffold_missing(tmp_path, tmp_path / "s.yaml", {"intent": "x"}, "read", "f.py:1")


def test_exclusive_create_still_refuses_an_existing_target(tmp_path, declared):
    target = tmp_path / "s.yaml"
    target.write_text("keep\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sa._scaffold_missing(tmp_path, target, {"intent": "x"}, None, None)
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_missing_intent_is_still_refused(tmp_path, declared):
    with pytest.raises(sa.SizingAssembleError):
        sa._scaffold_missing(tmp_path, tmp_path / "s.yaml", {}, None, None)
    assert not (tmp_path / "s.yaml").exists()


@pytest.mark.parametrize(
    "title,deliverable_id,premise,evidence",
    [
        ("plain ask", None, None, None),
        ('quoted "ask"\nwith\tctl \\ chars', "dlv-abc123", "executed", "tests/x.py:12"),
        ("recorded", "dlv-1", "read", "cmd output"),
    ],
)
def test_in_engine_scaffold_is_byte_identical_to_doc_new(title, deliverable_id, premise, evidence):
    oracle = _doc_new_module()._scaffold_sizing(
        title=title,
        deliverable_id=deliverable_id,
        premise=premise,
        premise_evidence=evidence,
    )
    assert (
        sa.scaffold_sizing_text(
            title=title, deliverable_id=deliverable_id, premise=premise, premise_evidence=evidence
        )
        == oracle
    )
