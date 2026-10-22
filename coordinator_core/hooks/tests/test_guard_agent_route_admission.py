"""guard_agent_route_admission: deny an Agent dispatch whose subagent_type lacks the work class's tools."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import machine_profile
from coordinator_core.hooks import guard_agent_route_admission as guard

UE = "example-game-repo-control:ue-editor"
TRIAGE = "example-game-repo-control:ue-triage"
EXECUTOR = "coordinator:executor"
NEED = "mcp__example_game_repo-control__"


def _table(on_mismatch: str = "refuse") -> dict:
    return {
        "schema_version": 1,
        "default_agent_type": EXECUTOR,
        "work_classes": [
            {
                "id": "ue-editor",
                "match": {"keywords": ["unreal"], "paths": ["Content/*"]},
                "agent_type": UE,
                "triage_agent_type": TRIAGE,
                "required_tools": [NEED],
                "on_mismatch": on_mismatch,
            }
        ],
        "agent_tool_ranges": {
            UE: [NEED + "execute_python_code"],
            TRIAGE: ["Read"],
            EXECUTOR: ["Read", "Edit", "Write", "Bash"],
        },
    }


@pytest.fixture
def repo(tmp_path):
    def make(table=None) -> str:
        (tmp_path / ".claude").mkdir(exist_ok=True)
        if table is not None:
            (tmp_path / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
        return str(tmp_path)

    return make


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "strict")


def _payload(cwd: str, prompt: str, subagent_type=EXECUTOR, tool_name="Agent") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
        "cwd": cwd,
    }


def _reason(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


def test_no_table_admits(repo):
    assert guard.evaluate(_payload(repo(), "import the unreal meshes")) == {}


def test_non_agent_tool_admits(repo):
    assert guard.evaluate(_payload(repo(_table()), "unreal work", tool_name="Bash")) == {}


def test_covering_subagent_type_admits(repo):
    assert guard.evaluate(_payload(repo(_table()), "import the unreal meshes", UE)) == {}


def test_unmatched_prompt_admits(repo):
    assert guard.evaluate(_payload(repo(_table()), "fix the changelog typo")) == {}


def test_refuse_names_class_and_agent_type(repo):
    reason = _reason(guard.evaluate(_payload(repo(_table("refuse")), "import the unreal meshes")))
    assert "ue-editor" in reason
    assert "example-game-repo" not in reason
    assert reason.endswith("Dispatch its agent_type.")


def test_redirect_wording(repo):
    reason = _reason(guard.evaluate(_payload(repo(_table("redirect")), "import the unreal meshes")))
    assert reason.endswith("Dispatch its agent_type instead.")


def test_path_token_in_prompt_matches_class(repo):
    result = guard.evaluate(_payload(repo(_table()), "Edit Content/Maps/Level1.umap now."))
    assert "ue-editor" in _reason(result)


def test_triage_agent_without_required_tools_admits(repo):
    assert guard.evaluate(_payload(repo(_table()), "unreal assessment", TRIAGE)) == {}


def test_omitted_subagent_type_is_resolved_not_skipped(repo):
    payload = _payload(repo(_table()), "import the unreal meshes")
    del payload["tool_input"]["subagent_type"]
    assert "ue-editor" in _reason(guard.evaluate(payload))


def test_malformed_table_fails_open(repo):
    bad = _table()
    bad["work_classes"][0]["on_mismatch"] = "explode"
    assert guard.evaluate(_payload(repo(bad), "import the unreal meshes")) == {}


def test_unparsable_table_fails_open(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "route-table.json").write_text("{not json", encoding="utf-8")
    assert guard.evaluate(_payload(str(tmp_path), "unreal")) == {}


def test_warn_level_is_an_advisory(repo, monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "warn")
    result = guard.evaluate(_payload(repo(_table()), "import the unreal meshes"))
    hso = result["hookSpecificOutput"]
    assert hso.get("permissionDecision") != "deny"
    assert "ue-editor" in hso["additionalContext"]


def test_no_updated_input_rewrite(repo):
    result = guard.evaluate(_payload(repo(_table("redirect")), "import the unreal meshes"))
    assert "updatedInput" not in result["hookSpecificOutput"]


def test_repo_root_arg_overrides_cwd(repo, tmp_path_factory):
    elsewhere = str(tmp_path_factory.mktemp("elsewhere"))
    result = guard.evaluate(_payload(elsewhere, "import the unreal meshes"), repo_root=repo(_table()))
    assert "ue-editor" in _reason(result)


def test_handler_delegates(repo):
    result = guard._handler(_payload(repo(_table()), "import the unreal meshes"))
    assert "ue-editor" in _reason(result)


def test_no_subprocess(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert "ue-editor" in _reason(guard.evaluate(_payload(repo(_table()), "import the unreal meshes")))
