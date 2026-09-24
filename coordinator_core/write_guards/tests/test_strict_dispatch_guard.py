"""Behavioral tests for
coordinator_core.write_guards.block_em_strict_dispatch_code_write -- the
OPT-IN `strict_dispatch` hard-deny guard -- and the engine's env gate that
keeps it out of discovery when unflagged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.write_guards import block_em_strict_dispatch_code_write as guard
from coordinator_core.write_guards import engine

_NAME = "block_em_strict_dispatch_code_write"


def _payload(
    repo_root: Path,
    file_path: str = "src/thing.py",
    tool_name: str = "Write",
    agent_id: str | None = None,
    session_id: str = "sess-12345678",
) -> dict:
    abs_path = str(repo_root / file_path)
    tool_input: dict = {"file_path": abs_path, "content": "x = 1"}
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(repo_root),
        "session_id": session_id,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_STRICT_DISPATCH", raising=False)


@pytest.fixture
def repo_root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _stub_repo_root(monkeypatch, repo_root: Path):
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(repo_root))


def _stub_not_subagent(monkeypatch):
    monkeypatch.setattr(guard, "_is_subagent_session", lambda sid, root: False)


def _stub_touched(monkeypatch, paths: list[str], repo_root: Path | None = None):
    for rel in paths if repo_root else []:
        f = repo_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    monkeypatch.setattr(guard, "_session_touched_sizing_files", lambda sid, root: paths)


def _stub_estimate(monkeypatch, tshirt):
    monkeypatch.setattr(
        guard,
        "_read_sizing_object_fields",
        lambda path: {"estimate": {"tshirt": tshirt}} if tshirt is not None else {"estimate": None},
    )


class TestEngineEnvGate:
    def test_flag_off_skips_without_reading_source(self, monkeypatch):
        read = []
        real = engine._cheap_guard_metadata
        monkeypatch.setattr(
            engine, "_cheap_guard_metadata", lambda name: read.append(name) or real(name)
        )
        guards, _ = engine._discover_guards()
        assert _NAME not in {g.name for g in guards}
        assert _NAME not in read

    @pytest.mark.parametrize("value", ["1", "true", "ON"])
    def test_flag_on_discovers_it(self, monkeypatch, value):
        monkeypatch.setenv("COORDINATOR_STRICT_DISPATCH", value)
        guards, _ = engine._discover_guards()
        assert _NAME in {g.name for g in guards}

    def test_flag_off_evaluate_never_imports_it(self, monkeypatch, repo_root):
        monkeypatch.delitem(sys.modules, guard.__name__, raising=False)
        engine.evaluate(_payload(repo_root))
        assert guard.__name__ not in sys.modules

    def test_gated_guard_still_listed_by_full_discovery(self):
        names, _ = engine.discover_guard_names()
        assert _NAME in names


def test_subagent_agent_id_allows(repo_root):
    assert guard.check(_payload(repo_root, agent_id="aexec-teammate-1234567890abcdef")) is None


def test_doc_file_allows(monkeypatch, repo_root):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    assert guard.check(_payload(repo_root, file_path="docs/plans/foo.md")) is None


def test_no_sizing_denies(monkeypatch, repo_root):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    _stub_touched(monkeypatch, [])
    result = guard.check(_payload(repo_root))
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "coordinator:sizing" in reason


def test_sized_xs_allows(monkeypatch, repo_root):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    _stub_touched(monkeypatch, ["state/sizings/a.yaml"], repo_root)
    _stub_estimate(monkeypatch, "XS")
    assert guard.check(_payload(repo_root)) is None


@pytest.mark.parametrize("tshirt", ["S", "M", "XXL"])
def test_sized_s_or_larger_denies(monkeypatch, repo_root, tshirt):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    _stub_touched(monkeypatch, ["state/sizings/a.yaml"], repo_root)
    _stub_estimate(monkeypatch, tshirt)
    result = guard.check(_payload(repo_root))
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "dispatch an executor" in reason


def test_unparseable_tshirt_denies(monkeypatch, repo_root):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    _stub_touched(monkeypatch, ["state/sizings/a.yaml"], repo_root)
    _stub_estimate(monkeypatch, None)
    result = guard.check(_payload(repo_root))
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ">= S" in reason


def test_autonomous_sentinel_does_not_suppress(monkeypatch, repo_root):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    _stub_touched(monkeypatch, [])

    from coordinator_core.session import autonomous_sentinel

    sentinel = autonomous_sentinel.sentinel_path("sess-12345678")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("1", encoding="utf-8")
    try:
        result = guard.check(_payload(repo_root))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        sentinel.unlink(missing_ok=True)


def test_unexpected_exception_returns_none(monkeypatch, repo_root):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "resolve_repo_root", _boom)
    assert guard.check(_payload(repo_root)) is None


def test_newest_sizing_by_mtime_wins_over_touch_order(monkeypatch, repo_root):
    # The touch record keeps first-touch order: re-sizing a.yaml after b.yaml
    # leaves a.yaml first in the list, yet it is the newest verdict.
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    sizings = repo_root / "state" / "sizings"
    sizings.mkdir(parents=True)
    (sizings / "b.yaml").write_text("estimate:\n  tshirt: M\n")
    (sizings / "a.yaml").write_text("estimate:\n  tshirt: XS\n")
    os.utime(sizings / "b.yaml", (1_000_000, 1_000_000))
    os.utime(sizings / "a.yaml", (2_000_000, 2_000_000))
    monkeypatch.setattr(guard, "_session_touched_sizing_files",
                        lambda sid, root: ["state/sizings/a.yaml", "state/sizings/b.yaml"])
    assert guard.check(_payload(repo_root)) is None


def test_touched_sizing_since_deleted_denies_as_unsized(monkeypatch, repo_root):
    _stub_repo_root(monkeypatch, repo_root)
    _stub_not_subagent(monkeypatch)
    _stub_touched(monkeypatch, ["state/sizings/gone.yaml"])
    out = guard.check(_payload(repo_root))
    assert "no sizing" in out["hookSpecificOutput"]["permissionDecisionReason"]
