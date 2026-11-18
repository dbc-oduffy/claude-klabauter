"""nag_text: full fire_step on a session's first nag, then nothing unless the caller opts into the terse repeat."""

from coordinator_core import warp_fire


def test_first_full_then_nothing_by_default(tmp_path):
    first = warp_fire.nag_text("s1", state_dir=tmp_path)
    assert first == warp_fire.fire_step()
    assert warp_fire.nag_text("s1", state_dir=tmp_path) == ""
    assert warp_fire.nag_text("s1", state_dir=tmp_path) == ""


def test_opted_in_repeat_is_the_terse_counted_line(tmp_path):
    first = warp_fire.nag_text("s1", state_dir=tmp_path, repeat=True)
    assert first == warp_fire.fire_step()
    second = warp_fire.nag_text("s1", state_dir=tmp_path, repeat=True)
    third = warp_fire.nag_text("s1", state_dir=tmp_path, repeat=True)
    assert second.startswith("[engine-ask nag #2]") and third.startswith("[engine-ask nag #3]")
    assert "\n" not in second and len(second) < len(first)
    assert warp_fire.fire_command() in second


def test_sessions_independent_and_sizing_carried(tmp_path):
    warp_fire.nag_text("a", state_dir=tmp_path)
    assert warp_fire.nag_text("b", "state/sizings/x.yaml", state_dir=tmp_path) == warp_fire.fire_step(
        "state/sizings/x.yaml"
    )
    assert "state/sizings/x.yaml" in warp_fire.nag_text("b", "state/sizings/x.yaml", state_dir=tmp_path, repeat=True)


INVOKE = "coordinator-invoke engine_ask.submit"


def test_non_claude_hosts_get_the_invoke_route_in_every_nag_form(tmp_path):
    for host in ("codex", "agy", "grok"):
        assert warp_fire.fire_command(host=host) == INVOKE
        assert INVOKE in warp_fire.fire_step(host=host)
        first = warp_fire.nag_text(f"s-{host}", state_dir=tmp_path, host=host, repeat=True)
        second = warp_fire.nag_text(f"s-{host}", state_dir=tmp_path, host=host, repeat=True)
        assert INVOKE in first and INVOKE in second
        assert "Workflow" not in first and "Workflow" not in second


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


def test_claude_and_unknown_hosts_keep_workflow(tmp_path):
    for host in (None, "", "claude"):
        assert warp_fire.fire_command(host=host).startswith("Workflow({name:")
        assert INVOKE not in warp_fire.fire_step(host=host)
    assert INVOKE not in warp_fire.nag_text("c", state_dir=tmp_path)


def test_invoke_route_is_the_agy_allow_rule_text():
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "coordinator/bin/unimatrix-install.py").read_text(encoding="utf-8")
    rule = re.search(r'^AGY_ALLOW_RULE = "command\((.*)\)"$', src, re.MULTILINE)
    assert rule and rule.group(1) == warp_fire.INVOKE_SUBMIT_COMMAND


def test_write_budget_advisory_threads_host_from_payload():
    from coordinator_core.hooks import _em_write_budget as wb

    assert INVOKE in wb.route_unfired_deny("codex") and "Workflow" not in wb.route_unfired_deny("codex")
    assert INVOKE in wb.sized_advisory("agy")
    assert wb._host({"host": "codex"}) == "codex" and wb._host({}) is None
    assert wb.route_unfired_deny() == wb._ROUTE_UNFIRED_DENY and "Workflow" in wb._ROUTE_UNFIRED_DENY


def test_turn_budget_and_prefire_texts_follow_host(tmp_path):
    from coordinator_core.hooks import guard_em_prefire_survey as g
    from coordinator_core.hooks import nudge_em_turn_budget as t

    assert INVOKE in t.pre_fire_text(11, 10, host="grok")
    assert "Workflow" in t.pre_fire_text(11, 10)
    assert INVOKE in g.prefire_advisory_text(9, 8, host="codex")
    assert "Workflow" in g.prefire_advisory_text(9, 8)


def test_truncated_edit_size_reads_at_least_and_an_exact_count_does_not(tmp_path):
    cut = warp_fire.nag_text("t1", state_dir=tmp_path, host="grok", edit_size=(3, 16), edit_size_truncated=True)
    assert "at least 3 files / 16 lines" in cut
    exact = warp_fire.nag_text("t2", state_dir=tmp_path, host="grok", edit_size=(3, 16))
    assert "is 3 files / 16 lines" in exact and "at least" not in exact
    again = warp_fire.nag_text(
        "t1", state_dir=tmp_path, host="grok", edit_size=(3, 16), edit_size_truncated=True, repeat=True
    )
    assert again.startswith(warp_fire.NAG_REPEAT_PREFIX) and "at least 3 files / 16 lines" in again


def test_unwritable_store_still_returns_text(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    assert warp_fire.nag_text("s", state_dir=blocker / "sub") == warp_fire.fire_step()
