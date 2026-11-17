"""The ask commit stage's in-run retest leg hands `retest_targeted` the commit stage's params file, so the
retest writes the verdict record the landing reads and a run whose own diff is clean lands.

An ask run's `_nextAction.params` carries no `task_output_path`; a `--from-task-output` built from it is the
literal `undefined`, the child exits 2 before writing anything, and the landing finds no verdict.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, landing_retest, retest_targeted
from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401
    RUN,
    SCRIPT,
    _author,
    _declare,
    _git,
    _land,
    _write,
    repo,
)
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INV_FILE = "coordinator_core/session/tests/test_machinery_paths.py"
INV = f"{INV_FILE}::test_every_tracked_state_first_segment_is_on_the_allowlist"
_START = re.compile(r'"[^"]*-m coordinator_core\.ops\.dispatch_emit\.retest_targeted ([^"]*)"')


def _emitted_retest_argv(root) -> list[str]:
    """The arguments of the retest command `_invariant_retest_call` emits, with the node-side `--tests`
    tail (a JS expression over the refusal's nodes) replaced by the invariant file it yields."""
    call = ask_compose._invariant_retest_call(head="h", anchor="a", repo_root=str(root), run_id=RUN)
    match = _START.search(call)
    assert match is not None, call
    return [*shlex.split(match.group(1)), "--tests", INV_FILE]


def test_the_emitted_retest_command_names_the_commit_stage_params_file_not_a_task_output(tmp_path: Path) -> None:
    call = ask_compose._invariant_retest_call(head="h", anchor="a", repo_root=str(tmp_path), run_id=RUN)

    assert f"--params-file {params_path(tmp_path, RUN).as_posix()}" in call
    assert "--from-task-output" not in call and "task_output_path" not in call
    assert "--isolated-export" in call


def test_the_commit_stage_threads_its_repo_root_into_the_retest_leg() -> None:
    lines: list = []
    ask_compose._append_docs_and_commit(
        lines, head="h", anchor="a", repo_root="/r", run_id=RUN, manifest_rel="m.json"
    )

    assert f"--params-file {params_path('/r', RUN).as_posix()}" in "\n".join(lines)


def test_a_clean_run_lands_after_the_emitted_retest_command_writes_its_verdict(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    monkeypatch.setattr(imp, "invariants_for_paths", lambda paths, root: [INV])
    monkeypatch.setattr(imp, "existing_invariants", lambda root, nodes: list(nodes))
    _write(repo, INV_FILE, "def test_every_tracked_state_first_segment_is_on_the_allowlist():\n    pass\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "invariant"], repo)
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    record = (record_dir(repo, RUN) / "manifest.json").relative_to(repo).as_posix()
    _write(repo, record, json.dumps({"review_declared_paths": ["a.py"]}))

    refused = _land(repo)
    assert refused["committed"] is False and refused["remedy_step"]["nodes"] == [INV], refused

    params_file = params_path(repo, RUN)
    params_file.parent.mkdir(parents=True, exist_ok=True)
    params_file.write_text(
        json.dumps({"run_id": RUN, "run_token": "tok", "script_path": SCRIPT}), encoding="utf-8"
    )
    verdict = landing_retest.fresh_verdict_path(repo, RUN)
    assert not verdict.exists()

    def _child_inline(_engine, _script, child_argv, **_kw) -> bool:
        return retest_targeted.main(child_argv, run=lambda *a, **k: 0) == 0

    argv = [*_emitted_retest_argv(repo), "--repo-root", str(repo)]
    assert retest_targeted.main(argv, spawn=_child_inline) == 0

    record = json.loads(verdict.read_text(encoding="utf-8"))
    assert record["status"] == "pass" and record["run_id"] == RUN, record

    out = _land(repo, retest_attempts=1)
    assert out["committed"] is True, out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


def _commit_stage(repo_root: str = "/r") -> str:
    lines: list = []
    ask_compose._append_docs_and_commit(
        lines, head="h", anchor="a", repo_root=repo_root, run_id=RUN, manifest_rel="m.json"
    )
    return "\n".join(lines)


def _await_loop(stage: str) -> str:
    start = stage.index("while (!_halted && _commit && _commit.awaiting && _awaits < 2) {")
    return stage[start:stage.index("\n    }", start)]


def test_an_awaiting_reply_waits_detached_then_reinvokes_land_run_without_spending_an_attempt() -> None:
    stage = _commit_stage()
    loop = _await_loop(stage)

    assert stage.index("_commit.awaiting") < stage.index("_commit.remedy_step")
    assert "label: 'retest-await'" in loop
    assert "--poll --wait --params-file " + params_path("/r", RUN).as_posix() in loop
    assert "Run it as ONE Bash call with run_in_background: true and no timeout parameter." in loop
    assert "timeout:" not in loop and "sleep 2" not in loop
    assert loop.count("label: 'commit'") == 1 and "await _agent" not in loop
    assert "_retestAttempts = Number" not in loop and "_retestAttempts++" not in loop
    assert "_awaits++" in loop and "let _awaits = 0;" in stage


def test_an_always_awaiting_landing_is_awaited_at_most_twice_then_the_stage_ends_unresolved(tmp_path: Path) -> None:
    """Runs the emitted commit-stage loop under node with an `agent` stub answering `awaiting` forever."""
    stage = _commit_stage()
    start = stage.index("let _retestAttempts = 0;")
    segment = stage[start:stage.index("if (_ownCloseKey && _commit", start)]
    program = "\n".join([
        "const calls = [];",
        "const agent = async (_p, opts) => { calls.push(opts.label); return opts.label === 'commit' ? { awaiting: true } : { status: 'pass' }; };",
        "const _haltOnUsageLimit = (e) => { throw e; };",
        "const _terminalCommitParamsBlock = () => '';",
        "const _halted = false; const _runId = 'r'; const _RUN_TOKEN = 't';",
        "const _nextAction = { params: { script_path: 's' } }; const _incompleteChunks = [];",
        "const _blockedChunks = []; const _testResult = null; const _docs = null;",
        "(async () => { let _commit = null;",
        segment,
        "console.log(JSON.stringify({ calls, awaiting: _commit && _commit.awaiting === true, refused: _commit && _commit.refused === true })); })();",
    ])
    script = tmp_path / "stage.mjs"
    script.write_text(program, encoding="utf-8")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30, cwd=tmp_path)

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["calls"] == ["commit", "retest-await", "commit", "retest-await", "commit"], out
    assert out["awaiting"] is True and out["refused"] is False, out


def test_a_refusal_with_a_remedy_step_still_takes_the_remedy_loop_not_the_await_loop() -> None:
    stage = _commit_stage()
    remedy_loop = stage[stage.index("_commit.refused && _commit.remedy_step"):]

    assert "label: 'retest-invariant'" in remedy_loop
    assert "_retestAttempts = Number(_rs.attempt)" in remedy_loop
    assert "label: 'retest-await'" not in remedy_loop.split("_ownCloseKey && _commit")[0]


def test_the_invariant_retest_poll_is_one_background_wait_call_not_a_sleep_loop(tmp_path: Path) -> None:
    call = ask_compose._invariant_retest_call(head="h", anchor="a", repo_root=str(tmp_path), run_id=RUN)

    assert "--poll --wait" in call
    assert "run_in_background: true and no timeout parameter" in call
    assert "sleep 2" not in call


def test_the_commit_reply_schema_carries_the_awaiting_block() -> None:
    assert "awaiting" in ask_compose._COMMIT_SCHEMA["properties"]
    assert "awaiting," in _commit_stage_prompt_text()


def _commit_stage_prompt_text() -> str:
    return ask_compose._commit_stage_call(head="h", anchor="a", repo_root="/r", run_id=RUN)


def test_the_retired_task_output_argument_still_writes_no_record_for_an_unreadable_path(repo) -> None:
    argv = ["--from-task-output", "undefined", "--tests", INV_FILE, "--repo-root", str(repo)]

    assert retest_targeted.main(argv, spawn=lambda *a, **k: pytest.fail("must not spawn")) == 2
    assert not landing_retest.fresh_verdict_path(repo, RUN).exists()


def test_wait_without_poll_is_refused_not_ignored() -> None:
    with pytest.raises(SystemExit) as exc:
        retest_targeted.main(["--wait", "--params-file", "x"])

    assert exc.value.code == 2
