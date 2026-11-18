"""A composed run keeps a plan row met only when its acceptance test command ran green; red, missing or unreported exits demote it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_COMMAND = "python -m pytest pkg/tests/test_x.py -q"
_ROW = {
    "row_id": "C2",
    "register_rows": ["R1"],
    "argv": ["python", "-m", "pytest", "pkg/tests/test_x.py", "-q"],
    "command": _COMMAND,
}
_PLAN = """\
---
title: p
---

# Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: t
  register_rows: ["R1"]
  acceptance: "python -m pytest pkg/tests/test_x.py -q exits 0"
- id: C2
  title: prose
  register_rows: ["R1"]
  acceptance: "the output reads well"
- id: C3
  title: no register
  acceptance: "python -m pytest pkg/tests/test_y.py -q exits 0"
```
"""


def _compose(rows: list) -> str:
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ask_compose, "_known_arm", lambda *_: "xs")
        mp.setattr(ask_compose, "_acceptance_rows", lambda *_: rows)
        return compose_ask_script(
            repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
            wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
            **REVIEW_KW,
        )
    finally:
        mp.undo()


def _demotion_line(script: str) -> str:
    match = re.search(r"    if \(_ledger && Array\.isArray\(_ledger\.items\)\) _ledger\.items = _demoteRed\(.*?;\n", script)
    assert match, "the composed script never feeds acceptance exits to _demoteRed"
    return match.group(0)


def _node() -> str:
    node = shutil.which("node")
    assert node is not None, "node is required: the demotion is JS and a skip would pass the falsifier"
    return node


def _demote(report: str, *, items: list | None = None) -> list:
    """Run the composed decls and demotion line with the acceptance leg stubbed to `report` (a JS expression)."""
    decls = ask_compose._acceptance_decls_js([_ROW], "_stubLeg()")
    harness = (
        "const _runId = 'run-1'; const _manifest = { review_declared_paths: [] }; const _USAGE_LIMIT_RE = /nomatch/;\n"
        "const log = () => {};\n"
        f"const _stubLeg = async () => ({report});\n"
        f"let _ledger = {{ items: {json.dumps(items or [{'id': 'R1', 'status': 'met'}, {'id': 'R2', 'status': 'met'}])} }};\n"
        f"{decls}\n"
        "(async () => {\n"
        "  await _runAcceptance();\n"
        f"{_demotion_line(_compose([_ROW]))}"
        "  process.stdout.write(JSON.stringify(_ledger.items));\n"
        "})();\n"
    )
    out = subprocess.run(
        [_node(), "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


def _by_id(items: list) -> dict:
    return {i["id"]: i for i in items}


def test_a_red_acceptance_exit_demotes_the_row_it_names_and_only_that_row() -> None:
    items = _by_id(_demote("{ results: [{ row_id: 'C2', exit: 1 }] }"))
    assert items["R1"]["status"] == "not_met"
    assert items["R1"]["remaining"] == [f"acceptance exit 1: {_COMMAND}"]
    assert items["R2"]["status"] == "met"


def test_a_green_acceptance_exit_leaves_the_row_met() -> None:
    items = _by_id(_demote("{ results: [{ row_id: 'C2', exit: 0 }] }"))
    assert items["R1"]["status"] == "met"
    assert "remaining" not in items["R1"]


@pytest.mark.parametrize(
    "report",
    [
        "{ results: [] }",
        "{ results: [{ row_id: 'C2' }] }",
        "{ results: [{ row_id: 'C2', exit: null }] }",
        "{ results: [{ row_id: 'C2', exit: '0' }] }",
        "{ }",
        "null",
        "(() => { throw new Error('agent died'); })()",
    ],
)
def test_a_missing_or_unreported_exit_fails_closed(report: str) -> None:
    items = _by_id(_demote(report))
    assert items["R1"]["status"] == "not_met"
    assert items["R1"]["remaining"] == [f"acceptance exit unreported: {_COMMAND}"]


def test_a_row_the_judge_did_not_call_met_is_left_alone() -> None:
    items = _by_id(_demote("{ results: [{ row_id: 'C2', exit: 1 }] }", items=[{"id": "R1", "status": "not_met"}]))
    assert items["R1"] == {"id": "R1", "status": "not_met"}


def test_the_default_compose_runs_the_acceptance_leg_before_the_verify_agent_and_demotes_after_it() -> None:
    script = _compose([_ROW])
    assert ask_compose.VERIFY_ACCEPTANCE_LABEL in script
    assert '"argv":["python","-m","pytest","pkg/tests/test_x.py","-q"]' in script
    run_verify = script.index("const _runVerify = async")
    assert run_verify < script.index("await _runAcceptance(); ", run_verify) < script.index("_verify = await ", run_verify)
    assert script.index("_demoteRed = ") < script.index("_ledger.items = _demoteRed(")


def test_a_plan_with_no_test_command_rows_composes_no_acceptance_machinery() -> None:
    script = _compose([])
    for token in ("_ACCEPT_ROWS", "_runAcceptance", "_demoteRed", ask_compose.VERIFY_ACCEPTANCE_LABEL):
        assert token not in script


def test_acceptance_rows_reads_the_plan_spine_and_skips_prose_and_unregistered_rows(tmp_path, monkeypatch) -> None:
    (tmp_path / "plan.md").write_text(_PLAN, encoding="utf-8")
    monkeypatch.setattr(ask_compose, "_static_plan_rel", lambda *_: "plan.md")
    rows = ask_compose._acceptance_rows(str(tmp_path), "state/sizings/x.yaml")
    assert rows == [
        {
            "row_id": "C1",
            "register_rows": ["R1"],
            "argv": ["python", "-m", "pytest", "pkg/tests/test_x.py", "-q"],
            "command": _COMMAND,
        }
    ]


def test_acceptance_rows_is_empty_without_a_plan(monkeypatch) -> None:
    monkeypatch.setattr(ask_compose, "_static_plan_rel", lambda *_: None)
    assert ask_compose._acceptance_rows("REPO", "state/sizings/x.yaml") == []
