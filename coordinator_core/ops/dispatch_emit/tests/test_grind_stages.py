"""
coordinator_core.ops.dispatch_emit.tests.test_grind_stages

Purpose: pins C5's three named assertions (plan
docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md, Tasks § C5) over
`grind_stages.py`'s composers:

  1. Only the review-fix committer mentions staging, and no composer mentions
     `grind-row settle` -- every other composer's call text stays clear of both.
  2. The grind's terminal commit prompt (`compose_grind_terminal_commit_call`) runs one
     bare `coordinator-invoke dispatch.terminal_commit --params-file <path>` segment and
     carries the fenced params block, never JSON typed by the agent.
  3. The op-runner verify composer's `agentType` equals
     `grind_vocab.OP_RUNNER_AGENT_TYPE`.

Negative-spec: does not repeat the banned-token/determinism assertions
overengineering-reviewer #8 places at C7's golden/falsifier over the
COMPOSED script -- this module tests each composer in isolation only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.contract.grind_vocab import OP_RUNNER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit import grind_stages


def _terminal_call(**kw) -> str:
    kw.setdefault("label", "commit")
    kw.setdefault("phase_title", "Grind")
    kw.setdefault("params_file_js", "_paramsFile")
    kw.setdefault("params_js", "params")
    return grind_stages.compose_grind_terminal_commit_call(**kw)


def _review_fix_call(**kw) -> str:
    kw.setdefault("label", "commit:review")
    kw.setdefault("phase_title", "Review fixes")
    kw.setdefault("touched_files_js", "TOUCHED")
    kw.setdefault("profile", "p1")
    return grind_stages.compose_review_fix_commit_call(**kw)


def _all_non_commit_calls() -> dict:
    return {
        "triage": grind_stages.compose_triage_call(
            label="triage:b1",
            phase_title="Triage",
            run_dir="state/queue-grind/run1",
            batch_id="b1",
            triage_depth="standard",
        ),
        "refute-close": grind_stages.compose_refute_close_call(
            label="refute-close:b1",
            phase_title="Refute-close",
        ),
        "fix": grind_stages.compose_fix_call(
            label="fix:row1",
            phase_title="Fix",
            row_id="row1",
            locked_files=["a.py"],
        ),
        "verify-agent": grind_stages.compose_verify_agent_call(
            label="verify:row1",
            phase_title="Verify",
        ),
        "verify-op": grind_stages.compose_verify_op_call(
            label="verify-op:b1",
            phase_title="Verify",
            op="lessons.verify_extraction",
            run_dir="state/queue-grind/run1",
            batch_id="b1",
        ),
        "verify-op-reject": grind_stages.compose_verify_op_call(
            label="verify-op",
            phase_title="Undo",
            run_dir="state/queue-grind/run1",
            reject_paths_js='["a.py", "b.py"]',
        ),
    }


def test_only_commit_composers_mention_settle_or_staging():
    # "stage" alone collides with the stage-kind role noun every composer's
    # prompt uses ("you are the X stage") -- the forbidden tokens are the
    # STAGING-ACTION markers (the git-staging verb and the settle verb),
    # never the role noun.
    forbidden = ("grind-row settle", "stage exactly", "you stage")

    for kind, call_text in _all_non_commit_calls().items():
        lowered = call_text.lower()
        for token in forbidden:
            assert token not in lowered, (
                f"{kind} composer's call text unexpectedly mentions {token!r}"
            )
        assert "you do not stage" in lowered

    review_fix_call = _review_fix_call()
    assert "grind-row settle" not in review_fix_call
    assert "stage exactly" in review_fix_call.lower()
    assert "grind-row settle" not in _terminal_call()

    sweep_call = _sweep_call()
    assert "stage exactly" not in sweep_call.lower()
    assert "grind-row settle" not in sweep_call
    assert "you do not stage" in sweep_call.lower()


def _sweep_call(**kw):
    kw.setdefault("queue_dirs", ["state/bug-backlog"])
    kw.setdefault("run_id_js", "RUN_ID")
    kw.setdefault("record_js", "REC")
    kw.setdefault("declared_js", "DECLARED")
    kw.setdefault("decisions_js", "DECISIONS")
    return grind_stages.compose_ledger_sweep_call(
        label="ledger-sweep:drain", phase_title="Grind", profile="p1", **kw
    )


def test_review_fix_commit_prompt_never_names_a_ledger_in_its_path_lists():
    """A ledger is usually untracked (commit_v2 refuses it as a declared
    deletion absent from HEAD) and, when tracked, its removal reads as a
    staged rollback -- it is settled out of band, never committed."""
    call_text = _review_fix_call()
    assert ".jsonl" not in call_text
    assert "Never name a state/queue-grind/ path in either commit path list" in call_text


def test_ledger_sweep_names_every_queue_and_never_commits():
    call_text = _sweep_call(queue_dirs=["state/bug-backlog", "state/other"])
    assert "grind-row run-record --sweep --profile-dir ' + (_shq(PROFILE_DIR)) + ' --profile p1 --queue state/bug-backlog --queue state/other --run-id " in call_text
    assert "--repo-root ' + (_shq(REPO_ROOT))" in call_text
    assert "never committed" not in call_text
    assert "commit_v2" not in call_text
    assert "coordinator:git-commit-agent" not in call_text


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _emitted_run_record_argv(cwd, *, record: str, declared: list, decisions: str, repo_root: str,
                             profile_dir: str, queue_dirs=("q",)) -> list:
    """The `grind-row run-record` argv the op-runner's Bash calls deliver to the verb.

    The sweep prompt is evaluated by node (the Workflow runtime's stand-in), every fenced block is
    run as its own `bash -c` in ``cwd`` -- each under ``BASH_COMMAND_CAP`` -- and the final block's
    assemble command is swapped for a NUL-delimited printf so the verb's argv comes back as parsed.
    Skips when node or bash cannot run on the host."""
    import re

    # Trap: on Windows `which("bash")` can be the WSL launcher, whose paths differ from Git Bash.
    node, bash = shutil.which("node"), shutil.which("bash")
    if node is None or bash is None:
        pytest.skip("node or bash not on PATH")
    probe = subprocess.run(
        [bash, "-c", "echo ok"], capture_output=True, text=True, check=False, creationflags=_no_console(),
    )
    if probe.stdout.strip() != "ok":
        pytest.skip("bash not runnable")
    sweep = grind_stages.compose_ledger_sweep_call(
        label="s", phase_title="p", profile="p1", queue_dirs=list(queue_dirs),
        run_id_js="RUN_ID", record_js="REC", declared_js="DECLARED", decisions_js="DEC",
    )
    script = (
        grind_stages.SHQ_JS
        + "\nconst E = JSON.parse(process.env.T_BINDINGS);"
        + "\nconst PROFILE_DIR = E.profile_dir, REPO_ROOT = E.repo_root, RUN_ID = E.run_id,"
        + " REC = E.record, DECLARED = E.declared, DEC = E.decisions;"
        + f"\n(async () => {{ const agent = async (p) => console.log(p);\n{sweep}; }})();"
    )
    bindings = {
        "profile_dir": profile_dir, "repo_root": repo_root, "run_id": "20261017T161125Z",
        "record": record, "declared": declared, "decisions": decisions,
    }
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=False,
        env={**os.environ, "T_BINDINGS": json.dumps(bindings)}, creationflags=_no_console(),
    )
    assert out.returncode == 0, out.stderr
    blocks = re.findall(r"```\n(.*?)\n```", out.stdout, re.S)
    assert len(blocks) >= 3, out.stdout
    argv: list = []
    for i, block in enumerate(blocks):
        assert len(block) < grind_stages.BASH_COMMAND_CAP, (i, len(block))
        last = i == len(blocks) - 1
        if last:
            block = block.replace(grind_stages.ASSEMBLE_CMD, r"printf '%s\0'", 1)
        ran = subprocess.run(
            [bash, "-n", "-c", block], capture_output=True, text=True, check=False, creationflags=_no_console(),
        )
        assert ran.returncode == 0, ran.stderr
        ran = subprocess.run(
            [bash, "-c", block], capture_output=True, text=True, check=False, cwd=str(cwd),
            creationflags=_no_console(),
        )
        assert ran.returncode == 0, ran.stderr
        if last:
            argv = ran.stdout.split("\0")[:-1]
    assert argv[0] == "grind-row"
    assert "rm -f" not in block, "the chained command carries no removal; the guard denies the whole command"
    return argv[1:]


@pytest.mark.spawns_process
def test_emitted_run_record_argv_is_accepted_by_the_cli(tmp_path, monkeypatch):
    """The drain brief and `grind_rows.cmd_run_record` agree: the argv the
    brief names, run as written with NOTHING on stdin, writes the run record
    byte-faithfully -- including a quote and non-ASCII in a reason string,
    the shapes that break a hand-quoted or piped blob."""
    import io

    from coordinator_core.backlog_grind_assemble import grind_rows

    record = {
        "profile": "p1", "run_id": "20261017T161125Z", "counts": {}, "spend": {},
        "handed_back": [], "halted": False, "reason": "it's \"quoted\" — café; $(x) `y`",
    }
    argv = _emitted_run_record_argv(
        tmp_path, record=json.dumps(record), declared=[], decisions="", repo_root=str(tmp_path),
        profile_dir=str(tmp_path / "profiles"), queue_dirs=(),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(grind_rows, "_declare_under_repo_root", lambda *_a: None)

    assert grind_rows.main(argv) == grind_rows.EXIT_OK
    target = tmp_path / "state" / "queue-grind" / "p1" / "runs" / "20261017T161125Z.json"
    assert json.loads(target.read_text(encoding="utf-8")) == record


def test_run_record_with_empty_stdin_names_the_heredoc(tmp_path, monkeypatch, capsys):
    """An op-runner that skips the heredoc gets a fix-naming error, not a JSON parse error."""
    import io

    from coordinator_core.backlog_grind_assemble import grind_rows

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = grind_rows.main([
        "run-record", "--profile", "p1", "--run-id", "r1",
        "--record-file", "-", "--repo-root", str(tmp_path),
    ])
    err = capsys.readouterr().err
    assert rc == grind_rows.EXIT_USAGE
    assert "no record on stdin" in err and "heredoc" in err
    assert "not valid JSON" not in err


def test_drain_call_carries_every_flag_in_one_command():
    from coordinator_core.ops.dispatch_emit import grind_commit_contract as gcc

    call_text = _sweep_call(queue_dirs=["state/bug-backlog", "state/other"])
    assert f"grind-row run-record --{gcc.SWEEP_FLAG} --profile-dir " in call_text
    assert call_text.count("grind-row ") == 1
    assert f"--{gcc.QUEUE_FLAG} state/other" in call_text
    assert "--record-urlenc" in call_text
    assert f"--{gcc.DECLARED_FLAG}" in call_text
    assert f"encodeURIComponent(JSON.stringify({{paths: DECLARED}})){grind_stages._URLENC_SHELL_INERT_JS}" in call_text
    assert f"--{gcc.DECISIONS_FLAG} " in call_text
    assert "_shq(DECISIONS)" in call_text
    assert "grind-row sweep" not in call_text
    assert "record-failed" in call_text and "recorded" in call_text
    assert '"swept": {"type": "boolean"}' in call_text and '"sweep_reason"' in call_text
    assert "coordinator:git-commit-agent" not in call_text


def test_drain_call_omits_decisions_flag_behind_a_runtime_guard():
    call_text = _sweep_call(decisions_js="DEC")
    assert "(DEC) ? " in call_text and "_shq(DEC) : ''" in call_text


def test_drain_runs_assemble_so_it_never_rides_the_op_runner():
    # The op-runner spec refuses anything but `coordinator-invoke <op>`; the drain runs backlog-grind-assemble.
    assert "run-record" not in grind_stages.STAGE_AGENT_TYPES
    assert "ledger-sweep" not in grind_stages.STAGE_AGENT_TYPES
    call = _sweep_call()
    assert OP_RUNNER_AGENT_TYPE not in call
    assert "agentType: 'general-purpose'" in call and "model: 'haiku'" in call


def test_drain_call_without_queues_omits_the_sweep_flag():
    call_text = _sweep_call(queue_dirs=[])
    assert "--sweep" not in call_text and "--profile-dir" not in call_text
    assert "grind-row run-record' + ' --profile p1 --run-id " in call_text


def test_fix_prompt_names_every_historical_record_prefix():
    from coordinator_core.ops.dispatch_emit.grind_commit_contract import HISTORICAL_RECORD_PREFIXES

    call_text = grind_stages.compose_fix_call(label="f", phase_title="F", row_id="r")
    assert "Never write a historical record" in call_text
    for prefix in HISTORICAL_RECORD_PREFIXES:
        assert f"`{prefix}`" in call_text
    assert "NEEDS_WIDER_SCOPE" in call_text


def test_terminal_commit_prompt_is_one_bare_invoke_segment_plus_the_params_block():
    call_text = _terminal_call(params_file_js="P", params_js="PARAMS")
    assert call_text.count("coordinator-invoke") == 1
    assert "coordinator-invoke dispatch.terminal_commit --params-file ' + (P) + '" in call_text
    assert "_terminalCommitParamsBlock(PARAMS)" in call_text
    assert "Do not copy, edit or pass it" in call_text
    assert "no `cd &&`, no wrapper, no env prefix" in call_text
    assert "ceremony.commit_v2" not in call_text
    assert "settle" not in call_text.lower()


def test_terminal_commit_call_is_the_haiku_committer_and_returns_the_reply_shape():
    call_text = _terminal_call()
    assert "agentType: 'coordinator:git-commit-agent'" in call_text
    assert "model: 'haiku'" in call_text
    for key in ('"committed"', '"rows"', '"dropped"', '"refused"', '"error"', '"sha"'):
        assert key in call_text
    assert "verbatim" in call_text


def test_terminal_commit_call_returns_already_landed_and_unlanded_verbatim():
    call_text = _terminal_call()
    assert '"already_landed"' in call_text and '"unlanded"' in call_text
    assert "already_landed and unlanded, verbatim (null where absent)" in call_text
    props = grind_stages._TERMINAL_COMMIT_SCHEMA["properties"]
    assert props["already_landed"]["type"] == ["boolean", "null"]
    assert set(props["unlanded"]["items"]["properties"]) == {"row_id", "reason"}
    assert grind_stages._TERMINAL_COMMIT_SCHEMA["required"] == ["committed"]


def test_terminal_commit_prompt_passes_no_trailer_or_path_list():
    call_text = _terminal_call()
    assert "trailer" not in call_text
    assert "deleted_paths" not in call_text.replace("no `paths` array is owed", "")
    assert "stage exactly" not in call_text.lower()


def test_op_runner_agent_type_equals_vocab_constant():
    call_text = grind_stages.compose_verify_op_call(
        label="verify-op:b1",
        phase_title="Verify",
        op="lessons.verify_extraction",
        run_dir="state/queue-grind/run1",
        batch_id="b1",
    )
    assert f"agentType: '{OP_RUNNER_AGENT_TYPE}'" in call_text


def _calls_by_stage(**kw) -> dict:
    calls = {
        "triage": grind_stages.compose_triage_call(
            label="triage:b1", phase_title="Triage", run_dir="r", batch_id="b1", **kw
        ),
        "refute-close": grind_stages.compose_refute_close_call(
            label="rc", phase_title="Refute-close", **kw
        ),
        **_size_calls(**kw),
        "fix": grind_stages.compose_fix_call(
            label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"], **kw
        ),
        "verify": grind_stages.compose_verify_agent_call(label="v", phase_title="Verify", **kw),
        "verify-routing": grind_stages.compose_verify_agent_call(
            label="vr", phase_title="Verify", routing=True, **kw
        ),
        "verify-op": grind_stages.compose_verify_op_call(
            label="vo", phase_title="Verify", op="x.y", run_dir="r", batch_id="b1", **kw
        ),
        "commit": _terminal_call(label="c", phase_title="Commit", **kw),
        "ledger-sweep": grind_stages.compose_ledger_sweep_call(
            label="ls", phase_title="Sweep", profile="p", run_id_js="RUN_ID", record_js="REC",
            declared_js="[]", decisions_js="''", **kw
        ),
        "verify-op-reject": grind_stages.compose_verify_op_call(
            label="vo", phase_title="Undo", run_dir="r", reject_paths_js='["a.py"]', **kw
        ),
    }
    calls["review-fix-commit"] = grind_stages.compose_review_fix_commit_call(
        touched_files_js="paths", label="rf", phase_title="Fix", **kw
    )
    return calls


def _runner_calls() -> dict:
    return {
        "triage": grind_stages.compose_triage_call(
            label="t", phase_title="T", run_dir="r", batch_id="b1", profile="bug",
            targeted_runner=True,
        ),
        "fix": grind_stages.compose_fix_call(
            label="f", phase_title="F", row_id="row1", profile="bug", targeted_runner=True,
        ),
        "verify": grind_stages.compose_verify_agent_call(
            label="v", phase_title="V", profile="bug", touched_files_js="row.touchedFiles",
            run_id_js="RUN_ID", row_id_js="row.rowId", row_path_js="row.path",
            targeted_runner=True,
        ),
    }


def test_runner_prompts_name_the_runner_with_every_flag_and_no_suite_command():
    for stage, call in _runner_calls().items():
        assert "bin/targeted-test-run" in call, stage
        for flag in ("--repo", "--row", "--touched", "--record"):
            assert flag in call, (stage, flag)
        assert ".tests.json" in call, stage
        for banned in ("fast_test_cmd", "full_test_cmd", "with-suite-mutex", "tier-u-grant-cli"):
            assert banned not in call, (stage, banned)


def test_runner_off_prompts_never_name_the_runner():
    off = {
        "triage": grind_stages.compose_triage_call(label="t", phase_title="T", run_dir="r"),
        "fix": grind_stages.compose_fix_call(label="f", phase_title="F", row_id="r"),
        "verify": grind_stages.compose_verify_agent_call(label="v", phase_title="V"),
    }
    for stage, call in off.items():
        assert "targeted-test-run" not in call, stage
    assert "run its tests" in off["fix"]


def test_runner_verify_schema_carries_the_runner_outcome_enum():
    from coordinator_core.bug_blitz.contract import OUTCOMES

    call = _runner_calls()["verify"]
    assert json.dumps(list(OUTCOMES)) in call or all(o in call for o in OUTCOMES)
    assert "tests_record" in call and "tests_outcome" in call


def test_guard_admits_the_composed_runner_line_and_denies_bare_suites(tmp_path, monkeypatch):
    import re

    from coordinator_core.bash_guards import check_test_suite_invocation as guard

    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    line = re.search(r"`([^`]*targeted-test-run[^`]*)`", _runner_calls()["verify"]).group(1)
    line = line.replace("' + ", "").replace(" + '", "")

    def _decision(cmd):
        out = guard.check({
            "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1",
            "cwd": str(tmp_path), "agent_id": "a0123456789abcdef",
        })
        return out and out["hookSpecificOutput"]["permissionDecision"]

    assert _decision(line) != "deny"
    assert _decision("pytest") == "deny"
    assert _decision("pnpm test") == "deny"


def test_runner_fail_outcomes_are_the_non_passing_ones():
    assert set(grind_stages.RUNNER_FAIL_OUTCOMES) == {"failed", "refused", "no-runner"}


def test_every_composer_carries_its_stage_model():
    for key, call_text in _calls_by_stage().items():
        stage = {"review-fix-commit": "commit", "verify-op-reject": "verify-op"}.get(key, key)
        assert f"model: '{grind_stages.STAGE_MODELS[stage]}'" in call_text


def test_triage_stage_model_is_haiku():
    assert grind_stages.STAGE_MODELS["triage"] == "haiku"
    triage_call = grind_stages.compose_triage_call(label="t", phase_title="T", run_dir="r")
    assert "model: 'haiku'" in triage_call


def test_models_override_changes_only_its_stage():
    calls = _calls_by_stage(models={"triage": "opus"})
    for key, call_text in calls.items():
        assert ("model: 'opus'" in call_text) == (key == "triage")


def test_unknown_stage_key_in_models_raises():
    with pytest.raises(ValueError, match="nonesuch"):
        grind_stages.compose_verify_op_call(
            label="u", phase_title="Undo", run_dir="r", reject_paths_js='["a"]',
            models={"nonesuch": "haiku"},
        )
    with pytest.raises(ValueError, match="bogus"):
        grind_stages._agent_call(
            "p", stage="bogus", label="l", phase_title="t", agent_type="a",
            agent_type_host=None, effort="low", schema={},
        )


def test_fix_prompt_precheck_peer_dirty_before_work():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1",
        phase_title="Fix",
        row_id="row1",
        locked_files=["a.py", "b.py"],
    )
    assert "PEER_DIRTY" in call_text
    peer_dirty_index = call_text.index("PEER_DIRTY")
    fix_index = call_text.lower().index("fix the row")
    assert peer_dirty_index < fix_index


def test_review_fix_commit_reconciles_before_retry():
    call_text = _review_fix_call()
    assert "git log" in call_text
    assert "git status" in call_text
    assert "retry blind" in call_text


# ---------------------------------------------------------------------------
# Runtime interpolation (`*_js` params) -- a static file/row-id list is a
# break-class defect for fix/commit/undo/ledger-only-commit, since the real
# touched/declared/unsettled set is only known at RUN time (EM follow-up on
# C7's redo).
# ---------------------------------------------------------------------------


def test_fix_prompt_interpolates_locked_files_js_expression():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1",
        phase_title="Fix",
        row_id="row1",
        locked_files_js="row.declaredFiles",
    )
    assert "(row.declaredFiles).join(', ')" in call_text
    assert "'a.py'" not in call_text  # no static list baked in


def test_fix_static_locked_files_still_works():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert "a.py" in call_text
    assert ".join(" not in call_text


def test_review_fix_commit_prompt_interpolates_the_touched_js_expression():
    call_text = _review_fix_call(touched_files_js="fixResult.touched_files")
    assert "(fixResult.touched_files).join(', ')" in call_text


def _assert_commit_route_bound_to_repo_root(call_text):
    # The route git-commit-agent allows (commit_v2 via coordinator-invoke), never
    # coordinator-safe-commit, which its rules forbid; `--repo` bound to the
    # fire-time REPO_ROOT, never the firing session's cwd.
    assert "coordinator-safe-commit" not in call_text
    assert 'coordinator-invoke" ceremony.commit_v2 --repo \' + (_shq(REPO_ROOT)) + \'' in call_text
    assert call_text.lstrip().startswith("await agent('Your repo is `' + (REPO_ROOT) + '`")


def test_review_fix_call_default_trailers_is_byte_identical_and_names_none():
    base = _review_fix_call()
    assert _review_fix_call(trailers=()) == base
    assert "trailer" not in base


def test_review_fix_call_trailers_are_json_quoted_and_precede_the_route():
    text = _review_fix_call(trailers=['X: a"b`c', "Y: 2"])
    assert "trailer lines" in text
    assert json.dumps(['X: a"b`c', "Y: 2"]) in text.replace("\\\\", "\\") or "Y: 2" in text
    assert text.index("trailer lines") < text.index("ceremony.commit_v2")


def test_fix_prompt_asks_for_repo_relative_paths_inside_and_absolute_outside():
    text = grind_stages.compose_fix_call(label="fix:row1", phase_title="Fix", row_id="row1")
    assert "relative to the repo root" in text
    assert "by its absolute path" in text


def test_review_fix_commit_prompt_names_commit_v2_as_the_route_bound_to_repo_root():
    # Bug e11733fb3a90: an agent left to improvise `git commit` was refused
    # by block-subagent-commit, stranding a settled/archived/staged row.
    call_text = _review_fix_call()
    _assert_commit_route_bound_to_repo_root(call_text)
    assert "block-subagent-commit guard" in call_text
    assert "is NOT a route" in call_text
    assert "Raw `git commit`" in call_text
    # never instructs a bare, unrefused `git commit`
    assert "` Then commit.`" not in call_text
    assert " Then commit. " not in call_text


def test_review_fix_commit_schema_carries_a_failure_reason():
    call_text = _review_fix_call()
    assert '"reason": {"type": "string"}' in call_text
    assert "verbatim refusal or the divergence you found in `reason`" in call_text


def test_reject_mode_is_one_undo_command_run_by_the_op_runner_under_the_verify_op_label():
    call_text = grind_stages.compose_verify_op_call(
        label="verify-op", phase_title="Undo", run_dir="r", reject_paths_js="fixResult.paths",
        reject_reason_js="why",
    )
    assert "label: 'verify-op'" in call_text
    assert "Skip the verify op" in call_text and "(why)" in call_text
    assert '"required": ["undo"]' in call_text
    assert "--params-file" not in call_text
    assert call_text.count("grind-row undo") == 1
    assert "grind-row undo --paths-urlenc \\'" in call_text
    assert "encodeURIComponent(JSON.stringify({paths: fixResult.paths})).replace(/'/g, '%27')" in call_text
    assert "\\' --repo-root ' + (_shq(REPO_ROOT))" in call_text
    assert "ONE Bash call" in call_text
    assert "git checkout" not in call_text
    assert "agentType: 'general-purpose'" in call_text
    assert grind_stages.OP_RUNNER_AGENT_TYPE not in call_text


def test_reject_mode_literal_paths_render_a_json_array():
    call_text = grind_stages.compose_verify_op_call(
        label="verify-op", phase_title="U", run_dir="r", reject_paths_js=json.dumps(["a.py", "b c.py"])
    )
    assert 'JSON.stringify({paths: ["a.py", "b c.py"]})' in call_text


def test_no_undo_or_run_record_composer_remains():
    for folded in ("undo", "run_record"):
        assert not hasattr(grind_stages, f"compose_{folded}_call")
    assert "undo" not in grind_stages.STAGE_MODELS and "run-record" not in grind_stages.STAGE_MODELS


def test_fix_schema_carries_touched_files():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert '"touched_files"' in call_text


_REPO = __import__("pathlib").Path(__file__).resolve().parents[4]
_GOLDEN = _REPO / "coordinator_core/ops/dispatch_emit/tests/fixtures/grind-fixture.golden.mjs"
_NEW_MODULES = [
    "coordinator_core/contract/grind_vocab.py",
    "coordinator_core/ops/dispatch_emit/queue_select.py",
    "coordinator_core/ops/dispatch_emit/grind_profile.py",
    "coordinator_core/ops/dispatch_emit/grind_stages.py",
    "coordinator_core/ops/dispatch_emit/grind_compose.py",
    "coordinator_core/ops/dispatch_emit/queue_emit.py",
    "coordinator_core/backlog_grind_assemble/grind_rows.py",
    "coordinator_core/ops/grind_ops.py",
]


def test_golden_carries_judge_against_head_clause_in_triage_refute_close_and_fix():
    text = _GOLDEN.read_text(encoding="utf-8")
    for fn_name, next_fn_name in (
        ("async function _triageCall", "async function _closeCall"),
        ("async function _closeCall", "async function _fixCall"),
        ("async function _fixCall", "async function _verifyCall"),
    ):
        block = text[text.index(fn_name): text.index(next_fn_name)]
        assert "git show HEAD" in block, fn_name


def test_emitted_prompts_carry_no_posix_only_construct():
    """multi-os-first-class: agents run on Bash or PowerShell, so no prompt
    may lean on a POSIX-only construct."""
    import re
    text = _GOLDEN.read_text(encoding="utf-8")
    for construct in ("/dev/null", "| grep", "sleep "):
        assert construct not in text, construct
    assert not re.search(r"(?<![\w-])[A-Z][A-Z0-9_]*=\S+ [a-z]", text), "VAR=value cmd"


def test_no_machine_local_path_in_new_modules_or_golden():
    """no-single-machine-assumptions: the queue path is cloud-launchable, so
    neither the engine modules nor the emitted script name a host path."""
    import re
    banned = re.compile(r"/Users/|/home/|\b[A-Za-z]:\\|~/\.claude/projects|/private/tmp|gettempdir|machine_local")
    for rel in _NEW_MODULES + [str(_GOLDEN.relative_to(_REPO))]:
        text = (_REPO / rel).read_text(encoding="utf-8")
        hit = banned.search(text)
        assert hit is None, f"{rel}: {hit.group(0) if hit else ''}"


def test_every_grind_row_close_in_golden_carries_every_required_flag():
    """A close invocation missing a flag is one the agent cannot run; the fixer's
    once said only "run `grind-row close`"."""
    import re
    text = _GOLDEN.read_text(encoding="utf-8")
    closes = re.findall(r"grind-row close --[^`]*", text)
    assert len(closes) >= 2
    required = ("--profile-dir", "--profile ", "--row ", "--digest", "--verdict", "--evidence-file",
                "--closed-by", "--run-stamp", "--repo-root")
    for invocation in closes:
        for flag in required:
            assert flag in invocation, (flag, invocation[:120])
    assert "PROFILE_DIR = args.profile_dir" in text


def test_fix_and_triage_schemas_gate_tradeoff_on_a_boolean():
    """Live-run defect 6: a non-empty `tradeoff` string ("None", "No
    tradeoff") routed to needs-judgment. Both schemas now require a
    `has_tradeoff` boolean and the prompt gates `tradeoff` on it."""
    fix_call = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert "has_tradeoff" in fix_call
    assert "has_tradeoff" in fix_call.lower()

    triage_call = grind_stages.compose_triage_call(
        label="triage:b1", phase_title="Triage", run_dir="state/queue-grind/run1",
        batch_id="b1", triage_depth="standard", verdicts=["confirmed-bug", "not-reproduced"],
    )
    assert "has_tradeoff" in triage_call
    assert '"enum": ["confirmed-bug", "not-reproduced"]' in triage_call


def test_review_fix_commit_prompt_carries_an_explicit_subject_and_body():
    """Live-run defect 1: `coordinator:git-commit-agent` refuses a brief with
    no subject. The review-fix committer composes one at runtime; the terminal
    commit's subject and body come from `dispatch.terminal_commit`."""
    commit_call = _review_fix_call()
    assert "Use commit subject `grind(p1): review fixes`" in commit_call
    assert "commit body naming the reviewed work" in commit_call


# ---------------------------------------------------------------------------
# P145-C2: judge-against-HEAD clause + refute-note revival + origin
# ---------------------------------------------------------------------------


def test_judge_against_head_clause_present_in_module_at_least_once():
    """`grep -c 'git show HEAD' grind_stages.py` is at least 1 (C2 acceptance)."""
    module_path = __import__("pathlib").Path(grind_stages.__file__)
    text = module_path.read_text(encoding="utf-8")
    assert text.count("git show HEAD") >= 1


def test_judge_against_head_clause_in_triage_refute_close_and_fix_prompts():
    triage_call = grind_stages.compose_triage_call(
        label="triage:b1", phase_title="Triage", run_dir="state/queue-grind/run1",
        batch_id="b1", triage_depth="standard",
    )
    refute_close_call = grind_stages.compose_refute_close_call(
        label="refute-close:b1", phase_title="Refute-close",
    )
    fix_call = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    for call_text in (triage_call, refute_close_call, fix_call):
        assert "git show HEAD" in call_text


def test_refute_close_prompt_names_each_proposals_origin():
    call_text = grind_stages.compose_refute_close_call(
        label="refute-close:b1", phase_title="Refute-close",
    )
    assert "origin" in call_text


def test_fix_prompt_interpolates_refute_note_js_expression():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1",
        phase_title="Fix",
        row_id="row1",
        locked_files=["a.py"],
        refute_note_js="row.refuteNote",
    )
    assert "(row.refuteNote)" in call_text


def test_fix_prompt_omits_refute_note_clause_when_absent():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert "refuteNote" not in call_text


def test_grind_row_verb_always_run_through_backlog_grind_assemble():
    """Every composer's PROMPT text names the installed entrypoint
    (`backlog-grind-assemble grind-row <verb>`) -- a bare `grind-row` is not
    on PATH and exits 127 (live-run defect 8)."""
    import re

    calls = dict(_all_non_commit_calls())
    calls["commit"] = _terminal_call()
    calls["ledger-sweep"] = _sweep_call()
    for kind, call_text in calls.items():
        for m in re.finditer(r"grind-row", call_text):
            prefix = call_text[max(0, m.start() - len("backlog-grind-assemble ")):m.start()]
            assert prefix.endswith("backlog-grind-assemble "), (
                f"{kind}: bare `grind-row` not preceded by `backlog-grind-assemble `: "
                f"...{call_text[max(0, m.start()-40):m.start()+20]!r}..."
            )


def test_every_stage_binds_the_fire_time_repo_root_never_cwd():
    """A queue grind emitted for repo A and fired from a session whose cwd is
    repo B ran every committer in B. Each prompt must anchor to `REPO_ROOT`,
    and no `grind-row` invocation may fall back to `--repo-root .`."""
    calls = dict(_all_non_commit_calls())
    calls["commit"] = _terminal_call()
    calls["ledger-sweep"] = _sweep_call()
    for kind, call_text in calls.items():
        if "agent(" not in call_text or kind.startswith("verify-op"):
            continue
        assert "Your repo is `' + (REPO_ROOT) + '`" in call_text, kind
        assert "--repo-root ." not in call_text, kind
        if "--repo-root" in call_text:
            assert "--repo-root ' + (_shq(REPO_ROOT)) + '" in call_text, kind


def test_review_fix_commit_call_stages_only_the_expression_and_settles_no_ledger():
    text = grind_stages.compose_review_fix_commit_call(
        touched_files_js="reviewFix.touched_files",
        label="commit:review",
        phase_title="Review fixes",
        profile="p",
        trailers=["X: 1"],
    )
    assert "(reviewFix.touched_files).join(', ')" in text
    assert "ledger:" not in text
    assert "grind-row settle" not in text
    assert "--row-id" not in text
    assert '"X: 1"' in text
    assert "no-op" in text
    assert "state/queue-grind/ path" in text


def test_triage_brief_binds_a_rows_pm_ruling():
    """An APM/PM ruling on a row is binding triage input: a grind that
    re-derived `park` from the body alone re-triaged rows the APM had ruled
    `cloud grinds: skip`."""
    call_text = grind_stages.compose_triage_call(
        label="triage:b0", phase_title="Grind", run_dir="RUN", profile="p1",
        batch_id="b0", triage_depth="standard", verdicts=["fix", "park"],
    )
    assert "`pm_ruling` field is a binding ruling" in call_text
    assert "quote it in that row\\'s `evidence`" in call_text
    assert "never re-derive a fresh verdict from the body alone" in call_text


def test_review_fix_commit_prompt_passes_drop_ignored_and_reports_the_dropped_paths():
    text = _review_fix_call()
    assert "drop_ignored" in text
    assert "dropped_ignored" in text


def test_fix_prompt_fences_the_published_mirror():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"]
    )
    assert "published mirror" in call_text
    assert "publish.mirrors.*" in call_text


