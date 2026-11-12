from __future__ import annotations

import pytest

from coordinator_core.hooks import session_start_watch_presence as mod
from coordinator_core.ipc import _REGISTRY


def test_op_registered():
    assert "hooks.session_start_watch_presence" in _REGISTRY


def test_handler_silent_when_nothing_to_report(monkeypatch):
    monkeypatch.setattr(mod, "compute_context", lambda payload: None)
    result = mod._handler({})
    assert result.get("hookSpecificOutput", {}).get("additionalContext") is None


def test_handler_returns_additional_context(monkeypatch):
    monkeypatch.setattr(mod, "compute_context", lambda payload: "GROUP EM WATCH: armed")
    result = mod._handler({})
    assert "GROUP EM WATCH: armed" in result["hookSpecificOutput"]["additionalContext"]
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_render_watch_line():
    assert mod.render_watch_line({"verdict": "armed"}) == "GROUP EM WATCH: armed"
    assert mod.render_watch_line({}) is None


def test_render_presence_line_named_holder():
    line = mod.render_presence_line({"holder_name": "em-alpha", "holder_session_id": "sid1"})
    assert line is not None and "em-alpha" in line


def test_render_presence_line_absent():
    assert mod.render_presence_line({"verdict": "absent"}) is None


def _stamp_heartbeat(root, sid, now, ahead=300):
    import json

    from coordinator_core.group_em import watch_heartbeat as hb

    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "group-em-watch.json").write_text(
        json.dumps(
            {
                "holder_session_id": sid,
                "holder_name": "stamped-name",
                "last_tick_at": hb.iso_instant(now),
                "tick_source": "monitor",
                "next_expected_by": hb.iso_instant(now + ahead),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _no_real_roster(monkeypatch):
    monkeypatch.setattr(mod.nomination, "fleet_holder", lambda *a, **k: None)


_REMOTE = {
    "session_id": "64ee648d-remote",
    "peer_name": "group-em-56",
    "live": True,
    "remote": True,
    "box_id": "box-win",
}


def test_stale_local_holder_with_live_remote_gem_names_the_remote_gem(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stale = "1152426d-cb66-46bb-9a41-6de53e4f3a70"
    _box(monkeypatch, home, sid=stale, live=False)
    monkeypatch.setattr(mod.nomination, "fleet_holder", lambda *a, **k: dict(_REMOTE))
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "group-em-56" in out and "box-win" in out
    assert "vacant" not in out.lower() and stale not in out


def test_no_local_holder_with_live_remote_gem_names_the_remote_gem(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.nomination, "box_holder", lambda *a, **k: None)
    monkeypatch.setattr(mod.nomination, "fleet_holder", lambda *a, **k: dict(_REMOTE))
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "group-em-56" in out and "GROUP EM WATCH: UNKNOWN" in out


def test_live_local_holder_is_not_overridden_by_the_roster(monkeypatch, tmp_path):
    import time

    home = tmp_path / "home"
    _stamp_heartbeat(home, "sid-live", time.time())
    _box(monkeypatch, home)
    monkeypatch.setattr(mod.nomination, "fleet_holder", lambda *a, **k: dict(_REMOTE))
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "group-em-ad" in out and "GROUP EM WATCH: armed" in out and "group-em-56" not in out


def test_local_fleet_holder_hit_is_not_treated_as_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.nomination, "box_holder", lambda *a, **k: None)
    monkeypatch.setattr(mod.nomination, "fleet_holder", lambda *a, **k: {**_REMOTE, "remote": False})
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "no live holder on record" in out and "group-em-56" not in out


def _box(monkeypatch, root, sid="sid-live", live=True):
    monkeypatch.setattr(
        mod.nomination,
        "box_holder",
        lambda *a, **k: {"repo_root": str(root), "session_id": sid, "peer_name": "group-em-ad", "live": live},
    )


def test_foreign_cwd_reads_the_gem_home_heartbeat(monkeypatch, tmp_path):
    import time

    home, foreign = tmp_path / "home", tmp_path / "foreign"
    foreign.mkdir()
    _stamp_heartbeat(home, "sid-live", time.time())
    _box(monkeypatch, home)
    from_home = mod.compute_context({"cwd": str(home)})
    from_foreign = mod.compute_context({"cwd": str(foreign)})
    assert from_home == from_foreign
    assert "group-em-ad" in from_foreign and "GROUP EM WATCH: armed" in from_foreign


def test_missing_heartbeat_is_unknown_never_vacant(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _box(monkeypatch, home)
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "GROUP EM WATCH: UNKNOWN" in out and "vacant" not in out.lower()


def test_expired_heartbeat_is_unknown_not_armed(monkeypatch, tmp_path):
    import time

    home = tmp_path / "home"
    _stamp_heartbeat(home, "sid-live", time.time() - 4 * 7 * 86400, ahead=60)
    _box(monkeypatch, home)
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "UNKNOWN" in out and "armed" not in out


def test_heartbeat_of_another_holder_is_unknown(monkeypatch, tmp_path):
    import time

    home = tmp_path / "home"
    _stamp_heartbeat(home, "sid-old", time.time())
    _box(monkeypatch, home)
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "UNKNOWN" in out and "armed" not in out


def test_stale_record_holder_names_live_box_holder_never_vacant(monkeypatch, tmp_path):
    import time

    home = tmp_path / "home"
    stale = "1152426d-cb66-46bb-9a41-6de53e4f3a70"
    _stamp_heartbeat(home, stale, time.time() - 99999, ahead=60)
    _box(monkeypatch, home, sid="41d000-live")
    result = mod._handler({"cwd": str(tmp_path)})
    out = result["hookSpecificOutput"]["additionalContext"]
    assert "group-em-ad" in out and "vacant" not in out.lower()
    assert stale not in out


def test_dead_holder_is_unknown_and_unnamed(monkeypatch, tmp_path):
    import time

    home = tmp_path / "home"
    _stamp_heartbeat(home, "sid-live", time.time())
    _box(monkeypatch, home, live=False)
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "UNKNOWN" in out and "armed" not in out and "group-em-ad" not in out


def test_no_box_holder_stale_cwd_heartbeat_is_unknown(monkeypatch, tmp_path):
    import time

    monkeypatch.setattr(mod.nomination, "box_holder", lambda *a, **k: None)
    _stamp_heartbeat(tmp_path, "sid-x", time.time() - 99999, ahead=60)
    out = mod.compute_context({"cwd": str(tmp_path)})
    assert "UNKNOWN" in out and "armed" not in out


def test_render_uhura_line_none_record():
    assert mod.render_uhura_line(None) is None


def test_render_uhura_line_named_peer():
    line = mod.render_uhura_line({"peer_name": "uhura-1", "session_id": "sidX"})
    assert line == (
        "Uhura channel: uhura-1. Its relayed PM rulings carry the PM's "
        "authority -- act, no round trip. Unproven live: if silent, treat unheld."
    )
