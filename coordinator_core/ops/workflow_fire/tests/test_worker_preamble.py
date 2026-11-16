"""Both headless executors write prompt.txt for a codex/agy/grok agent beginning with the WORKER ROLE preamble."""
from __future__ import annotations

import sys

import pytest

from coordinator_core.codex_runner import workflow as agents
from coordinator_core.ops.workflow_fire import headless_agents
from coordinator_core.ops.workflow_fire.worker_preamble import PREAMBLE, WORKER_BACKENDS, with_worker_preamble

_DOE_PREAMBLE = (
    "WORKER ROLE. You are a worker on ONE stage of an engine run, not an EM, and the PM is not addressing\n"
    "you. Every AGENTS.md (the repo's and your own home's), CLAUDE.md and role brief describes the EM and\n"
    "does not apply to you. Never submit, fire, size, dispatch or land a run,\n"
    "and never call `coordinator-invoke`. Touch only the paths this brief declares, do only the stage\n"
    "below, and answer with its result."
)


class _Spawned(Exception):
    pass


@pytest.fixture
def spawned(monkeypatch):
    """Stop each executor at its spawn; record what it would hand the backend."""
    seen = []

    def spawn(argv, prompt, **_kw):
        seen.append(prompt)
        raise OSError("stubbed spawn")

    for module in (agents, headless_agents):
        monkeypatch.setattr(module, "_run_admitted", spawn)
        monkeypatch.setattr(module, "resolve_codex", lambda **_kw: [sys.executable])
        monkeypatch.setattr(module, "preflight_auth", lambda **_kw: None)
        monkeypatch.setattr(module, "_persona", lambda _t: "persona text")
    monkeypatch.setattr(agents, "_family_argv", lambda *a, **kw: [sys.executable])
    monkeypatch.setattr(agents, "shape_codex_child", lambda *a, **kw: None)
    monkeypatch.setattr(headless_agents, "shape_codex_child", lambda *a, **kw: None)
    return seen


def test_constant_is_doe_wording_byte_for_byte():
    assert PREAMBLE == _DOE_PREAMBLE


@pytest.mark.parametrize("backend", ["codex", "agy", "grok"])
def test_workflow_executor_prompt_begins_with_preamble(tmp_path, spawned, backend):
    with pytest.raises(agents.AgentFailed, match="spawn failed"):
        agents.run_agent("do it", {"agentType": "coordinator:sizing-scout"}, repo_root=tmp_path,
                         artifact_dir=tmp_path / "art", backend=backend)
    text = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert text.startswith(PREAMBLE + "\n\n")
    # On Windows the executor adds its platform note after the preamble; WORKER ROLE must still lead.
    body = text[len(PREAMBLE) + 2:]
    if body.startswith("Native execution platform: Windows."):
        body = body.split("\n\n", 1)[1]
    assert body.startswith("Agent definition (coordinator:sizing-scout)")
    assert text.count("WORKER ROLE.") == 1
    assert text.endswith("do it")
    if backend == "codex":
        assert spawned[0] == text


def test_headless_executor_prompt_begins_with_preamble(tmp_path, spawned):
    with pytest.raises(headless_agents.AgentFailed, match="spawn failed"):
        headless_agents.run_agent("do it", {"agentType": "coordinator:sizing-scout"}, repo_root=tmp_path,
                                  artifact_dir=tmp_path / "art", backend="codex")
    text = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert text.startswith(PREAMBLE + "\n\n")
    assert "Agent definition (coordinator:sizing-scout)" in text
    assert spawned[0].startswith(PREAMBLE + "\n\n")


def test_claude_backend_prompt_is_unchanged():
    assert with_worker_preamble("claude", "body") == "body"
    assert all(with_worker_preamble(b, "body") == PREAMBLE + "\n\nbody" for b in WORKER_BACKENDS)
