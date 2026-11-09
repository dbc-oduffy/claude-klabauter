"""agent-worker-launch: refusals, branch checkout, env marker and exec argv, on a throwaway repo."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import worker_mode as wm

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("agent_worker_launch", BIN / "agent-worker-launch.py")
awl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(awl)

OK = wm.Preflight(True, "")


def _g(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _g(r, "init", "-q", "-b", "work/feature")
    _g(r, "config", "user.email", "t@t")
    _g(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a")
    _g(r, "add", "a.txt")
    _g(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def stub_wm(monkeypatch):
    class W:
        pass

    w = W()
    for name in ("WORKER_ENV", "WORKER_ARGV", "worker_branch", "is_protected_branch"):
        setattr(w, name, getattr(wm, name))
    w.preflight = lambda *a, **k: OK
    return w


def _run(repo, stub_wm, agent="codex", extra=(), which=lambda n: f"/bin/{n}", **kw):
    calls, env = [], {}
    rc = awl.launch(agent, list(extra), cwd=repo, wm=stub_wm, which=which, execv=lambda p, c: calls.append((p, c)),
                    host="Mac-Mini", today="2026-11-07", environ=env, **{"platform": "linux", **kw})
    return rc, calls, env


def test_clean_tree_checks_out_worker_branch_and_execs_argv(repo, stub_wm):
    rc, calls, env = _run(repo, stub_wm, extra=["-m", "x"])
    assert rc == 0
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/mac-mini/worker-codex-2026-11-07"
    assert calls == [("/bin/codex", ["/bin/codex", *wm.WORKER_ARGV["codex"], "-m", "x"])]
    assert env[wm.WORKER_ENV] == "codex"


def test_existing_worker_branch_is_reused(repo, stub_wm):
    _run(repo, stub_wm)
    _g(repo, "checkout", "-q", "work/feature")
    rc, calls, _ = _run(repo, stub_wm)
    assert rc == 0 and calls
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/mac-mini/worker-codex-2026-11-07"


def test_dirty_tree_is_refused_and_branch_unchanged(repo, stub_wm):
    (repo / "a.txt").write_text("peer work")
    rc, calls, env = _run(repo, stub_wm)
    assert rc == awl.REFUSED and calls == [] and wm.WORKER_ENV not in env
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/feature"


def test_untracked_file_counts_as_dirty(repo, stub_wm):
    (repo / "new.txt").write_text("x")
    assert _run(repo, stub_wm)[0] == awl.REFUSED


def test_main_branch_is_refused(repo, stub_wm):
    _g(repo, "checkout", "-q", "-b", "main")
    rc, calls, _ = _run(repo, stub_wm)
    assert rc == awl.REFUSED and calls == []
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_failed_preflight_is_refused_before_any_git_change(repo, stub_wm):
    stub_wm.preflight = lambda *a, **k: wm.Preflight(False, "box over capacity")
    rc, calls, _ = _run(repo, stub_wm)
    assert rc == awl.REFUSED and calls == []
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/feature"


def test_missing_cli_exits_127(repo, stub_wm):
    rc, calls, _ = _run(repo, stub_wm, which=lambda n: None)
    assert rc == 127 and calls == []


@pytest.mark.parametrize("agent", ["grok", "agy"])
def test_each_cli_gets_its_own_argv(repo, stub_wm, agent):
    rc, calls, _ = _run(repo, stub_wm, agent=agent)
    assert rc == 0 and calls[0][1] == [f"/bin/{agent}", *wm.WORKER_ARGV[agent]]


def test_session_and_run_reach_preflight(repo, stub_wm):
    seen = {}
    stub_wm.preflight = lambda agent, **k: seen.update(k) or OK
    _run(repo, stub_wm, session_id="s1", run="r1")
    assert seen["session_id"] == "s1" and seen["run"] == "r1"


def test_main_parses_agent_session_and_passthrough(repo, stub_wm):
    calls = []
    rc = awl.main(["codex", "--session-id", "s", "--run", "r", "--", "-m", "x"], cwd=repo, wm=stub_wm,
                  which=lambda n: "/bin/codex", execv=lambda p, c: calls.append(c), host="h", today="d", environ={}, platform="linux")
    assert rc == 0 and calls[0][-2:] == ["-m", "x"]


def _engine_repo(repo):
    marker = repo / "coordinator_core" / "agent_pipe" / "worker_mode.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "engine")


def test_engine_repo_is_refused_by_default_with_real_preflight(repo):
    _engine_repo(repo)
    calls, env = [], {}
    rc = awl.launch("codex", [], cwd=repo, which=lambda n: "/bin/codex", execv=lambda p, c: calls.append(c),
                    host="h", today="2026-11-07", environ=env)
    assert rc == awl.REFUSED and calls == [] and wm.WORKER_ENV not in env
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/feature"


def test_allow_engine_repo_override_drops_the_engine_start_dir(repo, stub_wm):
    seen = {}
    stub_wm.preflight = lambda agent, **k: seen.update(k) or OK
    _run(repo, stub_wm)
    assert seen["start_dir"] == str(repo)
    rc, calls, _ = _run(repo, stub_wm, allow_engine_repo=True)
    assert rc == 0 and calls and seen["start_dir"] is None


def test_main_parses_allow_engine_repo(monkeypatch):
    seen = {}
    monkeypatch.setattr(awl, "launch", lambda *a, **k: seen.update(k) or 0)
    assert awl.main(["codex", "--allow-engine-repo"]) == 0
    assert seen["allow_engine_repo"] is True
    assert awl.main(["codex"]) == 0 and seen["allow_engine_repo"] is False


def test_main_without_args_refuses():
    assert awl.main([]) == awl.REFUSED


def test_windows_python_cannot_be_interactive_parent(repo, stub_wm):
    rc, calls, env = _run(repo, stub_wm, platform="win32")
    assert rc == awl.REFUSED and not calls and not env
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/feature"


def test_windows_prepare_requires_native_executable(repo, stub_wm):
    rc, calls, env = _run(repo, stub_wm, platform="win32", prepare_only=True,
                          which=lambda n: "C:/bin/codex.cmd")
    assert rc == awl.REFUSED and not calls and not env
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work/feature"


def test_windows_prepare_emits_plan_without_executing(repo, stub_wm, capsys):
    import json
    rc, calls, env = _run(repo, stub_wm, platform="win32", prepare_only=True,
                          which=lambda n: "C:/bin/codex.exe")
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and not calls and not env
    assert plan["argv"] == ["C:/bin/codex.exe", *wm.WORKER_ARGV["codex"]]
    assert plan["environment"] == {wm.WORKER_ENV: "codex"}
