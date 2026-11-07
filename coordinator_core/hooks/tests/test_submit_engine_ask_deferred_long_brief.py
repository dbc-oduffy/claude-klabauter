"""A deferred emit with a long `em_context` brief reaches `dispatch.emit` with accepted keys only.

Drives `submit_engine_ask.evaluate` down the deferred path (the emit outruns the hook budget) and reads the written script.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_brief, op
from coordinator_core.ops.dispatch_emit.request_validation import Field


import pytest

pytestmark = pytest.mark.complete


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _defer(root: Path, args: dict, monkeypatch) -> str:
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }
    try:
        env = mod.evaluate(payload, emit=lambda params, r: release.wait(30) and {})
    finally:
        release.set()
        for worker in threading.enumerate()[1:]:
            worker.join(10)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    (script,) = (root / "scratch" / "warp").glob("defer-*.workflow.mjs")
    return script.read_text(encoding="utf-8")


def _const(text: str, name: str):
    literal = re.search(rf"^const {name} = (.*);$", text, re.M).group(1)
    return json.loads(literal)


def _emit_params(text: str) -> dict:
    """The params the relay's emit command reads: the file its `--params-file` names."""
    return json.loads(Path(_const(text, "PARAMS_FILE")).read_text(encoding="utf-8"))


def _emit_keys(text: str, root: Path) -> set:
    return set(_emit_params(text))


def _accepted() -> set:
    return {f.name for f in op._PARAM_FIELDS if isinstance(f, Field)} | {"ask", "session_id"}


def test_long_brief_defer_emits_accepted_keys_only(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    brief = "context line. " * (mod.DEFERRED_BRIEF_INLINE_MAX // 4)
    text = _defer(root, {"ask": "build it", "source": "relayed", "em_context": brief}, monkeypatch)

    keys = _emit_keys(text, root)
    assert keys <= _accepted(), keys - _accepted()
    assert "ask_brief_file" in keys and "ask_brief" not in keys
    assert "context line." not in text
    params = _emit_params(text)
    spilled = ask_brief.load_em_brief(root, brief=None, brief_file=params["ask_brief_file"])
    assert "context line." in spilled.text


def test_mid_length_brief_spills_and_emits_accepted_keys_only(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    brief = "x" * 1500
    assert mod.DEFERRED_BRIEF_INLINE_MAX < len(brief) < 2000
    text = _defer(root, {"ask": "build it", "source": "relayed", "em_context": brief}, monkeypatch)

    keys = _emit_keys(text, root)
    assert keys <= _accepted(), keys - _accepted()
    assert "ask_brief_file" in keys and "ask_brief" not in keys
    assert brief not in text


def test_params_ride_a_params_file_the_command_names(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    ask = "build the thing"
    text = _defer(root, {"ask": ask, "source": "relayed", "em_context": "x"}, monkeypatch)

    params_file = _const(text, "PARAMS_FILE")
    assert params_file and Path(params_file).is_file()
    assert _emit_params(text)["ask"] == ask
    assert "--params-file" in text and "const PARAMS =" not in text
    assert _emit_keys(text, root) <= _accepted()
