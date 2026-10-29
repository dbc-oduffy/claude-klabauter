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
    path.write_bytes((first + tool_use("b", "Read", {"file_path": "y"})[:20]).encode("utf-8"))
    st = epw.advance(path, epw.fresh_state())
    assert st["pre"] == ["a"] and st["offset"] == len(first.encode())
    path.write_bytes((first + tool_use("b", "Read", {"file_path": "y"})).encode("utf-8"))
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
        "cd /e/example-game-workbench-repo && git push origin work/machine-a/x 2>&1 | tail -5",
        "git fetch origin 2>&1 | head -n 3",
    ]
    bad = [
        "git log --oneline",
        "git diff HEAD",
        "git checkout abc",
        "git fetch origin | cat",
        "git push origin x && cat secrets",
        "git push origin x | tail -5 && cat secrets",
        "git log 2>&1 | tail -5",
        "cd",
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


def test_workflow_revise_with_cd_cat_substitution_and_trim_is_exempt():
    line = (
        'cd /opt/x/coordinator-content-repo && /opt/x/.coordinator-claude-settings/bin/coordinator-invoke '
        'workflow.revise "$(cat scratch/revise-338db0-r4.json)" --bare 2>&1 | tail -c 200'
    )
    assert epw.lifecycle_op(line) == "workflow.revise"
    assert epw.classify("Bash", {"command": line}, "", None) == (False, False)
    for ok in (
        "cd x && coordinator-invoke workflow.revise --params-file f.json | head -n 5",
        'coordinator-invoke workflow.revise "$(cat a.json)" | tail -20',
    ):
        assert epw.lifecycle_op(ok) == "workflow.revise", ok
    for bad in (
        "cd x && cat secret | tail",
        "cd x && coordinator-invoke workflow.revise x | tail",
        'coordinator-invoke workflow.revise "$(cat a; rm b)"',
        'coordinator-invoke workflow.revise "$(cat a b)"',
        "coordinator-invoke workflow.revise x | sh",
        "coordinator-invoke workflow.revise x 2>&1 | sh",
        "coordinator-invoke workflow.revise x | grep y | tail -c 5",
        "cd x && cd y && coordinator-invoke workflow.revise x",
        "cd x; coordinator-invoke workflow.revise x",
        "cd $(pwd) && coordinator-invoke workflow.revise x",
        "cd x && grep y z && coordinator-invoke workflow.revise x",
        'coordinator-invoke workflow.revise "$(cat -n f)"',
        'coordinator-invoke workflow.revise "$(cat ~/.ssh/id_rsa)"',
        'coordinator-invoke workflow.revise "$(cat /etc/*)"',
        'coordinator-invoke workflow.revise "$(cat $(evil))"',
        'coordinator-invoke workflow.revise "$(cat a)$(rm b)"',
        "coordinator-invoke workflow.revise $(cat a)",
        "coordinator-invoke workflow.revise `cat a`",
        "coordinator-invoke workflow.revise x || tail -5",
        "coordinator-invoke workflow.revise x > out | tail -5",
        "coordinator-invoke workflow.revise x\n| tail -5",
        "cd x\ncoordinator-invoke workflow.revise x",
        "cd x || coordinator-invoke workflow.revise x",
        "cd x && coordinator-invoke workflow.revise x && rm y",
    ):
        assert epw.lifecycle_op(bad) is None, bad


def test_deny_texts_name_the_exempt_form():
    from coordinator_core.hooks import guard_em_prefire_survey as g

    form = "coordinator-invoke workflow.revise --params-file <file>"
    assert form in g.postlanding_deny_text(5, 3)


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
    t.write_bytes(b"abc\n")
    p.write_text("{not json", encoding="utf-8")
    assert epw.load_state(p, t)["offset"] == 4
    epw.save_state(p, {**epw.fresh_state(), "pre": ["a"]})
    assert epw.load_state(p, t)["pre"] == ["a"]


def _result(tid, text) -> str:
    return _line(
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}
    )