def test_paths_reach_the_emitted_command_as_one_single_quoted_token():
    import json
    import shutil
    import subprocess

    import pytest

    from coordinator_core.ops.dispatch_emit import grind_stages as gs

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    profile_dir = "C:\\a b\\profiles"
    repo_root = "/r o/it's"
    sweep = gs.compose_ledger_sweep_call(
        label="s", phase_title="p", profile="x", queue_dirs=["q"],
        run_id_js="'r'", record_js="'{}'", declared_js="[]", decisions_js="''",
    )
    script = (
        gs.SHQ_JS
        + f"\nconst PROFILE_DIR = {json.dumps(profile_dir)}, REPO_ROOT = {json.dumps(repo_root)};\n"
        + f"(async () => {{ const agent = async (p) => console.log(p);\n{sweep}; }})();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    assert "--profile-dir 'C:\\a b\\profiles' " in out.stdout
    assert "--repo-root '/r o/it'\\''s'" in out.stdout


@pytest.mark.spawns_process
def test_emitted_ledger_sweep_command_parses_and_round_trips_under_bash(tmp_path):
    """A run record far past the ~8 KB `bash -c` cap, carrying quotes, spaces and Windows paths,
    reaches the verb byte-faithfully: every Bash call the prompt names parses and stays under the cap."""
    from urllib.parse import unquote

    record = json.dumps({
        "reason": "it's \"q\" (a) !b *c ~d $(x) `y` C:\\a b\\c " + "é'(x) " * 40,
        "spend": ["P" * 40 + " 'k' " + "Z" * 40] * 200,
    })
    declared = ["C:\\a b\\it's.yaml", "state/it's (x).yaml"] * 5
    assert len(record) > 2 * grind_stages.BASH_COMMAND_CAP
    argv = _emitted_run_record_argv(
        tmp_path, record=record, declared=declared, decisions="C:\\a b\\it's decisions.json",
        repo_root="/r o/it's", profile_dir="C:\\a b\\profiles",
    )
    assert unquote(argv[argv.index("--record-urlenc") + 1]) == record
    assert json.loads(unquote(argv[argv.index("--declared-urlenc") + 1])) == {"paths": declared}
    assert argv[argv.index("--profile-dir") + 1] == "C:\\a b\\profiles"
    assert argv[argv.index("--decisions-file") + 1] == "C:\\a b\\it's decisions.json"
    assert argv[argv.index("--repo-root") + 1] == "/r o/it's"


def test_every_evidence_file_flag_names_its_evidence_path():
    """AC10: no prompt leaves `--evidence-file` to a placeholder."""
    fix = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"], profile="bug",
    )
    assert "--evidence-file ' + 'state/queue-grind/bug/evidence/' + '<run-id>' + '/' + 'row1' + '.done.md'" in fix
    assert "<a file with your evidence>" not in fix
    refute = grind_stages.compose_refute_close_call(
        label="r", phase_title="R", profile="bug", run_id_js="RID",
    )
    assert "'state/queue-grind/bug/evidence/' + (RID) + '/' + '<its row_id>' + '.verify.md'" in refute
    assert "<a file with your evidence>" not in refute
    assert "never truncate" in refute


