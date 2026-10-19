"""A missing canonical terminal-commit params file fails invoke naming its writer stage."""

from __future__ import annotations

import json

from coordinator_core.invoke.__main__ import _dispatch_argv


def _message(stderr: str) -> str:
    return json.loads(stderr.strip())["error"]["message"]


def test_missing_canonical_path_names_the_writer(tmp_path):
    path = tmp_path / "scratch" / "warp" / "ask-1" / "terminal-commit-params.json"
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 1
    assert stdout == ""
    message = _message(stderr)
    assert "ask-1" in message
    assert "block_subagent_commit" in message
    assert "Not dispatched" in message
    assert "Cannot read" not in message


def test_missing_arbitrary_path_keeps_the_existing_message(tmp_path):
    path = tmp_path / "other" / "params.json"
    _, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 1
    message = _message(stderr)
    assert message.startswith("Cannot read --params-file")
    assert "block_subagent_commit" not in message


def test_present_canonical_file_still_dispatches(tmp_path):
    path = tmp_path / "scratch" / "warp" / "ask-1" / "terminal-commit-params.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True
