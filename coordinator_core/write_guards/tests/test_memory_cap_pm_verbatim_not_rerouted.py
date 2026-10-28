"""A new memory at the file cap lands as written and the oldest memory is evicted,
whatever the new or the evicted memory's type; nothing is rerouted to state/lessons/."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.write_guards import guard_memory_store_cap as guard

_LEVEL_ENV = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_GUARD-MEMORY-STORE-CAP"
_TYPES = ["user", "feedback", "project", "reference"]


def _body(mem_type: str, text: str = "x") -> str:
    return f"---\nname: n-{mem_type}\ntype: {mem_type}\n---\n{text}\n"


@pytest.fixture
def store(monkeypatch, tmp_path):
    home = tmp_path / "home"
    mem = home / ".claude" / "projects" / "-Some-project" / "memory"
    mem.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv(_LEVEL_ENV, "strict")
    lessons = tmp_path / "lessons-root"
    lessons.mkdir()
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(lessons))
    return mem, lessons


def _write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


def _seed(mem, oldest_type: str) -> None:
    rows = []
    for i in range(guard.MAX_MEMORY_FILES):
        p = mem / f"m{i}.md"
        p.write_text(_body(oldest_type if i == 0 else "project"), encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
        rows.append(f"- [m{i}](m{i}.md) -- note\n")
    (mem / "MEMORY.md").write_text("".join(rows), encoding="utf-8")


@pytest.mark.parametrize("mem_type", _TYPES)
def test_new_memory_of_any_type_at_cap_is_allowed_and_oldest_evicted(store, mem_type):
    mem, lessons = store
    _seed(mem, mem_type)

    result = guard.check(_write(mem / "new.md", _body(mem_type, "new")))

    assert result is None
    assert not (mem / "m0.md").exists()
    assert (mem / "m1.md").exists()
    index = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert "m0.md" not in index and "m1.md" in index
    log = [json.loads(ln) for ln in (mem.parent / "memory-evictions.jsonl").read_text().splitlines()]
    assert len(log) == 1
    assert log[0]["name"] == "m0.md"
    assert log[0]["type"] == mem_type
    assert log[0]["path"].endswith("m0.md")
    assert log[0]["evicted_at"]
    assert list(lessons.rglob("*")) == []


def test_oversize_new_body_at_cap_is_denied_and_evicts_nothing(store):
    mem, _lessons = store
    _seed(mem, "feedback")

    result = guard.check(_write(mem / "new.md", _body("feedback", "y" * 900)))

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Shorten it" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert (mem / "m0.md").exists()
    assert not (mem.parent / "memory-evictions.jsonl").exists()


def test_eviction_failure_is_a_deny_naming_the_oldest(store, monkeypatch):
    mem, _lessons = store
    _seed(mem, "user")

    def boom(_d):
        raise OSError("locked")

    monkeypatch.setattr(guard, "evict_oldest", boom)
    result = guard.check(_write(mem / "new.md", _body("user")))

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "m0.md" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert not (mem / "new.md").exists()