def _size_calls(**kw) -> dict:
    return {
        "size": grind_stages.compose_size_call(
            label="sz", phase_title="Size", row_js="row", evidence_js="ev", fix_plan_js="fp", **kw
        ),
    }


def test_size_call_carries_scout_agent_schema_and_clauses():
    from coordinator_core.ops.dispatch_emit import ask_contract

    call = _size_calls()["size"]
    assert "agentType: 'coordinator:sizing-scout'" in call
    for field in ask_contract.SCOUT_SCHEMA["required"]:
        assert f'"{field}"' in call
    assert grind_stages._JUDGE_AGAINST_HEAD_CLAUSE in call
    assert grind_stages._NO_STAGING_CLAUSE in call
    assert "tshirt_size" not in call
    assert "plan-weight" not in call


def test_size_call_runs_the_assemble_chain_as_its_closing_act():
    call = _size_calls()["size"]
    assert "_sizeChainTemplate(row)" in call
    assert "_sizeAssembleWhen()" in call
    assert "Return its exit code as exit_code" in call
    assert "run no sizing-assemble" not in call
    for field in ("exit_code", "stdout", "stderr"):
        assert f'"{field}"' in call
    assert "required" in call and '"exit_code"' not in grind_stages.SIZE_SCOUT_SCHEMA["required"]
    assert not hasattr(grind_stages, "compose_size_assemble_call")
    assert "size-assemble" not in grind_stages.STAGE_MODELS


