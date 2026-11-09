"""worker_mode: argv table, branch naming, and the load + G-EM slot preflight."""
import pytest

from coordinator_core.agent_pipe import worker_mode as wm
from coordinator_core.ops.dispatch_emit import admission


def _thresholds():
    return admission.AdmissionThresholds(2.0, 10.0, 5.0, 1.0, {})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.delenv(admission.DISABLE_ENV, raising=False)
    monkeypatch.setattr(admission, "_load_thresholds_with_missing", lambda start: (_thresholds(), []))


def _quiet():
    return {"cpu_load": 0.1, "mem_avail_mb": 8000, "mem_total_mb": 16000}


def _busy():
    return {"cpu_load": 9.0, "mem_avail_mb": 8000, "mem_total_mb": 16000}


def test_argv_table_covers_the_three_clis_with_a_source_each():
    assert set(wm.WORKER_ARGV) == {"codex", "grok", "agy"} == set(wm.WORKER_ARGV_SOURCE)
    assert wm.WORKER_ARGV["codex"] == ("--sandbox", "workspace-write", "--ask-for-approval", "never")
    assert "danger-full-access" not in wm.WORKER_ARGV["codex"]


def test_branch_name_slugs_host_and_never_main():
    b = wm.worker_branch("codex", "Mac-Mini.local", "2026-11-07")
    assert b == "work/mac-mini-local/worker-codex-2026-11-07"
    assert not wm.is_protected_branch(b)
    assert wm.is_protected_branch("main") and wm.is_protected_branch("master")


@pytest.mark.parametrize("agent,host,date", [("nope", "h", "d"), ("codex", "", "d"), ("codex", "h", "!!")])
def test_branch_rejects_unknown_agent_or_empty_parts(agent, host, date):
    with pytest.raises(ValueError):
        wm.worker_branch(agent, host, date)


def test_unknown_agent_refused():
    assert not wm.preflight("nope").ok


def test_quiet_box_no_holder_is_ok(configured):
    r = wm.preflight("codex", read_load=_quiet, box_holder=lambda: None)
    assert r == (True, "")


def test_busy_box_refused_with_reason(configured):
    r = wm.preflight("grok", read_load=_busy, box_holder=lambda: None)
    assert not r.ok and "cpu_load" in r.reason


def test_unconfigured_admission_admits(monkeypatch):
    monkeypatch.delenv(admission.DISABLE_ENV, raising=False)
    monkeypatch.setattr(admission, "_load_thresholds_with_missing", lambda start: (None, ["cpu_load_max"]))
    assert wm.preflight("agy", read_load=_busy, box_holder=lambda: None).ok


def test_live_holder_requires_ids(configured):
    live = lambda: {"live": True}
    r = wm.preflight("codex", read_load=_quiet, box_holder=live)
    assert not r.ok and "slot grant" in r.reason


def test_live_holder_without_grant_refused_with_grant_ok(configured):
    live = lambda: {"live": True}
    denied = wm.preflight("codex", session_id="s1", run="r1", read_load=_quiet, box_holder=live,
                          slot_check=lambda s, r: (False, None))
    assert not denied.ok and "s1" in denied.reason
    seen = []
    granted = wm.preflight("codex", session_id="s1", run="r1", read_load=_quiet, box_holder=live,
                           slot_check=lambda s, r: (seen.append((s, r)) or True, {}))
    assert granted.ok and seen == [("s1", "r1")]


def test_not_live_holder_owes_no_slot(configured):
    assert wm.preflight("codex", read_load=_quiet, box_holder=lambda: {"live": False}).ok


def test_unreadable_holder_fails_closed(configured):
    def boom():
        raise OSError("x")

    r = wm.preflight("codex", read_load=_quiet, box_holder=boom)
    assert not r.ok and "unreadable" in r.reason


def test_engine_repo_start_dir_refused_other_dir_admitted(configured, tmp_path):
    engine = tmp_path / "eng"
    (engine / "coordinator_core" / "agent_pipe").mkdir(parents=True)
    (engine / "coordinator_core" / "agent_pipe" / "worker_mode.py").write_text("")
    sub = engine / "sub"
    sub.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    kw = dict(read_load=_quiet, box_holder=lambda: None)
    refused = wm.preflight("codex", start_dir=sub, **kw)
    assert not refused.ok and "engine repo" in refused.reason
    assert wm.preflight("codex", start_dir=other, **kw).ok
