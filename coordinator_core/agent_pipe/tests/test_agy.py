"""Tests for the Antigravity adapter against the captured real stream."""
from __future__ import annotations

import json
from pathlib import Path

import sys

import pytest

from coordinator_core.agent_pipe import agy as agy_mod
from coordinator_core.agent_pipe import reviewer_run
from coordinator_core.agent_pipe.agy import AgyAcpDriver, AgyAdapter
from coordinator_core.agent_pipe.session_driver import SessionHandle

FIXTURE = Path(__file__).parent / "fixtures" / "agy_stream.ndjson"
CONV = "894074b8-1367-4122-8a8f-ef1f745ba338"


def _lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def test_parse_line_yields_only_agent_messages():
    parsed = [AgyAdapter().parse_line(line) for line in _lines()]
    assert [p for p in parsed if p is not None] == [(CONV, "ALPHA\n\nBETA")]


def test_parse_line_ignores_garbage():
    a = AgyAdapter()
    assert a.parse_line("") is None
    assert a.parse_line("not json") is None
    assert a.parse_line("[1]") is None
    assert a.parse_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"x"}}') is None


def test_parse_line_records_quota_cause_from_error_event():
    a = AgyAdapter()
    assert a.parse_line('{"event":"error","error":{"message":"RESOURCE_EXHAUSTED: quota exceeded"}}') is None
    assert a.cause == "quota_exhausted"


def test_ordinary_stream_leaves_no_cause():
    a = AgyAdapter()
    for line in _lines():
        a.parse_line(line)
    assert a.cause is None


def test_argv_shapes_and_no_bypass(monkeypatch):
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: "/bin/agy")
    a = AgyAdapter()
    spawn = a.spawn_argv("hi")
    assert spawn[:5] == ["/bin/agy", "-p", "hi", "--output-format", "stream-json"]
    resume = a.resume_argv("c1", "yo")
    assert resume[:7] == ["/bin/agy", "--conversation", "c1", "-p", "yo", "--output-format", "stream-json"]
    assert a.spawn_argv("yo", conversation="c1") == resume
    for argv in (spawn, resume):
        assert not any("dangerously" in x for x in argv)
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings == {"permissions": {"allow": [f"command({c} *)" for c in agy_mod.DEFAULT_ALLOW_COMMANDS]}}


def test_caller_allowlist_is_rendered_and_no_argv_carries_a_bare_wildcard(monkeypatch):
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: "/bin/agy")
    for a in (AgyAdapter(), AgyAdapter(["pytest", "git status"])):
        for argv in (a.spawn_argv("p"), a.resume_argv("c", "t")):
            allow = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
            assert allow and all(r != "command(*)" and r.startswith("command(") and r != "command( *)" for r in allow)
    custom = json.loads(AgyAdapter(["pytest"]).spawn_argv("p")[-1])
    assert custom == {"permissions": {"allow": ["command(pytest *)"]}}


def test_wildcard_or_empty_allowlist_entry_is_refused():
    import pytest

    for bad in (["*"], ["command(*)"], ["git", "*"], ["py*"], [""], []):
        with pytest.raises(ValueError):
            AgyAdapter(bad)


def _would_allow(command: str) -> bool:
    return any(command == c or command.startswith(c + " ") for c in agy_mod.DEFAULT_ALLOW_COMMANDS)


def test_default_allowlist_is_exact_prefixes_and_never_permits_push_or_python_dash_c():
    defaults = agy_mod.DEFAULT_ALLOW_COMMANDS
    assert not set(defaults) & {"python", "python3", "git", "nohup"}
    assert all(len(c.split()) >= 2 for c in defaults)
    assert all(c.split()[1] != "*" and not c.startswith("nohup nohup") for c in defaults)
    assert _would_allow("git status -s")
    assert _would_allow("nohup python3 coordinator/bin/unimatrix-agy-leg.py /tmp/s")
    for denied in ("git push origin main", "git fetch", "git remote add x y", "python -c 'print(1)'",
                   "python3 -c 'x'", "nohup python -c 'x'", "nohup sh -c x", "nohup git push", "python other.py"):
        assert not _would_allow(denied), denied