def _final_verify_calls() -> dict:
    kw = dict(final_js="row.verifyRetried", undo_paths_js="_undoPaths(row)")
    return {
        "verify-op": grind_stages.compose_verify_op_call(
            label="vo", phase_title="Verify", op="x.y", run_dir="r", batch_id="b1",
            row_id_js="row.rowId", row_path_js="row.path", **kw,
        ),
        "verify": grind_stages.compose_verify_agent_call(label="v", phase_title="Verify", **kw),
        "verify-targeted": grind_stages.compose_verify_agent_call(
            label="v", phase_title="Verify", targeted_runner=True, profile="bug", **kw
        ),
    }


def test_the_verify_stages_run_the_undo_op_themselves_on_the_final_verification():
    for key, call in _final_verify_calls().items():
        assert "(row.verifyRetried) ? " in call, key
        assert "grind-row undo --paths-urlenc" in call, f"{key} lacks the undo command"
        assert "_undoPaths(row)" in call, key
        assert "and return its result as `undo`" in call, key
        assert '"undo": {' in call, key
    op_call = _final_verify_calls()["verify-op"]
    assert "agentType: 'general-purpose'" in op_call and OP_RUNNER_AGENT_TYPE not in op_call
    assert "naming neither `' + (row.rowId) + '` nor `' + (row.path) + '`" in op_call
    assert "neither `passed` nor `nothing-to-run`" in _final_verify_calls()["verify-targeted"]


