"""`block-em-post-fire-product-write`: once an EM session has fired a Workflow run, a Bash write to a
tracked product path is recorded as a log-only advisory and admitted; nothing reaches the context.
Driven through `dispatch.evaluate_payload_json` (the production entry point) and `guard.check`."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

import coordinator_core.guard_advisory_counter as counter
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_em_post_fire_shell_write as guard
from coordinator_core.hooks import _em_post_fire
from coordinator_core.hooks import _em_write_budget as hook
from coordinator_core.hooks import flag_em_poll_in_flight

SID = "sess-post-fire"
GUARD = _em_post_fire.GUARD_NAME


@pytest.fixture
def repo(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    monkeypatch.setattr(hook, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "state" / "handoffs").mkdir(parents=True)
    run_dir = root / "state" / "warp" / "run-1"
    run_dir.mkdir(parents=True)
    owned = ["a.py", "c.py", "f.py", "src/a.py", "pkg/a.py", "pkg/other.py", "pkgother.py", "=", "CLAUDE.md"]
    (run_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": owned}], "doc_writes": []}), encoding="utf-8")
    return root


@pytest.fixture
def fires(monkeypatch):
    seen = []
    monkeypatch.setattr(counter, "record_advisory_fire", lambda name, sid, cwd=None: seen.append(name))
    return seen


def _fire(monkeypatch, runs):
    monkeypatch.setattr(flag_em_poll_in_flight, "session_workflow_run_ids", lambda sid: runs)


def _dispatch(root, cmd, **payload):
    raw = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": SID, "cwd": str(root), **payload}
    )
    return dispatch.evaluate_payload_json(raw)


def _decision(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


def _context(out):
    h = (out or {}).get("hookSpecificOutput") or {}
    return h.get("permissionDecisionReason") or h.get("additionalContext") or ""


def _check(repo, cmd):
    return guard.check(cmd, SID, str(repo), {"cwd": str(repo)})


LOGGED = (
    "echo x > a.py",
    "echo x | tee a.py",
    "sed -i s/a/b/ a.py",
    "cat > a.py <<'EOF'\nx = 1\nEOF",
    "cp b.txt a.py",
    "mv b.txt a.py",
    "python -c \"open('a.py','w').write('x')\"",
    "python - <<'EOF'\nopen('a.py','w').write('x')\nEOF",
    "git checkout -- a.py",
    "rm a.py",
    "rm -rf src/a.py",
    "perl -pi -e 's/a/b/' a.py",
    "perl -i.bak -pe 's/a/b/' a.py",
    "patch -p1 < x.diff",
    "patch a.py x.diff",
    "truncate -s 0 a.py",
    "dd if=/dev/zero of=a.py",
    "rsync -a b.txt a.py",
    "install -m 644 b.txt a.py",
)

UNLOGGED = (
    "rm {scratch}",
    "perl -ne 'print' a.py",
    "perl -pi -e 's/a/b/' {scratch}",
    "patch --dry-run a.py x.diff",
    "truncate -s 0 {scratch}",
    "dd if=a.py of={scratch}",
    "rsync -a a.py {scratch}",
    "install -m 644 a.py {scratch}",
    "ls a.py",
    "git add a.py",
    "git commit -m msg",
    "git add -- a.py b.py && git commit -m msg -- a.py b.py",
    "git add -- a.py && git commit -F msg.txt",
)


@pytest.mark.parametrize("cmd", UNLOGGED)
def test_fired_session_read_only_scratch_and_commit_forms_are_not_recorded(repo, monkeypatch, tmp_path, fires, cmd):
    _fire(monkeypatch, ["run-1"])
    # Bash eats an unquoted backslash, so a native Windows path would name a different file.
    cmd = cmd.format(scratch=(tmp_path / "sys-tmp" / "n.txt").as_posix())
    assert _check(repo, cmd) is None
    assert fires == []


@pytest.mark.parametrize("cmd", LOGGED)
def test_fired_session_product_write_is_admitted_and_recorded_not_denied(repo, monkeypatch, fires, cmd):
    _fire(monkeypatch, ["run-1"])
    out = _dispatch(repo, cmd)
    assert _decision(out) != "deny", out
    assert "workflow.revise" not in _context(out)
    assert fires == [GUARD]


def test_hit_returns_none_even_with_a_recorded_fire(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    assert _check(repo, "echo x > a.py") is None
    assert fires == [GUARD]


def test_commit_mixed_with_a_write_is_still_recorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    assert _check(repo, "git add a.py && echo x > a.py") is None
    assert fires == [GUARD]


def test_em_scoped_commit_of_state_run_records_is_admitted_unrecorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    rec = "state/warp/ask-20261027T225221-904da8/notes.jsonl"
    assert _check(repo, f"git add {rec} && git commit -m 'warp: record' -- {rec}") is None
    assert _check(repo, f"git commit -m 'warp: record' -- {rec}") is None
    assert fires == []


def test_fired_session_claude_md_write_is_recorded_and_the_text_names_the_grant_route(repo, monkeypatch, fires):
    from coordinator_core.session.claude_md_grant import GRANT_CLI_NAME

    _fire(monkeypatch, ["run-1"])
    out = _dispatch(repo, "echo x >> CLAUDE.md")
    assert _decision(out) != "deny", out
    assert fires == [GUARD]
    text = _em_post_fire._deny_text((str(repo / "CLAUDE.md"),))
    assert GRANT_CLI_NAME in text and "workflow.revise" in text
    assert "from the repo that holds the CLAUDE.md" in text


@pytest.mark.parametrize(
    "cmd",
    (
        "echo x > state/handoffs/h.md",
        "git tag -a v1 -m rel",
        "git push origin main",
    ),
)
def test_fired_session_non_product_command_is_not_recorded(repo, monkeypatch, fires, cmd):
    _fire(monkeypatch, ["run-1"])
    assert _check(repo, cmd) is None
    assert fires == []


def test_fired_session_scratch_write_is_not_recorded(repo, monkeypatch, tmp_path, fires):
    _fire(monkeypatch, ["run-1"])
    scratch = tmp_path / "sys-tmp" / "n.txt"
    assert _check(repo, f"echo x > {scratch}") is None
    assert fires == []


def test_fired_session_write_to_a_path_no_run_owns_is_not_recorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    assert _check(repo, "echo x > unowned.py") is None
    assert fires == []
    assert _check(repo, "echo x > a.py") is None
    assert fires == [GUARD]


def test_subagent_payload_is_never_recorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    assert guard.check("echo x > a.py", SID, str(repo), {"cwd": str(repo), "agent_id": "a1"}) is None
    assert fires == []


def test_unfired_session_is_not_recorded(repo, monkeypatch, fires):
    _fire(monkeypatch, [])
    assert _check(repo, "echo x > a.py") is None
    assert fires == []


def test_empty_session_id_is_not_recorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    assert guard.check("echo x > a.py", "", str(repo), {"cwd": str(repo)}) is None
    assert fires == []


def test_unfired_check_reads_once_and_never_parses(repo, monkeypatch):
    _fire(monkeypatch, [])
    import coordinator_core.bash_guards._shell_write_projection as proj

    def boom(*a, **k):
        raise AssertionError("parsed an unfired command")

    monkeypatch.setattr(proj, "shell_writes", boom)
    assert _check(repo, "echo x > a.py") is None


def test_fired_check_spawns_no_process(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])

    def boom(*a, **k):
        raise AssertionError("spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _check(repo, "echo x > a.py") is None
    assert fires == [GUARD]


def test_a_crash_admits(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    import coordinator_core.bash_guards._shell_write_projection as proj

    def boom(*a, **k):
        raise RuntimeError("projection crash")

    monkeypatch.setattr(proj, "shell_writes", boom)
    assert _check(repo, "echo x > a.py") is None
    assert fires == []


def _write_index(root, paths):
    body = b"DIRC" + (2).to_bytes(4, "big") + len(paths).to_bytes(4, "big")
    for p in paths:
        name = p.encode()
        entry = b"\x00" * 60 + len(name).to_bytes(2, "big") + name
        body += entry + b"\x00" * (8 - len(entry) % 8)
    (root / ".git" / "index").write_bytes(body + b"\x00" * 20)


def test_rm_of_empty_untracked_file_is_admitted_unrecorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    _write_index(repo, ["a.py"])
    (repo / "=").write_bytes(b"")
    assert _check(repo, "rm =") is None
    assert fires == []


@pytest.mark.parametrize(
    "setup,cmd",
    (
        ("tracked", "rm ="),
        ("nonempty", "rm ="),
        ("untracked", "rm -rf ="),
        ("untracked", "rm -r ="),
        ("untracked", "rm --recursive ="),
        ("untracked", "rm = && echo x > f.py"),
        ("untracked", "rm =; echo x > f.py"),
        ("untracked", "rm = | tee f.py"),
    ),
)
def test_rm_outside_the_empty_untracked_case_is_recorded_and_admitted(repo, monkeypatch, fires, setup, cmd):
    _fire(monkeypatch, ["run-1"])
    _write_index(repo, ["a.py", "="] if setup == "tracked" else ["a.py"])
    (repo / "=").write_bytes(b"x" if setup == "nonempty" else b"")
    assert _check(repo, cmd) is None
    assert fires == [GUARD]


def _record_touch(repo, rel):
    from pathlib import Path

    from coordinator_core.session import touch_record

    sink = touch_record.sink_path(Path(repo) / ".git" / "coordinator-sessions" / SID)
    touch_record.append_event(sink, session_id=SID, agent_id=None, verb=touch_record.VERB_TOUCH, path=rel)


@pytest.mark.parametrize("cmd", ("git checkout -- a.py b.py", "git restore a.py b.py", "git restore -- a.py"))
def test_pathspec_revert_of_own_written_paths_is_unrecorded(repo, monkeypatch, fires, cmd):
    _fire(monkeypatch, ["run-1"])
    _record_touch(repo, "a.py")
    _record_touch(repo, "b.py")
    assert _check(repo, cmd) is None
    assert fires == []


@pytest.mark.parametrize(
    "cmd",
    (
        "git checkout -- a.py c.py",
        "git checkout -- .",
        "git checkout HEAD~1 -- a.py",
        "git restore --source=HEAD~1 a.py",
        "git checkout -- a.py && echo x > a.py",
    ),
)
def test_revert_outside_the_own_written_pathspec_case_is_recorded(repo, monkeypatch, fires, cmd):
    _fire(monkeypatch, ["run-1"])
    _record_touch(repo, "a.py")
    _record_touch(repo, "b.py")
    assert _check(repo, cmd) is None
    assert fires == [GUARD]


def test_pathspec_revert_without_a_touch_record_is_recorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    assert _check(repo, "git checkout -- a.py") is None
    assert fires == [GUARD]


def test_pathspec_revert_with_a_backslash_path_is_unrecorded(repo, monkeypatch, fires):
    _fire(monkeypatch, ["run-1"])
    (repo / "pkg").mkdir()
    _record_touch(repo, "pkg/a.py")
    assert _check(repo, "git checkout -- pkg\\a.py") is None
    assert _check(repo, "git restore pkg\\a.py") is None
    assert fires == []
    assert _check(repo, "git restore pkg\\other.py") is None
    assert fires == [GUARD]


def test_envelope_is_advisory_allow_and_records_the_fire(fires):
    env = _em_post_fire.deny_envelope(SID, "/x")
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert env["guardLevel"] == "log"
    assert fires == [GUARD]


def test_message_is_an_engine_route_not_a_prohibition():
    text = _em_post_fire._POST_FIRE_DENY
    assert "workflow.revise" in text and "engine-ask" in text and "em_context" in text
    assert "belong to the run" not in text


def _launch_wf_run(repo, monkeypatch, tmp_path, engine_id, harness_id, plan_text):
    """A harness `wf_` run id launched by `SID`, whose engine run dir holds a spine with `plan_text`."""
    tmpdir = tmp_path / "managed-tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(flag_em_poll_in_flight, "_tmpdir", lambda: str(tmpdir))
    (tmpdir / f"workflow-run-{SID}-1.json").write_text(
        json.dumps({"run_id": harness_id, "scriptPath": f"/x/scratch/{engine_id}.workflow.mjs"}),
        encoding="utf-8",
    )
    run_dir = repo / "state" / "warp" / engine_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    (run_dir / f"{engine_id}.spine.md").write_text(plan_text, encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "R1", "writes": ["src/em_owned.py", "src/other.py"]}]}), encoding="utf-8")


_SPINE = "- id: R1\n  performer: em\n  disposition: open\n  writes:\n    - src/em_owned.py\n"


def test_wf_run_id_resolves_to_the_ask_run_dir_for_a_performer_em_row(repo, monkeypatch, tmp_path):
    _launch_wf_run(repo, monkeypatch, tmp_path, "ask-20261027T000000-abcdef", "wf_9", _SPINE)
    _fire(monkeypatch, ["wf_9"])
    target = str(repo / "src" / "em_owned.py")
    assert _em_post_fire._is_em_row_surface(os.path.normcase(target), str(repo), SID) is True
    assert _em_post_fire.is_product_path(target, str(repo), SID) is False
    assert _em_post_fire.is_product_path(str(repo / "src" / "other.py"), str(repo), SID) is True


def test_unmappable_wf_run_id_comes_back_unchanged(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(flag_em_poll_in_flight, "_tmpdir", lambda: str(tmp_path))
    assert _em_post_fire._engine_run_id("wf_none", SID) == "wf_none"
    assert _em_post_fire._engine_run_id("ask-1", SID) == "ask-1"


@pytest.mark.parametrize("script, engine_id", [
    (r"E:\r\scratch\warp\ask-20261030T000000-abcdef.workflow.mjs", "ask-20261030T000000-abcdef"),
    ("E:/r/state/warp/warp-x-20261030T000000-1e48f4/resume-script.workflow.mjs", "warp-x-20261030T000000-1e48f4"),
    ("E:/r/scratch/warp/resume-warp-x-20261030T000000-c98511-259aec.launcher.workflow.mjs",
     "warp-x-20261030T000000-c98511"),
    ("E:/r/scratch/warp/notes.md", None),
])
def test_a_resume_script_or_launcher_maps_to_the_run_it_resumes(script, engine_id):
    assert _em_post_fire._engine_id_of_script(script) == engine_id


def test_a_landed_run_is_not_reread_and_a_halted_one_can_resume(repo, monkeypatch):
    from coordinator_core.ops import warp_run_deliverable as wrd

    landed_reads = []
    halted = {"ask-halted-1": True}
    monkeypatch.setattr(wrd, "_landed", lambda root, rid: landed_reads.append(rid) or rid == "ask-landed-1")
    monkeypatch.setattr(wrd, "_halted", lambda root, rid: halted.get(rid, False))
    _fire(monkeypatch, ["ask-landed-1", "ask-halted-1"])
    assert _em_post_fire.live_run_ids(SID, str(repo)) == []
    halted["ask-halted-1"] = False
    assert _em_post_fire.live_run_ids(SID, str(repo)) == ["ask-halted-1"]
    assert landed_reads.count("ask-landed-1") == 1
