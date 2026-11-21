"""nag_text: full fire_step on a session's first nag, then nothing unless the caller opts into the terse repeat."""

from coordinator_core import warp_fire

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


INVOKE = "coordinator-invoke engine_ask.submit"


def test_advised_command_runs_as_given_with_no_params_file(tmp_path):
    """The advisory's command, argv as emitted, in a repo with no staged params: no missing-file failure."""
    import json
    import shlex
    import subprocess

    from coordinator_core.invoke.__main__ import _dispatch_argv

    # real git: the CLI resolves the repo root through `git rev-parse`, which a hand-built .git dir fools
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for host in ("codex", "agy", "grok"):
        step = warp_fire.fire_step(host=host)
        command = step.split("call `", 1)[1].split("`", 1)[0]
        argv = shlex.split(command)
        assert argv[0] == "coordinator-invoke" and "--params-file" not in argv
        stdout, stderr, _ = _dispatch_argv(argv[1:], str(tmp_path), allow_warm=False)
        assert "Cannot read" not in stderr and "never written" not in stderr and "unreadable" not in stdout
        result = json.loads(stdout)["result"]
        assert result["refused"] == "bad-request" and "No ask staged" in result["reason"]
        staged = tmp_path / ".coordinator-local" / "em-submit.json"
        assert json.loads(staged.read_text(encoding="utf-8")) == {"ask": "", "ask_source": "pm"}
        staged.unlink()
        # the advisory names where the params live, so the refusal is the one step the EM acts on
        assert ".coordinator-local/em-submit.json" in step and "Workflow" not in step
    assert "em-submit" not in warp_fire.fire_step(host="claude")