def test_a_verify_call_without_an_undo_carries_no_undo_clause():
    op_call = grind_stages.compose_verify_op_call(
        label="vo", phase_title="Verify", op="x.y", run_dir="r", batch_id="b1"
    )
    agent_call = grind_stages.compose_verify_agent_call(label="v", phase_title="Verify")
    for call in (op_call, agent_call):
        assert "grind-row undo" not in call
        assert '"undo"' not in call


def test_size_calls_carry_sonnet_and_follow_a_models_override():
    for call in _size_calls().values():
        assert "model: 'sonnet'" in call
    assert len(_size_calls()) == 1
    assert _size_calls(models={"size": "sonnet"})["size"].count("model: 'sonnet'") == 1
    with pytest.raises(ValueError, match="pinned to 'sonnet'"):
        _size_calls(models={"size": "haiku"})


def test_size_stages_are_in_the_table_driven_model_helper():
    for key, call_text in _size_calls().items():
        assert f"model: '{grind_stages.STAGE_MODELS[key]}'" in call_text


def test_size_assemble_js_commands():
    js = grind_stages.SIZE_ASSEMBLE_JS
    chain = js.split("const _sizeChainCmd")[1].split("const _SIZE_SLOTS")[0]
    assert "grind-row size-chain" in chain
    for gone in ("_sizeSupersededTest", "_sizeMarkCmd", "_sizeBatonCmd", "_sizeAssembleCmd", "grep", "/dev/null", "'cd '"):
        assert gone not in js, gone


