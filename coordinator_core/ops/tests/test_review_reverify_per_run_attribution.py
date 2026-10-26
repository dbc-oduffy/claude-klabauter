"""review.reverify attributes a created file per run: two runs sharing one session, run A's
reverify leaves run B's agent's undeclared untracked file in place and unmoved."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edit_source
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-shared"
PREFIX = "prep-run-a"
BASE = "abc1234"
MOD = b"VALUE = 1\n"
TEST_BODY = b"def test_x():\n    assert False\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_bytes(MOD)
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    (snap / _sha(MOD)).write_bytes(MOD)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": BASE,
            "recorded_at": "2026-10-19T00:00:00Z",
            "paths": ["pkg/mod.py"],
            "files": {"pkg/mod.py": _sha(MOD)},
        }),
        encoding="utf-8",
    )
    for rel in ("tests/test_a_new.py", "scratch/b/notes.py"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(TEST_BODY)
    by_agent = {
        "agentA": {"pkg/mod.py", "tests/test_a_new.py", f".coordinator-local/x/{PREFIX}/note.md"},
        "agentB": {"scratch/b/notes.py"},
    }
    latest = {"pkg/mod.py": "agentA", "tests/test_a_new.py": "agentA", "scratch/b/notes.py": "agentB"}
    monkeypatch.setattr(review_edit_source, "session_review_writes", lambda *_a, **_k: dict(latest))
    monkeypatch.setattr(review_edit_source, "session_agent_writes", lambda *_a, **_k: {k: set(v) for k, v in by_agent.items()})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    return tmp_path, by_agent, latest


def _list(root):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION)


def test_list_attributes_only_run_a_agent_file_to_run_a(world):
    root, _by, _latest = world
    listed = _list(root)
    assert listed["created"] == ["tests/test_a_new.py"]
    assert listed["foreign"] == ["scratch/b/notes.py"]


def test_revert_moves_run_a_file_aside_and_leaves_run_b_file_in_place(world):
    root, _by, _latest = world
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION,
        failing=["scratch/b/notes.py::test_x", "tests/test_a_new.py::test_x"],
    )
    assert out["error"] is None
    assert [r["path"] for r in out["reverted"]] == ["tests/test_a_new.py"]
    assert out["reverted"][0]["action"] == "removed"
    assert (root / out["reverted"][0]["kept_at"]).read_bytes() == TEST_BODY
    assert not (root / "tests/test_a_new.py").exists()
    assert (root / "scratch/b/notes.py").read_bytes() == TEST_BODY


def test_agent_with_no_write_in_the_run_surface_is_foreign_even_alone(world, monkeypatch):
    root, by_agent, _latest = world
    by_agent["agentA"] = {"tests/test_a_new.py"}
    listed = _list(root)
    assert listed["created"] == []
    assert sorted(listed["foreign"]) == ["scratch/b/notes.py", "tests/test_a_new.py"]


def test_agent_naming_the_slice_prefix_in_a_write_belongs_to_the_run(world):
    root, _by, _latest = world
    assert _list(root)["created"] == ["tests/test_a_new.py"]


def test_unreadable_agent_record_fails_closed_and_reverts_nothing(world, monkeypatch):
    root, _by, _latest = world
    monkeypatch.setattr(review_edit_source, "session_agent_writes", lambda *_a, **_k: None)
    listed = _list(root)
    assert listed["created"] == []
    assert sorted(listed["foreign"]) == ["scratch/b/notes.py", "tests/test_a_new.py"]
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=["tests/test_a_new.py::test_x"],
    )
    assert out["reverted"] == []
    assert (root / "tests/test_a_new.py").read_bytes() == TEST_BODY


def test_sibling_agent_touching_a_manifest_path_is_not_a_member(world):
    root, by_agent, _latest = world
    by_agent["agentB"] = {"scratch/b/notes.py", "pkg/mod.py"}
    listed = _list(root)
    assert listed["created"] == ["tests/test_a_new.py"]
    assert listed["foreign"] == ["scratch/b/notes.py"]