def test_every_installed_assemble_bin_is_lifecycle():
    names = {
        "backlog-grind-assemble", "baton-assemble", "consolidate-assemble", "coordinator-assemble",
        "execute-plan-assemble", "merge-assemble", "orient-assemble", "pickup-assemble",
        "plan-assemble", "quick-wrap-assemble", "review-assemble", "roadmap-planning-assemble",
        "sizing-assemble", "sprint-planning-assemble", "staff-session-assemble", "workday-complete-assemble",
        "workday-start-inbox-blitz-assemble", "workstream-complete-assemble",
    }
    assert names <= set(epw.LIFECYCLE_BINS)
    for n in names:
        assert epw.lifecycle_op(f"{n} brief --x 1") == n
    assert "handoff.append_session_ledger" in epw.LIFECYCLE_OPS
    assert epw.lifecycle_op("coordinator-invoke handoff.append_session_ledger '{\"a\":1}'") == "handoff.append_session_ledger"
    for bad in ("grep x workstream-complete-assemble", "cat baton-assemble", "baton-assemble brief; grep x y"):
        assert epw.lifecycle_op(bad) is None, bad


def test_doe_close_sequence_replay_is_not_counted_beyond_reply_named_output(tmp_path):
    led = "coordinator-invoke handoff.append_session_ledger '{\"a\":1}'"
    lines = [
        user("close it"),
        tool_use("a1", "Bash", {"command": "workstream-complete-assemble brief"}),
        tool_result("a1"),
        tool_use("a2", "Bash", {"command": "workstream-complete-assemble brief"}),
        tool_use("l1", "Bash", {"command": led}),
        tool_use("l2", "Bash", {"command": led}),
        tool_use("ap", "Bash", {"command": "workstream-complete-assemble apply"}),
        _result("ap", "applied; output at scratch/wsc-apply.json"),
        tool_use("r1", "Read", {"file_path": "/repo/scratch/wsc-apply.json"}),
        tool_use("p1", "Bash", {"command": "python3 -c \"import json; print(json.load(open('scratch/wsc-apply.json')))\""}),
        tool_use("o1", "Read", {"file_path": "other.json"}),
        tool_use("o2", "Bash", {"command": "cat scratch/wsc-apply.json other.json"}),
        tool_use("o3", "Bash", {"command": "cat scratch/wsc-apply.json; grep x y"}),
    ]
    st = run(tmp_path, lines)
    assert st["pre"] == ["o1", "o2", "o3"]
    assert st["named"] == ["scratch/wsc-apply.json"]


def test_reply_named_path_only_from_assembler_results(tmp_path):
    lines = [
        user(),
        tool_use("g", "Bash", {"command": "ls scratch"}),
        _result("g", "scratch/other.json"),
        tool_use("r", "Read", {"file_path": "scratch/other.json"}),
    ]
    assert run(tmp_path, lines)["pre"] == ["g", "r"]


RUN = "ask-20260101T000000-abc123"


