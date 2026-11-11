"""The sync SessionStart dispatcher names each open owed leg and adds nothing when none is open."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.hooks import sessionstart_dispatch as sd
from coordinator_core.ops.dispatch_emit.owed_leg_queue import write_owed_legs

PLAN = "docs/plans/p.md"
_ROW = "- id: {rid}\n  title: t\n  change_kind: code-edit\n  surface: a.md\n  writes: [a.md]\n  disposition: {disp}\n  body: b\n"


def _plan_text(c1):
    rows = "".join(_ROW.format(rid=rid, disp=disp) for rid, disp in (("C1", c1), ("C2", "open")))
    return "---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN).write_text(_plan_text("open"), encoding="utf-8")
    for _k, _s, fn_name in sd._LEGS:
        monkeypatch.setattr(sd, fn_name, lambda payload: None)
    return tmp_path


def _run(repo, source="startup"):
    result = asyncio.run(sd._handler({"payload": {"source": source, "cwd": str(repo)}}))
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def _owe(repo, *ids):
    write_owed_legs(
        repo, "run1", PLAN, [{"id": i, "reason": "em-performed", "detail": ""} for i in ids]
    )


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact", "fork"])
def test_each_open_leg_is_named_on_every_source(repo, source):
    _owe(repo, "C1", "C2")
    ctx = _run(repo, source)
    assert "OWED run1 C1 (em-performed)" in ctx
    assert "OWED run1 C2 (em-performed)" in ctx


def test_nothing_added_when_none_open(repo):
    assert _run(repo) == ""


def test_discharged_leg_is_dropped(repo):
    _owe(repo, "C1", "C2")
    (repo / PLAN).write_text(
        _plan_text("coded"), encoding="utf-8",
    )
    ctx = _run(repo)
    assert "run1 C1 " not in ctx
    assert "OWED run1 C2" in ctx


def test_no_repo_root_adds_nothing(tmp_path):
    assert sd._leg_owed_legs({"source": "startup", "cwd": str(tmp_path)}) is None
    assert sd._leg_owed_legs({"source": "startup"}) is None


def test_unmatched_source_still_returns_only_breadcrumb(repo):
    _owe(repo, "C1")
    ctx = _run(repo, "bogus")
    assert "matches no guard in REGISTRY" in ctx
    assert "OWED" not in ctx


def test_hundred_rows_read_under_budget(repo):
    _owe(repo, *[f"R{i}" for i in range(100)])
    out: dict = {}

    def _call():
        out["text"] = sd._leg_owed_legs({"cwd": str(repo)})

    timing = in_process_time_ms(_call)
    assert timing["process_time_ms"] < 500.0, timing["process_time_ms"]
    assert out["text"].count("OWED run1") == 100
