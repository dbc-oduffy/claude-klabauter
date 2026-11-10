"""headless-em-launch: an EM launch writes a grant that passes require_fire_authority; refusals write none."""
import importlib.util
import json
from pathlib import Path

import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.ops.workflow_fire import headless_em_grant as g

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("headless_em_launch", BIN / "headless-em-launch.py")
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)

_IDENTITY_VARS = ("COORDINATOR_SESSION_ID", "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID",
                  "COORDINATOR_AGENT_SESSION", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                  "COORDINATOR_AGENT_WORKER", "COORDINATOR_AGENT_ROLE", g.HEADLESS_EM_TOKEN_ENV)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name in _IDENTITY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(g.SETTINGS_HOME_KEY, str(tmp_path / "settings"))
    return tmp_path / "settings"


def _run(family="codex", env=None, **kw):
    calls, env = [], {} if env is None else env
    rc = hel.launch(family, list(kw.pop("extra", ())), which=lambda n: f"/bin/{n}",
                    execv=lambda p, c: calls.append((p, c)), environ=env,
                    **{"platform": "linux", **kw})
    return rc, calls, env


def _fire(monkeypatch, child_env):
    """Run require_fire_authority as the child: its env becomes the process env."""
    for k, v in child_env.items():
        monkeypatch.setenv(k, v)
    return authority.require_fire_authority("s.py", None, cwd=None, native=True, backend="codex")


@pytest.mark.parametrize("family", hel.FAMILIES)
def test_launched_em_passes_require_fire_authority(family, monkeypatch):
    rc, calls, env = _run(family, extra=["-m", "x"])
    assert rc == 0
    assert calls == [(f"/bin/{family}", [f"/bin/{family}", "-m", "x"])]
    assert g.headless_em_grant_matches(env, env[hel.SESSION_ENV], family)
    caller = _fire(monkeypatch, env)
    assert caller.session_id == env[hel.SESSION_ENV]


def test_explicit_session_id_wins_over_env_and_is_granted(monkeypatch):
    rc, _, env = _run(session_id="sess-explicit", env={hel.SESSION_ENV: "sess-env"})
    assert rc == 0 and env[hel.SESSION_ENV] == "sess-explicit"
    assert g.grant_path("sess-explicit").exists() and not g.grant_path("sess-env").exists()


def test_env_session_id_is_used_when_no_flag():
    _, _, env = _run(env={hel.SESSION_ENV: "sess-env"})
    assert env[hel.SESSION_ENV] == "sess-env" and g.grant_path("sess-env").exists()


def test_grant_for_other_session_does_not_admit(monkeypatch):
    _, _, env = _run(session_id="sess-a")
    env = {**env, hel.SESSION_ENV: "sess-b"}
    with pytest.raises(ValueError):
        _fire(monkeypatch, env)


@pytest.mark.parametrize("marker", [{"COORDINATOR_AGENT_WORKER": "1"}, {"COORDINATOR_AGENT_ROLE": "worker"}])
def test_worker_env_cannot_mint_a_grant(marker, home):
    rc, calls, env = _run(env=dict(marker))
    assert rc == hel.REFUSED and not calls
    assert g.HEADLESS_EM_TOKEN_ENV not in env and not (home / "state").exists()


def test_worker_marker_in_child_env_still_refuses_fire(monkeypatch):
    _, _, env = _run()
    with pytest.raises(ValueError):
        _fire(monkeypatch, {**env, "COORDINATOR_AGENT_WORKER": "1"})


def test_missing_cli_exits_127_and_writes_no_grant(home):
    calls, env = [], {}
    rc = hel.launch("codex", [], which=lambda n: None, execv=lambda *a: calls.append(a), environ=env,
                    platform="linux")
    assert rc == 127 and not calls and not (home / "state").exists()


def test_unknown_family_refused(home):
    rc, calls, _ = _run("claude")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_windows_exec_refused_without_grant(home):
    rc, calls, env = _run(platform="win32")
    assert rc == hel.REFUSED and not calls and not env and not (home / "state").exists()


def test_windows_prepare_emits_token_without_executing(capsys):
    calls, env = [], {}
    rc = hel.launch("codex", [], which=lambda n: None, execv=lambda *a: calls.append(a), environ=env,
                    platform="win32", prepare_only=True, native_executable="codex.exe")
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and not calls and not env
    assert plan["argv"] == ["codex.exe"]
    assert g.headless_em_grant_matches(plan["environment"], plan["session_id"], "codex")


def test_main_parses_family_session_and_passthrough(monkeypatch):
    seen = {}
    monkeypatch.setattr(hel, "launch", lambda family, extra, **k: seen.update(k, family=family, extra=extra) or 0)
    assert hel.main(["grok", "--session-id", "s1", "--prepare-only", "--", "-p", "hi"]) == 0
    assert seen["family"] == "grok" and seen["session_id"] == "s1" and seen["prepare_only"] is True
    assert seen["extra"] == ["-p", "hi"]


def test_main_without_args_refuses():
    assert hel.main([]) == hel.REFUSED
