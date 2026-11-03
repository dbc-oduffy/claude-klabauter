"""publish.py sends `pre-land` before the final land and `landed` after it, to live subscribers.

Every test drives `publish_mod.main(["row-a"])` at default arguments through the real `_run_round`
and the real `land_diff`; delivery goes through the real land_notify registry and mailbox.

Run: python -m pytest coordinator/bin/tests/test_publish_land_notify.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish_quiesces_warm_pair as _q  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SID = "sess-example-game-repo-1"


def _arm(tmp_path, monkeypatch, *, live: bool, engine_root: bool = True):
    env = _q._setup(tmp_path, monkeypatch, engine_root)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    from coordinator_core.agent_pipe.contract import mailbox_path
    from coordinator_core.percolate import land_notify

    land_notify.subscribe(SID)
    roster = [type("Row", (), {"session_id": SID})()] if live else []
    monkeypatch.setattr(land_notify, "_default_roster", lambda: roster)

    real_append = land_notify.notify

    def spy(event, round_id, repo_names, build_roster=None):
        env.events.append(("notify:" + event, env.held()))
        return real_append(event, round_id, repo_names, build_roster)

    monkeypatch.setattr(land_notify, "notify", spy)
    return env, mailbox_path(SID)


def _lines(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_default_round_sends_pre_land_before_land_and_landed_after(tmp_path, monkeypatch):
    env, mailbox = _arm(tmp_path, monkeypatch, live=True)
    rc, err = _q._run(env)
    assert rc == 0, err
    order = [e[0] for e in env.events if e[0] in ("notify:pre-land", "land_diff", "notify:landed")]
    assert order == ["notify:pre-land", "land_diff", "notify:landed"]
    assert ("notify:pre-land", False) in env.events and ("notify:landed", False) in env.events
    msgs = _lines(mailbox)
    assert [m["content"].split()[1] for m in msgs] == ["pre-land", "landed"]
    assert all(m["to_session"] == SID and m["sender_agent"] == "coordinator-publish" for m in msgs)
    assert len({m["sender_conversation"] for m in msgs}) == 1


def test_non_live_subscriber_gets_nothing_and_publish_succeeds(tmp_path, monkeypatch):
    env, mailbox = _arm(tmp_path, monkeypatch, live=False)
    rc, err = _q._run(env)
    assert rc == 0, err
    assert _lines(mailbox) == []
    assert env.names("land_diff") == [("land_diff", True)]


def test_land_failure_still_sends_landed(tmp_path, monkeypatch):
    env, mailbox = _arm(tmp_path, monkeypatch, live=True)
    from percolate import diff_commit

    def boom(*a, **k):
        env.events.append(("land_diff", env.held()))
        raise RuntimeError("land blew up")

    monkeypatch.setattr(diff_commit, "land_diff", boom)
    rc, err = _q._run(env)
    assert rc != 0
    assert [m["content"].split()[1] for m in _lines(mailbox)] == ["pre-land", "landed"]


def test_failed_delivery_prints_one_stderr_line_and_keeps_exit_code(tmp_path, monkeypatch):
    env, _mailbox = _arm(tmp_path, monkeypatch, live=True)
    from coordinator_core.agent_pipe import mailbox as mb

    def broken(msg):
        raise OSError("disk full")

    monkeypatch.setattr(mb, "append", broken)
    rc, err = _q._run(env)
    assert rc == 0, err
    failed = [ln for ln in err.splitlines() if "land-notify" in ln and "failed" in ln]
    assert len(failed) == 2 and all(SID in ln and "disk full" in ln for ln in failed)


def test_notify_raising_never_fails_the_publish(tmp_path, monkeypatch):
    env, _mailbox = _arm(tmp_path, monkeypatch, live=True)
    from coordinator_core.percolate import land_notify

    def boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(land_notify, "notify", boom)
    rc, err = _q._run(env)
    assert rc == 0, err
    assert "registry exploded" in err
    assert env.names("land_diff") == [("land_diff", True)]


def test_no_notice_when_no_commit_runs(tmp_path, monkeypatch):
    env, mailbox = _arm(tmp_path, monkeypatch, live=True)
    publish_mod = env.publish_mod
    sent = []
    monkeypatch.setattr(publish_mod, "_notify_land_event", lambda *a, **k: sent.append(a))
    with publish_mod._land_notices(False, "publish-x", ["r"]):
        pass
    assert sent == [] and _lines(mailbox) == []
    with publish_mod._land_notices(True, "publish-x", ["r"]):
        pass
    assert [s[0] for s in sent] == ["pre-land", "landed"]


def test_no_commit_round_sends_no_notice(tmp_path, monkeypatch):
    import contextlib
    import io

    env, mailbox = _arm(tmp_path, monkeypatch, live=True)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = env.publish_mod.main(["row-a", "--no-commit"])
    assert rc == 0, err.getvalue()
    assert not [e for e in env.events if e[0].startswith("notify:")]
    assert _lines(mailbox) == []
