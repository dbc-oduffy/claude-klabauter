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


def test_parse_line_yields_only_agent_messages() -> None:
    parsed = [AgyAdapter().parse_line(line) for line in _lines()]
    assert [p for p in parsed if p is not None] == [(CONV, "ALPHA\n\nBETA")]


def test_parse_line_ignores_garbage() -> None:
    a = AgyAdapter()
    assert a.parse_line("") is None
    assert a.parse_line("not json") is None
    assert a.parse_line("[1]") is None
    assert a.parse_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"x"}}') is None


def test_parse_line_records_quota_cause_from_error_event() -> None:
    a = AgyAdapter()
    assert a.parse_line('{"event":"error","error":{"message":"RESOURCE_EXHAUSTED: quota exceeded"}}') is None
    assert a.cause == "quota_exhausted"


def test_ordinary_stream_leaves_no_cause() -> None:
    a = AgyAdapter()
    for line in _lines():
        a.parse_line(line)
    assert a.cause is None


def test_argv_shapes_and_no_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_caller_allowlist_is_rendered_and_no_argv_carries_a_bare_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: "/bin/agy")
    for a in (AgyAdapter(), AgyAdapter(["pytest", "git status"])):
        for argv in (a.spawn_argv("p"), a.resume_argv("c", "t")):
            allow = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
            assert allow and all(r != "command(*)" and r.startswith("command(") and r != "command( *)" for r in allow)
    custom = json.loads(AgyAdapter(["pytest"]).spawn_argv("p")[-1])
    assert custom == {"permissions": {"allow": ["command(pytest *)"]}}


def test_wildcard_or_empty_allowlist_entry_is_refused() -> None:
    import pytest

    for bad in (["*"], ["command(*)"], ["git", "*"], ["py*"], [""], []):
        with pytest.raises(ValueError):
            AgyAdapter(bad)


def _would_allow(command: str) -> bool:
    return any(command == c or command.startswith(c + " ") for c in agy_mod.DEFAULT_ALLOW_COMMANDS)


def test_default_allowlist_is_exact_prefixes_and_never_permits_push_or_python_dash_c() -> None:
    defaults = agy_mod.DEFAULT_ALLOW_COMMANDS
    assert not set(defaults) & {"python", "python3", "git", "nohup"}
    assert all(len(c.split()) >= 2 for c in defaults)
    assert all(c.split()[1] != "*" and not c.startswith("nohup nohup") for c in defaults)
    assert _would_allow("git status -s")
    assert _would_allow("nohup python3 coordinator/bin/unimatrix-agy-leg.py /tmp/s")
    for denied in ("git push origin main", "git fetch", "git remote add x y", "python -c 'print(1)'",
                   "python3 -c 'x'", "nohup python -c 'x'", "nohup sh -c x", "nohup git push", "python other.py"):
        assert not _would_allow(denied), denied


def test_validated_refuses_bare_interpreters_push_dash_c_and_metacharacters() -> None:
    import pytest

    for bad in (["python"], ["python3"], ["git"], ["nohup"], ["nohup *"], ["python -c"], ["python3 -c x"],
                ["nohup python -c"], ["git push"], ["git fetch"], ["nohup git push"], ["git status; rm x"],
                ["python coordinator/bin/x.py && ls"]):
        with pytest.raises(ValueError):
            AgyAdapter(bad)
    AgyAdapter(["python coordinator/bin/x.py", "nohup  python3   coordinator/bin/y.py", "git log"])


def test_agy_missing_from_path_falls_back_to_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: None)
    assert AgyAdapter().spawn_argv("p")[0] == "agy"


FAKE_ACP = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")]


def test_agy_acp_driver_argv_is_agy_acp_with_no_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    assert AgyAcpDriver()._argv == ["agy", "acp"]
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: "/opt/agy")
    assert AgyAcpDriver()._argv == ["/opt/agy", "acp"]
    assert AgyAcpDriver(argv=FAKE_ACP)._argv == FAKE_ACP


def test_agy_acp_driver_starts_prompts_and_resumes_over_acp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_ACP_LOG", raising=False)
    with AgyAcpDriver(argv=FAKE_ACP) as d:
        h = d.start("/w")
        assert isinstance(h, SessionHandle) and h.cwd == "/w"
        ups = list(d.prompt(h, "hi"))
        assert ups[-1].kind == "done"
        assert d.resume(h.session_id).session_id == h.session_id


def test_agy_adapter_headless_argv_unchanged_by_acp_driver(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture
def agy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "ag" / "brain"))
    return tmp_path


def _hold_presence(tmp_path: Path, conv: str):
    path = tmp_path / "ag" / "presence" / f"{conv}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    assert agy_mod._try_lock(fh)
    return fh


def _drop(fh) -> None:
    agy_mod._unlock(fh)
    fh.close()


