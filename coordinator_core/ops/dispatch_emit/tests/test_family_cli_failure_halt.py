"""A family CLI auth or launch failure on a write row halts the run `family_cli_failure`, quoting the stderr line.

The emitted `_familyRun` runs in node against a stubbed family-stage-runner reply."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, halt_remedy_class, stage_family
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_NODE = shutil.which("node")
_LINE = "Failed to refresh token: refresh token was already used"
_FAMILY = {"family": "codex", "stage": "write", "exit_code": 1, "output_path": "", "verdict": "FAILED",
           "blocked_reason": f"codex exited 1: {_LINE}", "family_authored": False,
           "cli_failure": {"kind": "auth", "stderr_line": _LINE}}
_WRITE_ROW = {"id": "C1", "family": "codex", "stage_role": "write", "writes": ["pkg/a.py"], "fallback_brief_path": "b.md",
              "agent_type": "a", "fallback_agent_type": "b", "model": "m"}
_READ_ROW = {**_WRITE_ROW, "stage_role": "research"}


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "load_sizing", lambda root, rel: {"stage_families": {"review": "codex"}})
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="sizing.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", **REVIEW_KW,
    )


def _run(monkeypatch, tmp_path, row: dict, family_reply: dict, *, halted: str = "null") -> dict:
    """Execute `_familyRun` in node; returns the status line, the final `_halted` and the agent() call count."""
    script = _script(monkeypatch)
    body = script[script.index("  const _FAMILY_RESULT_SCHEMA = ") : script.index("\n  }\n", script.index("  async function _familyRun(r) {")) + 4]
    prog = (
        "const _rowPrompt = () => 'P'; const log = () => {}; const _famOk = {}; const _runId = 'run-1'; const _planRel = 'docs/plans/p.md';\n"
        f"let _halted = {halted};\n"
        f"const _replies = [{json.dumps(family_reply)}, 'DONE: C1\\nStatus: DONE']; let _n = 0;\n"
        "async function agent() { return _replies[_n++]; }\n"
        f"{body}\n_familyRun({json.dumps(row)}).then((head) => console.log(JSON.stringify({{ head, halted: _halted, calls: _n }})));\n"
    )
    path = tmp_path / "family.js"
    path.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        [_NODE, str(path)], capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    return json.loads(out)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("kind", ["auth", "launch", "exit"])
def test_write_row_cli_failure_halts_with_the_named_kind_quoting_the_stderr_line(monkeypatch, tmp_path, kind):
    reply = {**_FAMILY, "cli_failure": {"kind": kind, "stderr_line": _LINE}}
    out = _run(monkeypatch, tmp_path, _WRITE_ROW, reply)
    halted = out["halted"]
    assert halted["halted"] == ask_compose.HALT_FAMILY_CLI_FAILURE == "family_cli_failure"
    assert halted["halted"] != "review_floor"
    assert _LINE in halted["reason"] and f"codex CLI {kind} failure" in halted["reason"]
    assert halted["run_id"] == "run-1" and halted["plan"] == "docs/plans/p.md"
    assert out["head"].startswith("BLOCKED: C1 via codex") and "no claude fallback" in out["head"] and out["calls"] == 1


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_write_row_failure_without_cli_failure_sets_no_halt(monkeypatch, tmp_path):
    reply = {k: v for k, v in _FAMILY.items() if k != "cli_failure"}
    assert _run(monkeypatch, tmp_path, _WRITE_ROW, reply)["halted"] is None


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_an_earlier_halt_is_never_overwritten(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, _WRITE_ROW, _FAMILY, halted="{ halted: 'usage_limit' }")
    assert out["halted"] == {"halted": "usage_limit"}


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_read_only_row_with_cli_failure_still_falls_back_to_claude_and_sets_no_halt(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, _READ_ROW, {**_FAMILY, "stage": "research"})
    assert out["halted"] is None
    assert out["head"].startswith("DONE: C1 via claude fallback after codex FAILED") and out["calls"] == 2


def test_result_schema_declares_cli_failure_so_the_runner_reply_keeps_it(monkeypatch):
    schema = json.loads(re.search(r"_FAMILY_RESULT_SCHEMA = (\{.*\});", _script(monkeypatch)).group(1))
    cli = schema["properties"]["cli_failure"]
    assert cli["properties"]["kind"]["enum"] == ["auth", "launch", "exit"]
    assert cli["required"] == ["kind", "stderr_line"]
    assert "cli_failure" not in schema["required"]
    assert "cli_failure" not in stage_family._RESULT_KEYS


def test_kind_is_registered_as_a_decision_halt_and_an_irreversible_kind():
    assert halt_remedy_class.HALT_REMEDY["family_cli_failure"] == (halt_remedy_class.DECISION, None)
    assert "login renewal" in halt_remedy_class.IRREVERSIBLE_KINDS["family_cli_failure"]


def test_review_floor_still_fires_only_when_no_halt_is_set(monkeypatch):
    assert "if (!_halted && !_alreadyDelivered && _codeReviewOwed" in _script(monkeypatch)
