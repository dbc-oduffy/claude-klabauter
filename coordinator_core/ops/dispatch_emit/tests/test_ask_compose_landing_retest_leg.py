"""The ask commit stage's in-run retest leg hands `retest_targeted` the commit stage's params file, so the
retest writes the verdict record the landing reads and a run whose own diff is clean lands.

An ask run's `_nextAction.params` carries no `task_output_path`; a `--from-task-output` built from it is the
literal `undefined`, the child exits 2 before writing anything, and the landing finds no verdict.
"""

from __future__ import annotations

import json
import re
import shlex

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

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INV_FILE = "coordinator_core/session/tests/test_machinery_paths.py"
INV = f"{INV_FILE}::test_every_tracked_state_first_segment_is_on_the_allowlist"
_START = re.compile(r"'python -m coordinator_core\.ops\.dispatch_emit\.retest_targeted ([^']*)'")


def _emitted_retest_argv(root) -> list[str]:
    """The arguments of the retest command `_invariant_retest_call` emits, with the node-side `--tests`
    tail (a JS expression over the refusal's nodes) replaced by the invariant file it yields."""
    call = ask_compose._invariant_retest_call(head="h", anchor="a", repo_root=str(root), run_id=RUN)
    match = _START.search(call)
    assert match is not None, call
    return [*shlex.split(match.group(1)), "--tests", INV_FILE]


def test_the_emitted_retest_command_names_the_commit_stage_params_file_not_a_task_output(tmp_path):
    call = ask_compose._invariant_retest_call(head="h", anchor="a", repo_root=str(tmp_path), run_id=RUN)

    assert f"--params-file {params_path(tmp_path, RUN).as_posix()}" in call
    assert "--from-task-output" not in call and "task_output_path" not in call
    assert "--isolated-export" in call


def test_the_commit_stage_threads_its_repo_root_into_the_retest_leg():
    lines: list = []
    ask_compose._append_docs_and_commit(
        lines, head="h", anchor="a", repo_root="/r", run_id=RUN, manifest_rel="m.json"
    )

    assert f"--params-file {params_path('/r', RUN).as_posix()}" in "\n".join(lines)


def test_a_clean_run_lands_after_the_emitted_retest_command_writes_its_verdict(repo, monkeypatch):
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

    def _child_inline(_engine, _script, child_argv, **_kw):
        return retest_targeted.main(child_argv, run=lambda *a, **k: 0) == 0

    argv = [*_emitted_retest_argv(repo), "--repo-root", str(repo)]
    assert retest_targeted.main(argv, spawn=_child_inline) == 0

    record = json.loads(verdict.read_text(encoding="utf-8"))
    assert record["status"] == "pass" and record["run_id"] == RUN, record

    out = _land(repo, retest_attempts=1)
    assert out["committed"] is True, out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


def test_the_retired_task_output_argument_still_writes_no_record_for_an_unreadable_path(repo):
    argv = ["--from-task-output", "undefined", "--tests", INV_FILE, "--repo-root", str(repo)]

    assert retest_targeted.main(argv, spawn=lambda *a, **k: pytest.fail("must not spawn")) == 2
    assert not landing_retest.fresh_verdict_path(repo, RUN).exists()
