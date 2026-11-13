"""The grind's commit stage writes its params file before `dispatch.land_run` runs, with no hook leg."""
from __future__ import annotations

import json

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

from .test_grind_compose import _compose
from .test_grind_stages import _terminal_call

RUN = "20261113T030303Z"
SCRIPT = "scratch/warp/g.workflow.mjs"
SESSION = "sess-prewritten"


def _commit_call(script: str) -> str:
    start = script.index("async function _terminalCommitCall")
    return script[start: script.index("function _counts()", start)]


def test_prompt_orders_the_prewrite_before_the_one_bare_land_run_segment():
    call = _terminal_call(params_file_js="P", params_js="PARAMS", run_id_js="RID", repo_root_js="ROOT")
    pre = call.index("grind-row prewrite-commit-params --repo-root ")
    land = call.index("coordinator-invoke dispatch.land_run --params-file ")
    assert pre < land
    assert "_shq(ROOT)" in call and "--run-id ' + (RID)" in call.replace("' + RID", "' + (RID)")
    assert call.count("coordinator-invoke") == 1
    assert "If command 1 exits non-zero, still run command 2" in call


def test_composed_grind_script_carries_the_prewrite_in_the_commit_call_only():
    script = _compose()
    call = _commit_call(script)
    assert "grind-row prewrite-commit-params" in call
    assert call.index("prewrite-commit-params") < call.index("coordinator-invoke dispatch.land_run")
    assert script.count("prewrite-commit-params") == call.count("prewrite-commit-params")
    assert "record-fix-paths" not in script


def _spawn_with_block(tmp_path, monkeypatch, params):
    home = tmp_path / "home"
    wf = home / "projects" / "p" / SESSION / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    prompt = "\n".join("  " + ln for ln in ["Commit.", *chan.render_params_block(params).split("\n")])
    (wf / "agent-1.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


def _params(repo, token):
    row = {"row_id": "r1", "outcome": "settled", "paths": ["lesson.md", "undeclared/README.md"], "deleted_paths": []}
    return {
        "run_id": RUN, "run_token": token, "script_path": SCRIPT,
        "grind": {"profile": "lessons", "rows": [row], "halted": None},
    }


def test_params_file_exists_at_the_canonical_path_before_any_hook_leg(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    params = _params(repo, run_token.mint(repo, RUN, SCRIPT))
    _spawn_with_block(tmp_path, monkeypatch, params)
    target = chan.params_path(repo, RUN)
    assert not target.exists()
    code = grind_rows.main(["prewrite-commit-params", "--repo-root", str(repo), "--run-id", RUN])
    assert code == grind_rows.EXIT_OK
    assert target == repo / RUN_DIR_ROOT / RUN / chan.PARAMS_FILE_NAME
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["grind"]["rows"][0]["paths"] == ["lesson.md", "undeclared/README.md"]
    assert chan.missing_file_diagnosis(str(target)) is None


def test_a_forged_token_leaves_no_params_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run_token.mint(repo, RUN, SCRIPT)
    _spawn_with_block(tmp_path, monkeypatch, _params(repo, "forged"))
    code = grind_rows.main(["prewrite-commit-params", "--repo-root", str(repo), "--run-id", RUN])
    assert code == grind_rows.EXIT_REFUSAL
    assert not chan.params_path(repo, RUN).exists()
