"""Tests for coordinator_core.write_guards._memory_reroute."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter import schema_validate
from coordinator_core.write_guards._memory_reroute import added_lines, reroute_to_lesson

BODY = "---\nname: Pin the root\ndescription: commits refuse unless pinned\n---\n\nPin CLAUDE_KLABAUTER_ROOT first.\n"


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    root = tmp_path / "out"
    root.mkdir()
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return root


def _reroute(content, filename="pin_root.md", cwd=None):
    return reroute_to_lesson(
        content,
        display=f"slug/memory/{filename}",
        filename=filename,
        cwd=cwd,
        session_id="",
    )


def test_body_file_becomes_valid_lesson_with_equal_body(out_root):
    path = _reroute(BODY)
    assert path is not None and Path(path).is_file()
    assert Path(path).parent == out_root / "state" / "lessons"
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assert schema_validate.validate("lesson-entry", doc)["ok"]
    assert doc["body"].strip() == BODY.strip()
    assert doc["scope"] == "project" and doc["status"] == "open"
    assert doc["tags"] == ["rerouted-from-memory"]
    assert doc["evidence"] == "rerouted from slug/memory/pin_root.md by guard_memory_store_cap"


def test_frontmatter_name_is_title_and_stem_otherwise(out_root):
    doc = yaml.safe_load(Path(_reroute(BODY)).read_text(encoding="utf-8"))
    assert doc["title"] == "Pin the root"
    doc = yaml.safe_load(Path(_reroute("plain prose", filename="stem_name.md")).read_text(encoding="utf-8"))
    assert doc["title"] == "stem_name"


def test_description_titles_when_no_name(out_root):
    content = "---\ndescription: only a description\n---\nbody\n"
    doc = yaml.safe_load(Path(_reroute(content)).read_text(encoding="utf-8"))
    assert doc["title"] == "only a description"


def test_unresolvable_destination_returns_none_and_writes_nothing(tmp_path, monkeypatch):
    from coordinator_core.ops import queue_append

    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
    monkeypatch.setattr(queue_append, "_claude_klabauter_root", lambda: None)
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    assert _reroute(BODY, cwd=str(nogit)) is None
    assert not list(tmp_path.rglob("*.yaml"))


def test_append_exception_returns_none(out_root, monkeypatch):
    from coordinator_core.ops import queue_append

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(queue_append, "append_queue_entry", boom)
    assert _reroute(BODY) is None


def test_added_lines_returns_only_new_lines():
    old = "# idx\n- a\n- b\n"
    new = "# idx\n- a\n- b\n- c\n- d\n"
    assert added_lines(old, new) == "- c\n- d"
    assert added_lines(old, old) == ""


def test_zero_spawns(out_root, monkeypatch):
    def no_spawn(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    assert _reroute(BODY) is not None


def test_same_content_same_day_is_one_file(out_root):
    assert _reroute(BODY) == _reroute(BODY)
    assert len(list((out_root / "state" / "lessons").glob("*.yaml"))) == 1
