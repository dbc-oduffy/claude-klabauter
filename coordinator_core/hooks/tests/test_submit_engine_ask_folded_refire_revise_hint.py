"""A raw-ask refire the emit folds into an unfired run replies naming that run, and `workflow.revise` accepts it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core import warp_fire, warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops.dispatch_emit import ask_compose, requirements_ledger as rl, run_token

SID = "sess-folded-refire"
ASK = "add the widget"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_HINT_ID = re.compile(r'workflow\.revise \'\{"run_id": "([^"]+)"')


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    managed, legacy = tmp_path / "managed", tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    pm_turns.log_path(SID, str(root)).parent.mkdir(parents=True)
    for ask in (ASK, "an unrelated ask"):
        assert pm_turns.append_turn(ask, session_id=SID, cwd=str(root))["ok"]
    return root


def _submit(root: Path, ask: str) -> dict:
    return mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": ask, "source": "pm"}},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })


def _hinted_run(env: dict) -> str:
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    found = _HINT_ID.search(json.dumps(env).replace("\\", ""))
    assert found, env
    return found.group(1)


def _emit_deferred(env: dict, root: Path) -> str:
    """The run the premint-deferred script's relay emits: `dispatch.emit` on the params file the script names."""
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    literal = re.search(r"^const PARAMS_FILE = (.*);$", script.read_text(encoding="utf-8"), re.M).group(1)
    params = json.loads(Path(json.loads(literal)).read_text(encoding="utf-8"))
    return mod._default_emit(params, root)["run_id"]


def test_a_refire_folded_into_an_unfired_run_names_that_run_and_revise_accepts_it(repo) -> None:
    first = _emit_deferred(_submit(repo, ASK), repo)

    named = _hinted_run(_submit(repo, ASK))

    assert named == first
    rl.revise(repo, named, verbatim="also handle the empty widget")


def test_a_first_submission_still_premints_and_names_its_own_run(repo) -> None:
    env = _submit(repo, "an unrelated ask")

    named = _hinted_run(env)

    assert run_token._record_path(repo, named).is_file()
    assert _emit_deferred(env, repo) == named
    rl.revise(repo, named, verbatim="and the rest")


def test_a_different_ask_is_not_folded_into_a_held_run(repo) -> None:
    first = _emit_deferred(_submit(repo, ASK), repo)

    assert _hinted_run(_submit(repo, "an unrelated ask")) != first
