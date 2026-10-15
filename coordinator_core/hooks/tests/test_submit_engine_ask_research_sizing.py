"""A sizing with a `research` block submitted through the hook goes to the research route (`from_sizing`)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as mod

_FIXTURE = Path(__file__).parents[2] / "ops" / "dispatch_emit" / "tests" / "fixtures" / "pipeline_research"
REL = "state/sizings/2026-10-15-research-fixture.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ctx.md").write_text("x", encoding="utf-8")
    return tmp_path


def _put(root: Path, research) -> None:
    doc = {"schema": "sizing-object", "name": "fx", "route": "dispatch"}
    if research is not None:
        doc["research"] = research
    (root / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _payload(root: Path, **extra) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(root),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": REL, **extra}},
    }


def _capture(root: Path):
    seen: list[dict] = []

    def emit(params: dict, r: Path) -> dict:
        seen.append(params)
        return {"path": str(root / "run.js"), "run_id": "r1", "fire_args": {}}

    return seen, emit


def test_research_sizing_emits_from_sizing_without_the_plan_chain_params(repo):
    _put(repo, {"value_class": "scouts", "appetite": "medium"})
    seen, emit = _capture(repo)
    env = mod.evaluate(_payload(repo), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert seen[0]["from_sizing"] == REL
    assert "ask" not in seen[0] and "sizing_path" not in seen[0]


def test_research_sizing_maps_context_paths_to_the_route_context(repo):
    _put(repo, {"value_class": "scouts"})
    seen, emit = _capture(repo)
    mod.evaluate(_payload(repo, context="docs/ctx.md"), emit=emit)
    assert seen[0]["context"] == ["docs/ctx.md"]
    assert "ask_context" not in seen[0]


@pytest.mark.parametrize("extra", [{"brief": "my words"}, {"brief_file": "docs/ctx.md"}, {"context": "free prose here"}])
def test_brief_on_a_research_sizing_is_denied(repo, extra):
    _put(repo, {"value_class": "scouts"})
    seen, emit = _capture(repo)
    env = mod.evaluate(_payload(repo, **extra), emit=emit)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "research sizing takes no brief" in hso["permissionDecisionReason"]
    assert not seen


@pytest.mark.parametrize("research", [None, {}])
def test_sizing_without_a_research_block_keeps_the_plan_chain(repo, research):
    _put(repo, research)
    seen, emit = _capture(repo)
    mod.evaluate(_payload(repo), emit=emit)
    assert seen[0]["ask"] is True
    assert seen[0]["sizing_path"] == REL
    assert "from_sizing" not in seen[0]


def test_research_sizing_real_emit_carries_the_research_route(repo, monkeypatch):
    import coordinator_core.ops.dispatch_emit as pkg
    from coordinator_core.ops.dispatch_emit import op as op_module

    class _Admitted:
        @staticmethod
        def await_admission(start_dir, *, hold_allowed):
            return {"verdict": "disabled"}

    monkeypatch.setattr(pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    (repo / ".git" / "objects").mkdir()
    (repo / ".git" / "refs").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    _put(repo, {"value_class": "scouts", "appetite": "medium", "questions": ["what is x?"]})
    payload = _payload(repo)
    start = time.process_time()
    env = mod.evaluate(payload)
    elapsed = time.process_time() - start
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", hso["permissionDecisionReason"]
    script = Path(hso["updatedInput"]["scriptPath"])
    assert script.is_file()
    assert elapsed < 0.5, elapsed
