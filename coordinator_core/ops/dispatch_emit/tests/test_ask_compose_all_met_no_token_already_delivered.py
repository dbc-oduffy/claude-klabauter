"""An all-met, nothing-staged run (empty prep, every blocking row met by a landed commit) with no run
token ends already_delivered naming that sha; any other no-token terminal commit still halts."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file

_SHA = "19b6d7cd03"


def _in_run_lines() -> str:
    lines: list = []
    ask_compose._append_docs_and_commit(
        lines, head="h", anchor="a", repo_root="/r", run_id="ask-20261110T071829-ab7f5a", manifest_rel="m.json"
    )
    return "\n".join(lines)


def _run(met_by: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        "(async () => {\n"
        "let _halted = null; const _closeable = true; const _runId = 'r'; const _sizingRel = 's';"
        "let _nextAction = { kind: 'terminal_commit', params: {} }; let _commit = null; const _RUN_TOKEN = null;"
        f"let _alreadyDelivered = null; {met_by}\n"
        f"{_in_run_lines()}\n"
        "console.log(JSON.stringify({ halted: _halted, commit: _commit, delivered: _alreadyDelivered, next: _nextAction }));\n"
        "})();"
    )
    out = subprocess.run(
        [node, "-e", "eval(require('fs').readFileSync(process.env.PROG_FILE,'utf8'))"],
        env={**__import__("os").environ, "PROG_FILE": payload_file(program)},
        capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


@pytest.mark.spawns_process
def test_all_met_nothing_staged_without_a_token_ends_already_delivered_naming_the_sha() -> None:
    out = _run(f"let _metBySkipped = {{ shas: ['{_SHA}'] }};")
    assert out["halted"] is None and out["commit"] is None
    assert out["delivered"]["sha"] == _SHA
    assert out["next"]["kind"] == "none"
    assert out["next"]["params"]["already_delivered"]["sha"] == _SHA


@pytest.mark.spawns_process
@pytest.mark.parametrize("met_by", ["let _metBySkipped = null;", "let _metBySkipped = { shas: [] };"])
def test_a_run_not_met_by_a_landed_commit_still_halts_at_commit_without_a_token(met_by: str) -> None:
    out = _run(met_by)
    assert out["delivered"] is None
    assert out["halted"]["halted"] == "commit"
    assert out["halted"]["reason"] == ask_compose.NO_RUN_TOKEN_COMMIT_REASON