def test_validated_refuses_bare_interpreters_push_dash_c_and_metacharacters():
    import pytest

    for bad in (["python"], ["python3"], ["git"], ["nohup"], ["nohup *"], ["python -c"], ["python3 -c x"],
                ["nohup python -c"], ["git push"], ["git fetch"], ["nohup git push"], ["git status; rm x"],
                ["python coordinator/bin/x.py && ls"]):
        with pytest.raises(ValueError):
            AgyAdapter(bad)
    AgyAdapter(["python coordinator/bin/x.py", "nohup  python3   coordinator/bin/y.py", "git log"])


def test_agy_missing_from_path_falls_back_to_bare_name(monkeypatch):
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: None)
    assert AgyAdapter().spawn_argv("p")[0] == "agy"


FAKE_ACP = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")]


def test_agy_acp_driver_argv_is_agy_acp_with_no_bypass(monkeypatch):
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    assert AgyAcpDriver()._argv == ["agy", "acp"]
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: "/opt/agy")
    assert AgyAcpDriver()._argv == ["/opt/agy", "acp"]
    assert AgyAcpDriver(argv=FAKE_ACP)._argv == FAKE_ACP


def test_agy_acp_driver_starts_prompts_and_resumes_over_acp(monkeypatch):
    monkeypatch.delenv("FAKE_ACP_LOG", raising=False)
    with AgyAcpDriver(argv=FAKE_ACP) as d:
        h = d.start("/w")
        assert isinstance(h, SessionHandle) and h.cwd == "/w"
        ups = list(d.prompt(h, "hi"))
        assert ups[-1].kind == "done"
        assert d.resume(h.session_id).session_id == h.session_id


def test_agy_adapter_headless_argv_unchanged_by_acp_driver(monkeypatch):
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    assert AgyAdapter().spawn_argv("p")[:5] == ["agy", "-p", "p", "--output-format", "stream-json"]


def test_agy_family_stage_argv_is_unrestricted_and_reviewer_argv_stays_sandboxed() -> None:
    stage = agy_mod.family_stage_argv(["/bin/agy"], log_file="a.log")
    assert stage[0] == "/bin/agy" and "--dangerously-skip-permissions" in stage
    assert not {"--sandbox", "--mode", "--settings"} & set(stage)
    assert stage[stage.index("--log-file") + 1] == "a.log"
    assert "--log-file" not in agy_mod.family_stage_argv(["/bin/agy"])
    review = reviewer_run._agy_argv("p", "m")
    assert "--sandbox" in review and "--dangerously-skip-permissions" not in review


_FAKE_STAGE_AGY = '''
import json, subprocess, sys
sys.stdin.read()
if "--dangerously-skip-permissions" in sys.argv:
    out = subprocess.run([sys.executable, "-c", "print('shell-ran-ok')"], capture_output=True, text=True).stdout.strip()
    result = {"status": "success", "response": "DONE: row via agy -- ran shell: " + out}
else:
    result = {"status": "success", "response": "", "denied_actions": ["command/Bash"]}
print(json.dumps({"event": "result", "result": result}))
'''


def test_agy_family_stage_runs_a_shell_command_and_is_consumed_as_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from coordinator_core.codex_runner import workflow

    fake = tmp_path / "fake_agy.py"
    fake.write_text(_FAKE_STAGE_AGY, encoding="utf-8")
    monkeypatch.setattr(workflow, "resolve_agy", lambda: [sys.executable, str(fake)])
    argv = workflow._family_argv("agy", tmp_path)
    assert "--dangerously-skip-permissions" in argv and "--settings" not in argv and "--mode" not in argv
    out = tmp_path / "stdout.jsonl"
    with out.open("wb") as fh:
        proc = subprocess.run(argv, input=b'{"event":"user"}\n', stdout=fh, stderr=subprocess.PIPE, check=False)
    assert proc.returncode == 0
    text = workflow._family_text("agy", out)
    assert text.startswith("DONE: row via agy") and text.endswith("shell-ran-ok")

    denied = tmp_path / "denied.jsonl"
    with denied.open("wb") as fh:
        subprocess.run([sys.executable, str(fake)], input=b"", stdout=fh, check=False)
    with pytest.raises(ValueError, match="no response"):
        workflow._family_text("agy", denied)
