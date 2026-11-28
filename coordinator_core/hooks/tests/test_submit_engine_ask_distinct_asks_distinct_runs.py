"""Back-to-back raw-ask submissions through `submit_engine_ask.evaluate` and the real `dispatch.emit`:
a different ask (text or brief) gets its own run id; the same ask and brief folds onto the first run."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_compose

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


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
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _submit(root: Path, args: dict) -> tuple[str, dict]:
    """(run id, envelope): the premint id of a new run, or the id a fold names; the deferred emit then
    runs as the relay would so the run's token carries its emitted identity."""
    env = mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-distinct",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    updated = hso.get("updatedInput")
    script = Path(updated["scriptPath"]).read_text(encoding="utf-8") if updated else ""
    found = re.search(r"^const PARAMS_FILE = (.*);$", script, re.M)
    if found is None:  # a fold answers with the run it folded into, nothing deferred
        return re.search(r"ask-\d{8}T\d{6}-[0-9a-f]{6}", hso["permissionDecisionReason"]).group(0), env
    literal = found.group(1)
    params = json.loads(Path(json.loads(literal)).read_text(encoding="utf-8"))
    result = mod._default_emit(params, root)
    assert result["run_id"] == params["preminted_run_id"], result
    return result["run_id"], env


def test_two_different_asks_back_to_back_get_distinct_runs(repo) -> None:
    first, _ = _submit(repo, {"ask": "add the widget", "source": "relayed"})
    second, _ = _submit(repo, {"ask": "remove the gadget completely", "source": "relayed"})
    assert first != second


def test_same_ask_text_with_a_different_brief_is_a_different_ask(repo) -> None:
    first, _ = _submit(repo, {"ask": "Execute the brief", "brief": "job one", "source": "relayed"})
    second, _ = _submit(repo, {"ask": "Execute the brief", "brief": "job two, entirely different", "source": "relayed"})
    assert first != second


def test_a_true_duplicate_still_folds_onto_the_first_run(repo) -> None:
    args = {"ask": "Execute the brief", "brief": "job one", "source": "relayed"}
    first, _ = _submit(repo, args)
    again, env = _submit(repo, dict(args))
    assert again == first
    assert f"{first}.workflow.mjs" in env["hookSpecificOutput"]["permissionDecisionReason"]

    plain = {"ask": "add the widget", "source": "relayed"}
    first_plain, _ = _submit(repo, plain)
    again_plain, _ = _submit(repo, {"ask": "Add  the WIDGET", "source": "relayed"})
    assert again_plain == first_plain
