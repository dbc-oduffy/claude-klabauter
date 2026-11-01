"""Tests for idle_compact.eligibility.decide: each skip, the inject path, re-arm, precompact-blocked."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coordinator_core.idle_compact import contract, eligibility

NOW = 10_000.0
SID = "sid-1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    (home / "idle-compact-allowlist.json").write_text(json.dumps(["repo-a"]), encoding="utf-8")
    hub = tmp_path / "hub"
    hub.mkdir()
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 100)
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()

    state = {"fire_turn": 3}
    ctx = {"epoch": NOW - 400, "pct": 25.0}
    monkeypatch.setattr(contract, "optout_marker_path", lambda sid, cwd=None: hub / "optout")
    monkeypatch.setattr(contract, "compact_hold_marker_path", lambda sid, cwd=None: hub / "hold")
    monkeypatch.setattr(eligibility.em_turn_count, "read_state", lambda r, s, t: state)
    monkeypatch.setattr(eligibility, "transcript_activity_epoch", lambda s, c: (ctx["epoch"], True))
    monkeypatch.setattr(
        eligibility.context_usage_sidecar,
        "read_usage",
        lambda sid, now, transcript_path=None: SimpleNamespace(
            context_window={"used_percentage": ctx["pct"]}, age_seconds=0
        ),
    )
    monkeypatch.setattr(eligibility, "_sentinel_dir", lambda: str(sentinels))
    record = SimpleNamespace(cwd=str(tmp_path / "repo-a"), name="repo-a-7")
    return SimpleNamespace(
        hub=hub, transcript=transcript, sentinels=sentinels, state=state, ctx=ctx,
        record=record, armed={}, home=home,
    )


def _decide(e):
    return eligibility.decide(SID, e.record, str(e.transcript), NOW, e.armed)


def test_eligible_injects(env):
    assert _decide(env) == eligibility.Decision("inject", "eligible")


def test_not_allowlisted_absent_file(env):
    (env.home / "idle-compact-allowlist.json").unlink()
    assert _decide(env).reason == "not-allowlisted"


def test_allowlist_matches_session_name(env):
    (env.home / "idle-compact-allowlist.json").write_text('["repo-a-7"]', encoding="utf-8")
    env.record.cwd = str(env.hub / "other")
    assert _decide(env).action == "inject"


def test_not_allowlisted_other_name(env):
    (env.home / "idle-compact-allowlist.json").write_text('["zzz"]', encoding="utf-8")
    assert _decide(env).reason == "not-allowlisted"


def test_opted_out(env):
    (env.hub / "optout").write_text("")
    assert _decide(env).reason == "opted-out"


def test_compact_hold(env):
    (env.hub / "hold").write_text("")
    assert _decide(env).reason == "compact-hold"


def test_no_engine_ask(env):
    env.state["fire_turn"] = None
    assert _decide(env).reason == "no-engine-ask"


def test_not_idle_boundary(env):
    env.ctx["epoch"] = NOW - 299
    assert _decide(env).reason == "not-idle"
    env.ctx["epoch"] = NOW - 300
    assert _decide(env).action == "inject"


def test_context_boundary_is_strictly_over(env):
    env.ctx["pct"] = 18.0
    assert _decide(env).reason == "under-threshold"
    env.ctx["pct"] = 18.1
    assert _decide(env).action == "inject"


def test_skip_order_allowlist_before_optout(env):
    (env.home / "idle-compact-allowlist.json").unlink()
    (env.hub / "optout").write_text("")
    assert _decide(env).reason == "not-allowlisted"


def test_already_injected_until_transcript_grows(env):
    eligibility.arm(env.armed, SID, 100)
    assert _decide(env).reason == "already-injected-this-stretch"
    env.transcript.write_bytes(b"x" * 150)
    (env.sentinels / f"compaction-occurred-{SID}").write_text("120\n")
    assert _decide(env).action == "inject"


def test_precompact_blocked_until_later_turn(env):
    eligibility.arm(env.armed, SID, 100)
    env.transcript.write_bytes(b"x" * 150)
    assert _decide(env).reason == "precompact-blocked"
    assert _decide(env).reason == "precompact-blocked"
    env.transcript.write_bytes(b"x" * 200)
    assert _decide(env).action == "inject"


def test_stale_sentinel_from_before_injection_does_not_count(env):
    eligibility.arm(env.armed, SID, 100)
    env.transcript.write_bytes(b"x" * 150)
    (env.sentinels / f"compaction-occurred-{SID}").write_text("50\n")
    assert _decide(env).reason == "precompact-blocked"