def _spool_lines(tmp_path: Path, conv: str) -> list[dict]:
    path = tmp_path / "home" / "unimatrix" / "agy-legs" / f"{conv}.queue.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_presence_probe_reads_a_held_lock_and_a_free_one(agy_home: Path) -> None:
    assert not agy_mod.presence_held("c1")
    fh = _hold_presence(agy_home, "c1")
    try:
        assert agy_mod.presence_held("c1")
        assert not agy_mod.presence_held("c2")
    finally:
        _drop(fh)
    assert not agy_mod.presence_held("c1")


def test_claim_with_presence_held_spools_one_unimatrix_line_and_returns_no_lease(agy_home: Path) -> None:
    fh = _hold_presence(agy_home, "c1")
    try:
        assert agy_mod.claim_resume("c1", "hello", "g-em", wait_s=0) is None
    finally:
        _drop(fh)
    [line] = _spool_lines(agy_home, "c1")
    assert set(line) == {"id", "text", "from"} and line["text"] == "hello" and line["from"] == "g-em"
    record = json.loads((agy_home / "home" / "unimatrix" / "agy-legs" / "c1.json").read_text())
    assert record == {"conversation": "c1", "mode": "pending", "spooled": True}


def test_free_conversation_gets_a_lease_and_spools_nothing(agy_home: Path) -> None:
    lease = agy_mod.claim_resume("c1", "hello", "g-em", wait_s=0)
    assert lease is not None
    lease.release()
    assert _spool_lines(agy_home, "c1") == []


def test_second_claim_on_a_running_resume_spools_then_a_released_slot_is_free_again(agy_home: Path) -> None:
    first = agy_mod.claim_resume("c1", "one", "s", wait_s=0)
    assert first is not None
    assert agy_mod.claim_resume("c1", "two", "s", wait_s=0) is None
    assert [x["text"] for x in _spool_lines(agy_home, "c1")] == ["two"]
    other = agy_mod.claim_resume("other", "x", "s", wait_s=0)
    assert other is not None
    other.release()
    first.release()
    first.release()
    again = agy_mod.claim_resume("c1", "three", "s", wait_s=0)
    assert again is not None
    again.release()


def test_presence_taken_while_the_slot_was_held_spools_instead_of_spawning(agy_home: Path) -> None:
    lease = agy_mod.claim_resume("c1", "one", "s", wait_s=0)
    assert lease is not None
    fh = _hold_presence(agy_home, "c1")
    try:
        lease.release()
        assert agy_mod.claim_resume("c1", "two", "s", wait_s=0) is None
    finally:
        _drop(fh)
    assert [x["text"] for x in _spool_lines(agy_home, "c1")] == ["two"]


def test_concurrent_claims_never_overlap(agy_home: Path) -> None:
    import threading
    import time

    live = [0]
    peak = [0]
    guard = threading.Lock()
    done = []

    def worker() -> None:
        lease = agy_mod.claim_resume("c1", "m", "s", wait_s=20)
        assert lease is not None
        with guard:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.03)
        with guard:
            live[0] -= 1
        lease.release()
        done.append(1)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(done) == 6 and peak[0] == 1
    assert _spool_lines(agy_home, "c1") == []


def test_full_spool_and_unsafe_id_refuse_but_an_unsafe_id_still_resumes(agy_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = agy_home / "home" / "unimatrix" / "agy-legs"
    spool.mkdir(parents=True)
    (spool / "c1.queue.jsonl").write_bytes(b"x" * 20)
    monkeypatch.setattr(agy_mod, "SPOOL_MAX_BYTES", 10)
    with pytest.raises(agy_mod.SpoolRefused):
        agy_mod.spool_text("c1", "y", "s")
    with pytest.raises(agy_mod.SpoolRefused):
        agy_mod.spool_text("../evil", "x", "s")
    lease = agy_mod.claim_resume("a/b", "x", "s", wait_s=0)
    assert lease is not None
    lease.release()


def test_take_spool_claims_texts_once_and_leaves_a_held_presence_alone(agy_home: Path) -> None:
    agy_mod.spool_text("c1", "a", "s")
    agy_mod.spool_text("c1", "b", "s")
    fh = _hold_presence(agy_home, "c1")
    try:
        assert agy_mod.take_spool("c1") == []
    finally:
        _drop(fh)
    assert agy_mod.take_spool("c1") == ["a", "b"]
    assert agy_mod.take_spool("c1") == []
    assert _spool_lines(agy_home, "c1") == []


def test_presence_open_denied_counts_as_held(agy_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.open

    def denied(path, *a, **kw):
        if str(path).endswith("c1.lock"):
            raise PermissionError(13, "denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", denied)
    assert agy_mod.presence_held("c1")
    assert not agy_mod.presence_held("c2")


def test_default_resume_wait_is_inside_the_op_budget() -> None:
    assert agy_mod.RESUME_WAIT_S <= 0.5


def test_claim_without_text_spools_nothing_when_presence_held(agy_home: Path) -> None:
    fh = _hold_presence(agy_home, "c1")
    try:
        assert agy_mod.claim_resume("c1", "", "s", wait_s=0) is None
    finally:
        _drop(fh)
    assert _spool_lines(agy_home, "c1") == []


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
