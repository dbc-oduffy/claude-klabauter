"""dispatch.terminal_commit with an empty params payload is refused before the JSON parse.

An empty file (a same-call shell redirect truncated it) used to surface as
"Invalid params_json: Expecting value"; the refusal names the missing params instead and
never reaches the handler. Other ops keep the JSON-parse failure.
"""

from __future__ import annotations

import io
import json

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv

_RUN_ID = "ask-20261021T130141-d21c45"
_CANONICAL = f"scratch/warp/{_RUN_ID}/terminal-commit-params.json"


def _message(stderr: str) -> str:
    return json.loads(stderr.strip())["error"]["message"]


def _assert_refused(stdout: str, stderr: str, exit_code: int) -> str:
    assert exit_code != 0
    assert stdout == ""
    message = _message(stderr)
    for key in ("run_id", "run_token", "script_path"):
        assert key in message
    assert "Invalid params_json" not in message
    return message


@pytest.mark.parametrize("content", ["", "  \n\t\n"])
def test_empty_or_whitespace_params_file_refused(tmp_path, content):
    path = tmp_path / "params.json"
    path.write_text(content, encoding="utf-8")
    out = _dispatch_argv(
        ["dispatch.terminal_commit", "--params-file", str(path)],
        str(tmp_path),
        allow_warm=False,
    )
    message = _assert_refused(*out)
    assert "is empty." not in message


def test_canonical_params_path_adds_the_writer_fact(tmp_path):
    path = tmp_path / _CANONICAL
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    out = _dispatch_argv(
        ["dispatch.terminal_commit", "--params-file", _CANONICAL],
        str(tmp_path),
        allow_warm=False,
    )
    message = _assert_refused(*out)
    assert "is empty." in message


def test_empty_stdin_refused(monkeypatch, tmp_path):
    class _Stdin:
        buffer = io.BytesIO(b"")

    monkeypatch.setattr("sys.stdin", _Stdin(), raising=False)
    out = _dispatch_argv(
        ["dispatch.terminal_commit", "--params-file", "-"],
        str(tmp_path),
        allow_warm=False,
    )
    _assert_refused(*out)


def test_empty_positional_refused(tmp_path):
    out = _dispatch_argv(
        ["dispatch.terminal_commit", "  "], str(tmp_path), allow_warm=False
    )
    _assert_refused(*out)


def test_other_ops_keep_the_json_parse_failure(tmp_path):
    path = tmp_path / "params.json"
    path.write_text("", encoding="utf-8")
    _, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert exit_code != 0
    assert "Invalid params_json" in _message(stderr)


def test_empty_object_and_no_params_refused(tmp_path):
    for argv in (["dispatch.terminal_commit", "{}"], ["dispatch.terminal_commit"]):
        _assert_refused(*_dispatch_argv(argv, str(tmp_path), allow_warm=False))


def test_non_empty_params_pass_the_refusal(tmp_path):
    path = tmp_path / "params.json"
    path.write_text('{"unrelated": 1}', encoding="utf-8")
    _, stderr, _ = _dispatch_argv(
        ["dispatch.terminal_commit", "--params-file", str(path)],
        str(tmp_path),
        allow_warm=False,
    )
    assert "params are empty" not in stderr
