"""em_prefire_window: window reset, exemptions, disarms, incremental reads."""

from __future__ import annotations

import json

from coordinator_core.session import em_prefire_window as epw


def _line(rec: dict) -> str:
    return json.dumps(rec, separators=(",", ":")) + "\n"


def user(text="do the thing") -> str:
    return _line({"type": "user", "message": {"role": "user", "content": text}})


def tool_result(tid="x") -> str:
    return _line(
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}
    )


def tool_use(tid, name, inp, sidechain=False) -> str:
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
    }
    if sidechain:
        rec["isSidechain"] = True
    return _line(rec)


def run(tmp_path, lines, state=None, **kw):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    return epw.advance(path, state or epw.fresh_state(), **kw)


def test_counts_reads_and_resets_on_user_not_tool_result(tmp_path):
    st = run(tmp_path, [user(), tool_use("a", "Read", {"file_path": "x.py"}), tool_result("a"), tool_use("b", "Grep", {"pattern": "q"})])
    assert st["pre"] == ["a", "b"]
    st = run(tmp_path, [user(), tool_use("a", "Read", {"file_path": "x.py"}), user("again"), tool_use("c", "Glob", {"pattern": "*"})])
    assert st["pre"] == ["c"] and st["window"] == 2


def test_current_id_joins_once(tmp_path):
    lines = [user(), tool_use("a", "Read", {"file_path": "x.py"})]
    st = run(tmp_path, lines, current_tool_use_id="a", current_call=("Read", {"file_path": "x.py"}))
    assert st["pre"] == ["a"]
    st = run(tmp_path, lines, current_tool_use_id="z", current_call=("Read", {"file_path": "q.py"}))
    assert st["pre"] == ["a", "z"]


def test_subagent_records_never_count_or_open(tmp_path):
    st = run(tmp_path, [user(), tool_use("a", "Read", {"file_path": "x"}, sidechain=True)])
    assert st["pre"] == []


def test_path_exemptions(tmp_path):
    st = run(
        tmp_path,
        [
            user("look at src/mod.py please"),
            tool_use("a", "Read", {"file_path": "src/mod.py"}),
            tool_use("b", "Read", {"file_path": "/r/state/warp/ask/requirements.md"}),
            tool_use("c", "Grep", {"pattern": "x", "path": "r\\.coordinator-local\\notes"}),
            tool_use("d", "Read", {"file_path": "/r/state/cross-repo/inbox/m.md"}),
            tool_use("e", "Read", {"file_path": "other.py"}),
        ],
    )
    assert st["pre"] == ["e"]


def test_opening_text_does_not_exempt_dot_or_substring_paths(tmp_path):
    st = run(
        tmp_path,
        [
            user("see src/mod.py. also e.g. foo"),
            tool_use("a", "Grep", {"pattern": "x", "path": "."}),
            tool_use("b", "Read", {"file_path": "src"}),
            tool_use("c", "Read", {"file_path": "src/mod.py"}),
        ],
    )
    assert st["pre"] == ["a", "b"]


def test_settings_bin_cli_forms(tmp_path):
    home = str(tmp_path / "sh")
    cmds = {
        f"{home}/bin/coordinator-doc-new --type x": True,
        f'"{home}/bin/tool.exe" a': True,
        'FOO=1 "${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/cli" a': True,
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/cli": True,
        '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/cli" x': True,
        "grep -r foo .": False,
        f"{home}/other/cli": False,
        f"{home}/bin/sub/cli": False,
        f"{home}/bin/cli x && grep -r foo .": False,
        f"{home}/bin/cli x; ls": False,
        f"{home}/bin/cli $(grep -r foo .)": False,
    }
    for cmd, want in cmds.items():
        assert epw.is_settings_bin_cli(cmd, home) is want, cmd


def test_cli_exempt_pre_but_counted_in_all(tmp_path):
    home = str(tmp_path / "sh")
    st = run(
        tmp_path,
        [user(), tool_use("a", "Bash", {"command": f"{home}/bin/cli x"}), tool_use("b", "Bash", {"command": "ls"})],
        settings_home=home,
    )
    assert st["pre"] == ["b"] and st["all"] == ["a", "b"]


def test_fire_skill_revise_flags(tmp_path):
    from coordinator_core import warp_fire

    st = run(tmp_path, [user(), tool_use("a", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME})])
    assert st["fired"] and st["fire_seen"]
    st = run(tmp_path, [user(), tool_use("a", "Skill", {"skill": "coordinator:pickup"})])
    assert st["disarmed"]
    st = run(tmp_path, [user(), tool_use("a", "Skill", {"skill": "coordinator:plan"})])
    assert not st["disarmed"]
    st = run(tmp_path, [user(), tool_use("a", "Skill", {"skill": "coordinator:something-else"})])
    assert not st["disarmed"]
    st = run(tmp_path, [user(), tool_use("a", "Bash", {"command": "coordinator-invoke workflow.revise y"})])
    assert st["revised"]


def test_new_user_resets_flags_but_not_fire_seen(tmp_path):
    from coordinator_core import warp_fire

    st = run(tmp_path, [user(), tool_use("a", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}), user("next")])
    assert not st["fired"] and st["fire_seen"]


def test_incremental_read_and_partial_line(tmp_path):
    path = tmp_path / "t.jsonl"
    first = user() + tool_use("a", "Read", {"file_path": "x"})
    path.write_text(first + tool_use("b", "Read", {"file_path": "y"})[:20], encoding="utf-8")
    st = epw.advance(path, epw.fresh_state())
    assert st["pre"] == ["a"] and st["offset"] == len(first.encode())
    path.write_text(first + tool_use("b", "Read", {"file_path": "y"}), encoding="utf-8")
    st = epw.advance(path, st)
    assert st["pre"] == ["a", "b"]