def _repo_with_manifest(tmp_path):
    d = tmp_path / "state" / "warp" / RUN
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "doc_writes": ["docs/delivery/" + RUN + ".md"],
                "rows": [{"id": "C1", "writes": ["pkg/mod.py", "pkg/tests/test_mod.py"]}],
                "test_targets": ["pkg/tests/test_mod.py"],
                "write_baseline": {"pkg/mod.py": "x"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_touches_landed_run_footprint_and_records(tmp_path):
    root = _repo_with_manifest(tmp_path)
    t = lambda name, inp: epw.touches_landed_run(name, inp, RUN, root)
    assert t("Read", {"file_path": "pkg/mod.py"})
    assert t("Read", {"file_path": str(root / "pkg" / "mod.py")})
    assert t("Read", {"file_path": "state/warp/" + RUN + "/run.json"})
    assert t("Read", {"file_path": "docs/plans/2026-01-01-thing-abc123.md"})
    assert t("Read", {"file_path": "state/sizings/2026-01-01-thing-abc123.yaml"})
    assert t("Read", {"file_path": "docs/delivery/" + RUN + ".md"})
    assert t("Grep", {"pattern": "x", "path": "pkg"})
    assert t("Bash", {"command": "python -m pytest pkg/tests/test_mod.py"})
    assert t("Bash", {"command": "cat pkg/mod.py | head"})


def test_unrelated_calls_do_not_touch_landed_run(tmp_path):
    root = _repo_with_manifest(tmp_path)
    t = lambda name, inp: epw.touches_landed_run(name, inp, RUN, root)
    assert not t("Bash", {"command": "python -m pytest other/tests/test_other.py"})
    assert not t("Read", {"file_path": "r51/ladder/cell-3.json"})
    assert not t("Read", {"file_path": "state/warp/ask-other-111111/run.json"})
    assert not t("Read", {"file_path": "docs/plans/2026-01-01-thing-zzz999.md"})
    assert not t("Bash", {"command": "python runner.py judge --help"})
    assert not t("Grep", {"pattern": "x", "path": "other"})


def test_missing_or_bad_manifest_fails_open_but_records_still_count(tmp_path):
    t = lambda name, inp: epw.touches_landed_run(name, inp, RUN, tmp_path)
    assert epw.run_footprint(tmp_path, RUN) is None
    assert not t("Bash", {"command": "runner.py judge --help"})
    assert not t("Grep", {"pattern": "x"})
    assert t("Read", {"file_path": "state/warp/" + RUN + "/run.json"})
    assert t("Read", {"file_path": "state/sizings/2026-01-01-thing-abc123.yaml"})
    (tmp_path / "state" / "warp" / RUN).mkdir(parents=True)
    (tmp_path / "state" / "warp" / RUN / "manifest.json").write_text("{bad", encoding="utf-8")
    assert not t("Read", {"file_path": "elsewhere.txt"})
    assert t("Bash", {"command": "ls state/warp/" + RUN})
    assert epw.run_footprint(tmp_path, "") is None
    assert epw.run_footprint(tmp_path, "../x") is None


def _w(tid, path, name="Write"):
    return tool_use(tid, name, {"file_path": path})


def test_self_written_scratch_reads_and_syntax_checks_are_free(tmp_path):
    st = run(
        tmp_path,
        [
            user(),
            _w("w", "/r/scratch/chk/a.py"),
            tool_use("a", "Read", {"file_path": "/r/scratch/chk/a.py"}),
            tool_use("b", "Bash", {"command": "python3 -m py_compile /r/scratch/chk/a.py"}),
            tool_use("c", "Bash", {"command": "cat /r/scratch/chk/a.py"}),
            tool_use("d", "Bash", {"command": "wc -l /r/scratch/chk/a.py"}),
            tool_use("e", "Grep", {"pattern": "x", "path": "/r/scratch/chk/a.py"}),
        ],
    )
    assert st["written"] == ["/r/scratch/chk/a.py"] and st["pre"] == []


def test_self_written_backslash_and_edit_tools(tmp_path):
    st = run(
        tmp_path,
        [user(), _w("w", "C:\\r\\scratch\\n.py", "Edit"), tool_use("a", "Read", {"file_path": "C:/r/scratch/n.py"})],
    )
    assert st["pre"] == []


def test_self_written_does_not_exempt_other_or_mixed_paths(tmp_path):
    st = run(
        tmp_path,
        [
            user(),
            _w("w", "/r/scratch/chk/a.py"),
            tool_use("a", "Read", {"file_path": "scratch/base-HEAD/coordinator_core/x.py"}),
            tool_use("b", "Bash", {"command": "cat /r/scratch/chk/a.py coordinator_core/x.py"}),
            tool_use("c", "Bash", {"command": "python3 -m py_compile coordinator_core/x.py"}),
            tool_use("d", "Bash", {"command": "cat /r/scratch/chk/a.py; grep -r q ."}),
            tool_use("e", "Read", {"file_path": "/r/coordinator_core/a.py"}),
        ],
    )
    assert st["pre"] == ["a", "b", "c", "d", "e"]


def test_self_written_cleared_on_new_user_turn_and_py_compile_not_for_named(tmp_path):
    st = run(tmp_path, [user(), _w("w", "/r/scratch/a.py"), user("next"), tool_use("a", "Read", {"file_path": "/r/scratch/a.py"})])
    assert st["written"] == [] and st["pre"] == ["a"]
    assert epw.named_read("Bash", {"command": "python3 -m py_compile plan.md"}, ["plan.md"]) is False


def test_memo_tooling_is_exempt_whatever_the_tail(tmp_path):
    cmds = [
        "cross-repo-memo send --to x --body y",
        "$HOME/.coordinator-claude-settings/bin/cross-repo-memo list; echo done",
        "cd /r && cross-repo-memo send --body \"$(cat m.md)\" | head -5",
    ]
    st = run(tmp_path, [user()] + [tool_use(f"m{i}", "Bash", {"command": c}) for i, c in enumerate(cmds)])
    assert st["pre"] == [] and st["all"] == []
    st = run(tmp_path, [user(), tool_use("g", "Bash", {"command": "grep -r cross-repo-memo ."})])
    assert st["pre"] == ["g"]


def test_memo_gh_comment_update_ref_and_ledger_append_are_not_counted(tmp_path):
    cmds = [
        'cross-repo-memo send --to coordinator-content-repo-em --body "x"',
        'gh issue comment 12 --body "done; see x"',
        "gh pr comment 7 --body-file notes.md",
        "git update-ref refs/preserve/pin-1 abc123",
        "git -C /r update-ref refs/preserve/pin-2 abc123",
        "coordinator-invoke handoff.append_session_ledger --params-file p.json",
    ]
    st = run(tmp_path, [user()] + [tool_use(f"c{i}", "Bash", {"command": c}) for i, c in enumerate(cmds)])
    assert st["pre"] == [] and st["all"] == []


def test_comms_integrity_exemption_still_refuses_chains_and_other_verbs(tmp_path):
    cmds = [
        "gh issue view 12",
        "gh pr comment 7 --body x && grep -r foo .",
        "git update-ref -d refs/x | grep y",
        "git log --oneline",
        "grep update-ref README.md",
    ]
    st = run(tmp_path, [user()] + [tool_use(f"c{i}", "Bash", {"command": c}) for i, c in enumerate(cmds)])
    assert st["pre"] == [f"c{i}" for i in range(len(cmds))]
    st = run(tmp_path, [user(), tool_use("r", "Read", {"file_path": "/r/src/a.py"})])
    assert st["pre"] == ["r"]


def test_task_reset_on_changed_in_progress_task_only(tmp_path):
    def upd(tid, task, status="in_progress"):
        return tool_use(tid, "TaskUpdate", {"taskId": task, "status": status})

    st = run(
        tmp_path,
        [
            user(),
            upd("u1", "1"),
            tool_use("a", "Read", {"file_path": "x.py"}),
            _w("w", "/r/scratch/z.py"),
            upd("u1b", "1"),
            tool_use("b", "Read", {"file_path": "y.py"}),
            tool_use("c1", "TaskCreate", {"subject": "t2"}),
            tool_use("c", "Read", {"file_path": "z.py"}),
        ],
    )
    assert st["pre"] == ["a", "b", "c"] and st["task_key"] == "1"
    st = run(tmp_path, [user(), upd("u1", "1"), tool_use("a", "Read", {"file_path": "x.py"}), upd("u2", "2"), tool_use("b", "Read", {"file_path": "y.py"})])
    assert st["pre"] == ["b"] and st["task_key"] == "2" and st["written"] == []
    st = run(tmp_path, [user(), upd("u1", "1"), tool_use("a", "Read", {"file_path": "x.py"}), upd("u2", "2", "pending")])
    assert st["pre"] == ["a"]
    todo = {"todos": [{"content": "second", "status": "in_progress"}, {"content": "first", "status": "pending"}]}
    st = run(tmp_path, [user(), tool_use("a", "Read", {"file_path": "x.py"}), tool_use("t", "TodoWrite", todo), tool_use("b", "Read", {"file_path": "y.py"})])
    assert st["pre"] == ["b"] and st["task_key"] == "second"


def test_written_list_is_capped_and_paths_past_the_cap_are_charged(tmp_path):
    over = epw.WRITTEN_CAP + 1
    events = [user()] + [_w(f"w{i}", f"/r/scratch/f{i}.py") for i in range(over)]
    events.append(tool_use("last", "Read", {"file_path": f"/r/scratch/f{over - 1}.py"}))
    st = run(tmp_path, events)
    assert len(st["written"]) == epw.WRITTEN_CAP and st["pre"] == ["last"]
