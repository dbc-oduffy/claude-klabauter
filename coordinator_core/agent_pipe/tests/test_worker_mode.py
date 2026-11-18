"""worker_mode: argv table, branch naming, and the load + G-EM slot preflight."""
import pytest

from coordinator_core._settings_home import settings_home
from coordinator_core.agent_pipe import worker_mode as wm
from coordinator_core.ops.dispatch_emit import admission
from pathlib import Path


def _thresholds():
    return admission.AdmissionThresholds(2.0, 10.0, 5.0, 1.0, {})


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(admission.DISABLE_ENV, raising=False)
    monkeypatch.setattr(admission, "_load_thresholds_with_missing", lambda start: (_thresholds(), []))


def _quiet() -> dict[str, float]:
    return {"cpu_load": 0.1, "mem_avail_mb": 8000, "mem_total_mb": 16000}


def _busy() -> dict[str, float]:
    return {"cpu_load": 9.0, "mem_avail_mb": 8000, "mem_total_mb": 16000}


def test_argv_table_covers_the_three_clis_with_a_source_each() -> None:
    assert set(wm.WORKER_ARGV) == {"codex", "grok", "agy"} == set(wm.WORKER_ARGV_SOURCE)
    assert wm.WORKER_ARGV["codex"] == (
        "--sandbox", "workspace-write", "--ask-for-approval", "never", "--add-dir", str(settings_home()),
        *wm.lock_and_gitdir_add_dir_argv(),
    )
    assert "danger-full-access" not in wm.WORKER_ARGV["codex"]
    assert wm.WORKER_ARGV["grok"] == ("--always-approve",) and "--add-dir" not in wm.WORKER_ARGV["agy"]


def test_codex_worker_settings_home_root_follows_the_resolver(tmp_path, monkeypatch):
    home = tmp_path / "sh"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    assert ("--add-dir", str(settings_home())) == wm.WORKER_ARGV["codex"][4:6]
    assert wm.settings_home_add_dir_argv() == ["--add-dir", str(settings_home())]


def test_lock_and_gitdir_roots_follow_env_override_and_repo(tmp_path, monkeypatch):
    locks = tmp_path / "lockroot"
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(locks))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    argv = wm.lock_and_gitdir_add_dir_argv(repo)
    assert argv[:2] == ["--add-dir", str(locks / "coordinator-locks")]
    assert argv[2] == "--add-dir" and argv[3].endswith(".git") and len(argv) == 4


def test_lock_and_gitdir_roots_omit_git_dir_outside_a_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "lr"))
    bare = tmp_path / "plain"
    bare.mkdir()
    if any((p / ".git").exists() for p in (bare, *bare.parents)):
        pytest.skip("tmp_path sits inside a git repo")
    assert wm.lock_and_gitdir_add_dir_argv(bare) == ["--add-dir", str(tmp_path / "lr" / "coordinator-locks")]


def test_branch_name_slugs_host_and_never_main(monkeypatch) -> None:
    monkeypatch.setenv("COORDINATOR_BRANCH_OWNER", "alice")
    b = wm.worker_branch("codex", "Mac-Mini.local", "2026-11-07")
    assert b == "alice/mac-mini-local/worker-codex-2026-11-07"
    assert not wm.is_protected_branch(b)
    assert wm.is_protected_branch("main") and wm.is_protected_branch("master")


@pytest.mark.parametrize("agent,host,date", [("nope", "h", "d"), ("codex", "", "d"), ("codex", "h", "!!")])
def test_branch_rejects_unknown_agent_or_empty_parts(agent, host, date) -> None:
    with pytest.raises(ValueError):
        wm.worker_branch(agent, host, date)


def test_unknown_agent_refused() -> None:
    assert not wm.preflight("nope").ok


def test_quiet_box_no_holder_is_ok(configured) -> None:
    r = wm.preflight("codex", read_load=_quiet, box_holder=lambda: None)
    assert r == (True, "")


def test_busy_box_refused_with_reason(configured) -> None:
    r = wm.preflight("grok", read_load=_busy, box_holder=lambda: None)
    assert not r.ok and "cpu_load" in r.reason


def test_unconfigured_admission_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(admission.DISABLE_ENV, raising=False)
    monkeypatch.setattr(admission, "_load_thresholds_with_missing", lambda start: (None, ["cpu_load_max"]))
    assert wm.preflight("agy", read_load=_busy, box_holder=lambda: None).ok


def test_live_holder_requires_ids(configured) -> None:
    live = lambda: {"live": True}
    r = wm.preflight("codex", read_load=_quiet, box_holder=live)
    assert not r.ok and "slot grant" in r.reason


def test_live_holder_without_grant_refused_with_grant_ok(configured) -> None:
    live = lambda: {"live": True}
    denied = wm.preflight("codex", session_id="s1", run="r1", read_load=_quiet, box_holder=live,
                          slot_check=lambda s, r: (False, None))
    assert not denied.ok and "s1" in denied.reason
    seen = []
    granted = wm.preflight("codex", session_id="s1", run="r1", read_load=_quiet, box_holder=live,
                           slot_check=lambda s, r: (seen.append((s, r)) or True, {}))
    assert granted.ok and seen == [("s1", "r1")]


def test_not_live_holder_owes_no_slot(configured) -> None:
    assert wm.preflight("codex", read_load=_quiet, box_holder=lambda: {"live": False}).ok


def test_unreadable_holder_fails_closed(configured) -> None:
    def boom():
        raise OSError("x")

    r = wm.preflight("codex", read_load=_quiet, box_holder=boom)
    assert not r.ok and "unreadable" in r.reason


def test_engine_repo_start_dir_refused_other_dir_admitted(configured, tmp_path: Path) -> None:
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