def test_delta_over_read_cap_jumps_to_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(epw, "READ_CAP", 10)
    st = run(tmp_path, [user(), tool_use("a", "Read", {"file_path": "x"})])
    assert st["partial"] is True and st["pre"] == []


def test_publish_mechanics_exempt_and_survey_git_counts():
    ok = [
        "git push origin abc:refs/heads/x",
        "git -C scratch/claude-klabauter-pub fetch origin",
        "cd scratch/claude-klabauter-pub && git fetch origin && git checkout -q abc",
        "git -C scratch/claude-klabauter-pub checkout abc",
        "t=$(git merge-tree --write-tree a b)",
        "python3 coordinator/bin/percolate-round.py claude-klabauter --source-ref a",
    ]
    bad = [
        "git log --oneline",
        "git diff HEAD",
        "git checkout abc",
        "git fetch origin | cat",
        "git push origin x && cat secrets",
        "cd /tmp/other && git checkout abc",
        "git status",
    ]
    assert all(epw.publish_mechanics(c) for c in ok)
    assert not any(epw.publish_mechanics(c) for c in bad)


def test_gate_probe_ops_and_sibling_git_probes_count_toward_no_budget():
    exempt = [
        "coordinator-invoke baton.awaiting_gate_recheck --path x",
        "coordinator-invoke roadmap.plan_gate",
        "git -C ../sibling merge-base HEAD origin/main",
        "git -C ../sibling log --oneline -5",
        "cd scratch/x-pub && git -C ../sibling log -1",
    ]
    for cmd in exempt:
        assert epw.classify("Bash", {"command": cmd}, "", None) == (False, False), cmd
    counted = [
        "coordinator-invoke baton.awaiting_gate_recheck | cat",
        "grep baton.awaiting_gate_recheck x",
        "git log --oneline",
        "git merge-base HEAD origin/main",
        "git -C ../sibling diff HEAD",
        "git -C ../sibling log --output=x",
    ]
    for cmd in counted:
        assert epw.classify("Bash", {"command": cmd}, "", None) == (True, True), cmd


def test_lifecycle_op_every_invocation_shape():
    verbatim = "it'\\''s a `thing`; ok & \"q\" $(x)"
    for inv in ("coordinator-invoke", "/opt/x/.coordinator-claude-settings/bin/coordinator-invoke"):
        for arg in (
            "'{\"run_id\":\"r\",\"verbatim\":\"" + verbatim + "\"}'",
            "--params-file /tmp/p.json",
            "'{\"verbatim\":\"line1\nline2; `x`\"}'",
        ):
            for op in epw.LIFECYCLE_OPS:
                assert epw.lifecycle_op(f"{inv} {op} {arg}") == op, (inv, op, arg)
    assert epw.lifecycle_op("coordinator-invoke --params-file f.json workflow.revise") == "workflow.revise"
    for bad in (
        "coordinator-invoke workflow.revise '{\"a\":1}' | cat",
        "coordinator-invoke workflow.revise '{\"a\":1}'; grep x y",
        "coordinator-invoke workflow.revise \"`grep x y`\"",
        "coordinator-invoke workflow.revise $(grep x y)",
        "coordinator-invoke workflow.revise '{\"a\":1}",
        "coordinator-invoke workflow.revise x && grep x y",
        "coordinator-invoke sizing.list '{}'",
    ):
        assert epw.lifecycle_op(bad) is None, bad


def test_relayed_workflow_revise_classifies_exempt_in_every_shape():
    run_id = "warp-2026-10-24-ladder-add-l-c-xl-xxl-tasks-16cd3b-20261024T141445-90120f"
    inline = "coordinator-invoke workflow.revise '{\"run_id\":\"" + run_id + "\",\"verbatim\":\"it'\\''s `x`; a & b\"}'"
    forms = (
        inline,
        "/opt/x/.coordinator-claude-settings/bin/" + inline,
        "coordinator-invoke workflow.revise --params-file /tmp/p.json",
        "/opt/x/.coordinator-claude-settings/bin/coordinator-invoke workflow.revise --params-file /tmp/p.json",
    )
    for cmd in forms:
        assert epw.classify("Bash", {"command": cmd}, "", None) == (False, False), cmd
    survey = "coordinator-invoke sizing.list '{}'"
    assert epw.classify("Bash", {"command": survey}, "", None) == (True, True)


def test_one_test_run_per_window_is_exempt_postlanding(tmp_path):
    cmd = {"command": "python3 -m pytest coordinator_core/x/tests/test_a.py -q"}
    st = run(tmp_path, [user(), tool_use("a", "Bash", cmd), tool_use("b", "Bash", cmd)])
    assert st["all"] == ["b"] and st["test_used"] is True
    assert not epw.is_test_run("pytest coordinator_core/")
    assert not epw.is_test_run("pytest test_a.py; cat x")


def test_unreadable_transcript_returns_state(tmp_path):
    st = epw.advance(tmp_path / "missing.jsonl", epw.fresh_state())
    assert st == epw.fresh_state()


def test_state_roundtrip_and_malformed_reset(tmp_path):
    p = tmp_path / "em-prefire.json"
    t = tmp_path / "t.jsonl"
    t.write_text("abc\n", encoding="utf-8")
    p.write_text("{not json", encoding="utf-8")
    assert epw.load_state(p, t)["offset"] == 4
    epw.save_state(p, {**epw.fresh_state(), "pre": ["a"]})
    assert epw.load_state(p, t)["pre"] == ["a"]
