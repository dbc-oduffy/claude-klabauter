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


def test_missing_canonical_path_without_a_note_says_the_writer_was_not_reached(tmp_path):
    path = tmp_path / "scratch" / "warp" / "ask-1" / "terminal-commit-params.json"
    _, stderr, _ = _dispatch_argv(["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False)
    message = _message(stderr)
    assert "run-token writer was not reached" in message
    assert "never written" in message and "did not run or refused" in message


def test_missing_canonical_path_with_a_note_names_the_refusing_leg(tmp_path):
    path = tmp_path / "scratch" / "warp" / "ask-1" / "terminal-commit-params.json"
    path.parent.mkdir(parents=True)
    path.with_name("terminal-commit-params.refused").write_text("some-leg", encoding="utf-8")
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    text = chan.missing_file_diagnosis(str(path))
    assert "some-leg" in text and "was not reached" not in text


def test_missing_arbitrary_path_keeps_the_existing_message(tmp_path):
    path = tmp_path / "other" / "params.json"
    _, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 1
    message = _message(stderr)
    assert message.startswith("Cannot read --params-file")
    assert "block_subagent_commit" not in message


_REL = "scratch/warp/ask-1/terminal-commit-params.json"


def test_relative_path_from_below_repo_root_reads_the_existing_file(tmp_path):
    """Hypothesis (a) held: the caller's cwd sits below the repo root, so the typed relative
    path misses under cwd while the canonical file exists at the root."""
    (tmp_path / _REL).parent.mkdir(parents=True)
    (tmp_path / _REL).write_text("{}", encoding="utf-8")
    deep = tmp_path / "sub" / "deeper"
    deep.mkdir(parents=True)
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", _REL], str(deep), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True


def test_relative_path_absent_everywhere_still_says_never_written(tmp_path):
    deep = tmp_path / "sub"
    deep.mkdir()
    _, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", _REL], str(deep), allow_warm=False
    )
    assert exit_code == 1
    assert "was never written" in _message(stderr)


def _land_params(run_id):
    return {"run_id": run_id, "run_token": "t" * 8, "script_path": "s.mjs", "inline_review": {"k": 1}}


def test_absent_params_file_is_recovered_from_the_land_params(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    target = chan.params_path(tmp_path, "ask-1")
    chan.materialize(target, _land_params("ask-1"))
    target.unlink()
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(target)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert "recovered" in stderr and "ask-1" in stderr
    assert target.is_file()


def test_absent_relative_params_file_is_recovered_against_cwd(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    target = chan.params_path(tmp_path, "ask-1")
    chan.materialize(target, _land_params("ask-1"))
    target.unlink()
    _, stderr, exit_code = _dispatch_argv(["ping", "--params-file", _REL], str(tmp_path), allow_warm=False)
    assert exit_code == 0, stderr
    assert target.is_file()


def test_relative_path_from_below_the_root_is_not_recovered(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    target = chan.params_path(tmp_path, "ask-1")
    chan.materialize(target, _land_params("ask-1"))
    target.unlink()
    deep = tmp_path / "sub"
    deep.mkdir()
    _, stderr, exit_code = _dispatch_argv(["ping", "--params-file", _REL], str(deep), allow_warm=False)
    assert exit_code == 1
    assert "was never written" in _message(stderr)
    assert not target.exists()


def test_missing_params_file_without_land_params_names_the_door_fail_open(tmp_path, monkeypatch):
    import time

    from coordinator_core.ops.dispatch_emit import run_token

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    run_token.mint(repo, "ask-1", "s.mjs")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
    (home / "state" / "door-fail-open.log").write_text(f"ts={stamp} cause=wedged\n", encoding="utf-8")
    _, stderr, exit_code = _dispatch_argv(["ping", "--params-file", _REL], str(repo), allow_warm=False)
    assert exit_code == 1
    message = _message(stderr)
    assert "PreToolUse door failed open" in message and "cause=wedged" in message


def test_present_canonical_file_still_dispatches(tmp_path):
    path = tmp_path / "scratch" / "warp" / "ask-1" / "terminal-commit-params.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    stdout, stderr, exit_code = _dispatch_argv(
        ["ping", "--params-file", str(path)], str(tmp_path), allow_warm=False
    )
    assert exit_code == 0, stderr
    assert json.loads(stdout)["result"]["ok"] is True