_SCOUTED = {
    "tshirt": "M", "intent": "rework", "name": "row-a-rework", "premise_provenance": "read",
    "premise_evidence": "read-the-row", "exit_criterion": "tests-pass",
}


def _size_chain_command(row_id: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    js = (
        grind_stages.SHQ_JS + "\n" + grind_stages.SIZE_ASSEMBLE_JS
        + "\nconst REPO_ROOT = '/r', PROFILE_NAME = 'bug', RUN_ID = 'x';"
        + f"\nconsole.log(_sizeChainCmd({json.dumps(_SCOUTED)}, {json.dumps({'row_id': row_id, 'path': 'p', 'digest': 'd'})}, 's.yaml'));"
    )
    out = subprocess.run(
        [node, "-e", js], capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_size_chain_is_one_plain_engine_command_for_a_reminted_and_a_plain_row():
    for row_id in ("2026-10-15-tf-coordinator-tests-test-x-fedcba987654", "plain-row"):
        cmd = _size_chain_command(row_id)
        assert "grind-row size-chain" in cmd
        for flag in ("--profile", "--row-id", "--digest", "--sizing", "--run-stamp", "--repo-root", " -- ", "--tshirt", "--exit-criterion"):
            assert flag in cmd, flag
        for banned in ("|", "2>", "/dev/null", "if ", "; fi", "&&", "cd ", ";"):
            assert banned not in cmd, banned


def test_the_commit_agent_is_haiku_in_every_emitter():
    from coordinator_core.ops.dispatch_emit import emit

    assert grind_stages.STAGE_MODELS["commit"] == "haiku"
    assert emit._AGENT_MODELS[emit._COMMIT_AGENT_TYPE] == "haiku"
    with pytest.raises(ValueError, match="pinned to 'haiku'"):
        _calls_by_stage(models={"commit": "sonnet"})


def test_verify_test_run_is_the_test_runner_on_haiku():
    from coordinator_core.ops.dispatch_emit import emit

    for targeted in (False, True):
        call = grind_stages.compose_verify_agent_call(
            label="v", phase_title="Verify", targeted_runner=targeted
        )
        assert f"agentType: '{emit._TEST_AGENT_TYPE}'" in call
        assert "model: 'haiku'" in call


def test_every_doe_typed_call_reads_the_engine_table():
    from coordinator_core.ops.dispatch_emit import emit

    for stage, agent_type in grind_stages.STAGE_AGENT_TYPES.items():
        assert grind_stages.STAGE_MODELS[stage] == emit._AGENT_MODELS[agent_type]
    general = set(grind_stages.STAGE_MODELS) - set(grind_stages.STAGE_AGENT_TYPES)
    assert general == {"triage", "refute-close", "fix", "ledger-sweep"}


@pytest.mark.parametrize("stage", sorted(set(grind_stages.STAGE_AGENT_TYPES) - {"adjudicate"}))
def test_a_models_override_changing_a_pinned_type_is_refused(stage):
    with pytest.raises(ValueError, match="pinned to"):
        _calls_by_stage(models={stage: "opus"})


def test_the_adjudicator_override_is_refused_and_its_model_is_opus():
    from coordinator_core.ops.dispatch_emit import pm_adjudication

    assert pm_adjudication.ADJUDICATOR_AGENT_TYPE == grind_stages.APM_AGENT_TYPE
    assert "model: 'opus'" in pm_adjudication.compose_adjudicate_block(agent_type_host=None)
    with pytest.raises(ValueError, match="pinned to 'opus'"):
        pm_adjudication.compose_adjudicate_block(
            agent_type_host=None, models={"adjudicate": "haiku"}
        )
