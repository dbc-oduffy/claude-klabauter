"""A halt-commit's halted_by params holding both quote kinds reach dispatch.land_run byte-identical via the params file."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.win_portability import no_console_creationflags

_RUN_ID = "warp-2026-11-14-5c7d8f-1-dense-registration-gate-20261114T135839-31100a"
_APPLIED = (
    "edited it's \"unmatched\" quote; ran `echo \"a'b\"` and $(date) with \\\"escaped\\\" \\n and a line sep, "
    "plus -----BEGIN TERMINAL-COMMIT PARAMS----- quoted"
)
_HALTED = {
    "halted": "review_blocked",
    "self_correction": {"attempts": 1, "applied": [_APPLIED, 'say "hi" and \'bye\''], "note": "it's fine"},
}


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    return node


def _composed_prompt(repo_root: Path, halted: dict) -> str:
    """The halt-commit agent's prompt as the composed JS builds it, indented as the harness indents computed text."""
    fn = ask_compose._halt_record_commit_fn(
        head="H", anchor="A", script_path="scratch/warp/x.mjs", repo_root=str(repo_root), run_id=_RUN_ID
    )
    prog = (
        f"{chan.PARAMS_BLOCK_JS}\n"
        "const _runId = " + json.dumps(_RUN_ID) + ", _RUN_TOKEN = 'tok', _consumedReturns = [], _planRel = null;\n"
        "const _nextAction = null, _testResult = null;\n"
        "const _halted = " + json.dumps(halted) + ";\n"
        "const _refireNextAction = () => {};\n"
        "const agent = async (prompt) => prompt;\n"
        f"{fn}\n"
        "_commitHaltRecord().then((p) => process.stdout.write(Buffer.from(p, 'utf8')));\n"
    )
    out = subprocess.run(
        [_node(), "-"], input=prog.encode("utf-8"), capture_output=True, timeout=30, check=True, **no_console_creationflags()
    )
    return "\n".join("    " + ln for ln in out.stdout.decode("utf-8").split("\n"))


def test_halt_commit_prompt_runs_a_bare_params_file_command_and_never_inlines_params(tmp_path):
    prompt = _composed_prompt(tmp_path, _HALTED)
    params_file = chan.params_path(tmp_path, _RUN_ID)
    cmd_lines = [ln.strip() for ln in prompt.split("\n") if ln.strip().startswith("coordinator-invoke ")]
    assert cmd_lines == [
        f"coordinator-invoke dispatch.land_run --params-file {params_file.as_posix()} --repo {tmp_path.as_posix()}"
    ]
    assert ask_compose._PARAMS_FILE_ONLY_CLAUSE.strip() in " ".join(prompt.split())
    outside_block = prompt.split(chan.BLOCK_OPEN)[0] + prompt.split(chan.BLOCK_CLOSE)[1]
    assert "self_correction" not in outside_block and _APPLIED not in outside_block


def test_params_with_both_quote_kinds_reach_the_params_file_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(run_token, "halt_admits", lambda *_a: True)
    prompt = _composed_prompt(tmp_path, _HALTED)

    written, leg = chan.prewrite_for_spawn(tmp_path, prompt)

    assert leg is None and written == chan.params_path(tmp_path, _RUN_ID)
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk["halted_by"] == _HALTED
    assert on_disk["halted_by"]["self_correction"]["applied"][0] == _APPLIED
    assert "'" in _APPLIED and '"' in _APPLIED


def test_prompt_params_reads_back_a_halted_by_with_both_quote_kinds_from_the_python_block():
    params = {"run_id": _RUN_ID, "run_token": "t", "script_path": "s", "halted_by": _HALTED}
    assert chan.prompt_params(chan.render_params_block(params)) == params
    assert chan.prompt_params("\n".join("  " + ln for ln in chan.render_params_block(params).split("\n"))) == params


def test_commit_stage_prompt_carries_the_params_file_only_clause():
    call = ask_compose._commit_stage_call(head="h", anchor="a", repo_root="/mnt/r", run_id="run-1")
    assert ask_compose._PARAMS_FILE_ONLY_CLAUSE.strip() in " ".join(call.split())
