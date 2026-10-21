"""`--from-task-output PATH` maps onto dispatch.terminal_commit's task_output_path param."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.invoke.__main__ import _build_arg_parser, _dispatch_argv


@pytest.fixture
def captured(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    seen: list[dict] = []

    async def _fake(msg, caller=None):
        seen.append(msg["params"])
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    monkeypatch.setattr("coordinator_core.invoke.dispatch.dispatch_message", _fake)
    return seen


def test_parser_accepts_the_flag():
    args = _build_arg_parser().parse_intermixed_args(
        ["dispatch.terminal_commit", "--from-task-output", "out.json"]
    )
    assert args.from_task_output == "out.json"


def test_params_carry_the_absolute_path_resolved_against_cwd(captured, tmp_path):
    _, stderr, code = _dispatch_argv(
        ["dispatch.terminal_commit", "--from-task-output", "out.json"],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 0, stderr
    assert captured == [{"task_output_path": str((tmp_path / "out.json").resolve())}]
    assert Path(captured[0]["task_output_path"]).is_absolute()


def test_the_flag_with_empty_object_positional_params_is_admitted(captured, tmp_path):
    _, stderr, code = _dispatch_argv(
        ["dispatch.terminal_commit", "{}", "--from-task-output", "out.json"],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 0, stderr
    assert captured == [{"task_output_path": str((tmp_path / "out.json").resolve())}]


def test_a_matching_positional_task_output_path_is_accepted(captured, tmp_path):
    abs_path = str((tmp_path / "out.json").resolve())
    import json

    _, stderr, code = _dispatch_argv(
        ["dispatch.terminal_commit", json.dumps({"task_output_path": abs_path, "k": 1}),
         "--from-task-output", abs_path],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 0, stderr
    assert captured == [{"task_output_path": abs_path, "k": 1}]


def test_land_run_takes_the_flag_and_carries_the_absolute_path(captured, tmp_path):
    _, stderr, code = _dispatch_argv(
        ["dispatch.land_run", "--from-task-output", "out.json"],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 0, stderr
    assert captured == [{"task_output_path": str((tmp_path / "out.json").resolve())}]


def test_land_run_conflicting_task_output_path_is_refused(captured, tmp_path):
    _, stderr, code = _dispatch_argv(
        ["dispatch.land_run", '{"task_output_path": "other.json"}',
         "--from-task-output", "out.json"],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 2
    assert "conflicts" in stderr
    assert captured == []


def test_another_op_is_a_usage_error(captured, tmp_path):
    _, stderr, code = _dispatch_argv(
        ["ping", "--from-task-output", "out.json"], str(tmp_path), allow_warm=False
    )
    assert code == 2
    assert "dispatch.terminal_commit" in stderr
    assert captured == []


def test_a_conflicting_positional_task_output_path_is_refused(captured, tmp_path):
    _, stderr, code = _dispatch_argv(
        ["dispatch.terminal_commit", '{"task_output_path": "other.json"}',
         "--from-task-output", "out.json"],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 2
    assert "conflicts" in stderr
    assert captured == []
