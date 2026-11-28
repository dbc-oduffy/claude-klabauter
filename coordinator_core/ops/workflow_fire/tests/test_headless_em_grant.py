import json

import pytest

from coordinator_core.ops.workflow_fire import headless_em_grant as g


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(g.SETTINGS_HOME_KEY, str(tmp_path / "settings"))
    return tmp_path / "settings"


@pytest.mark.parametrize("family", g.HEADLESS_EM_FAMILIES)
def test_matching_token_verifies(family):
    token = g.write_headless_em_grant("sess-1", family)
    env = {g.HEADLESS_EM_TOKEN_ENV: token}
    assert g.headless_em_grant_matches(env, "sess-1")
    assert g.headless_em_grant_matches(env, "sess-1", family)


def test_family_mismatch_fails():
    token = g.write_headless_em_grant("sess-1", "codex")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "sess-1", "grok")


def test_wrong_token_other_session_absent_and_empty_fail():
    token = g.write_headless_em_grant("sess-1", "codex")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: "x" * 64}, "sess-1")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "sess-2")
    assert not g.headless_em_grant_matches({}, "sess-1")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: ""}, "sess-1")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "")
    assert not g.headless_em_grant_matches(None, "sess-1")


def test_unrelated_env_flags_never_verify():
    g.write_headless_em_grant("sess-1", "codex")
    env = {"COORDINATOR_AGENT_ROLE": "em", "COORDINATOR_AGENT_VENDOR": "codex"}
    assert not g.headless_em_grant_matches(env, "sess-1")


def test_unknown_family_and_blank_session_refused_by_writer():
    with pytest.raises(g.HeadlessEmGrantError):
        g.write_headless_em_grant("sess-1", "gemini")
    with pytest.raises(g.HeadlessEmGrantError):
        g.write_headless_em_grant("  ", "codex")


def test_grant_stores_digest_not_token(home):
    token = g.write_headless_em_grant("sess-1", "agy")
    path = g.grant_path("sess-1")
    assert path.is_relative_to(home)
    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert json.loads(text)["token_sha256"] == g.token_digest(token)


def test_tampered_family_in_grant_fails(home):
    token = g.write_headless_em_grant("sess-1", "codex")
    path = g.grant_path("sess-1")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["family"] = "claude"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "sess-1")


def test_malformed_grant_file_fails(home):
    token = g.write_headless_em_grant("sess-1", "codex")
    g.grant_path("sess-1").write_text("{not json", encoding="utf-8")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "sess-1")


def test_regrant_replaces_earlier_token():
    first = g.write_headless_em_grant("sess-1", "codex")
    second = g.write_headless_em_grant("sess-1", "codex")
    assert not g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: first}, "sess-1")
    assert g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: second}, "sess-1")


def test_unresolved_home_is_typed_refusal_naming_key(monkeypatch):
    monkeypatch.delenv(g.SETTINGS_HOME_KEY)

    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(g.Path, "home", staticmethod(boom))
    with pytest.raises(g.HeadlessEmGrantError, match=g.SETTINGS_HOME_KEY):
        g.write_headless_em_grant("sess-1", "codex")


def test_launch_thread_requires_matching_live_registration(tmp_path, monkeypatch):
    import os
    import uuid
    import psutil
    from coordinator_core.session import external_registry

    sid = "launcher"
    thread = str(uuid.uuid4())
    other = str(uuid.uuid4())
    env = {g.HEADLESS_EM_TOKEN_ENV: g.write_headless_em_grant(sid, "codex"),
           "COORDINATOR_SESSION_ID": sid, "CODEX_THREAD_ID": thread}
    child = psutil.Process(os.getpid())
    row = dict(session_id=thread, role="em", provider="codex", pid=child.pid,
               start_epoch=int(child.create_time()), cwd=str(tmp_path))
    monkeypatch.setattr(external_registry, "lookup", lambda key: dict(row, session_id=key))
    assert g.observe_codex_registration(env, thread, child.pid, str(tmp_path))
    assert g.registered_launch_thread(env, sid, str(tmp_path)) == thread
    assert not g.registered_launch_thread(dict(env, CODEX_THREAD_ID=other), sid, str(tmp_path))
    assert not g.registered_launch_thread(dict(env, COORDINATOR_AGENT_WORKER="1"), sid, str(tmp_path))
    assert not g.registered_launch_thread(env, sid, str(tmp_path.parent))
    row["start_epoch"] -= 1
    assert not g.registered_launch_thread(env, sid, str(tmp_path))
