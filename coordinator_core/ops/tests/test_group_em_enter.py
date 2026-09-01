"""Tests for `groupem.enter` -- the composed MUTATING entry op.

Spec backlink: docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md § C5
"""

from __future__ import annotations

import time
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_enter as gee
from coordinator_core.op_scopes import OP_KEY_SCOPE


def test_payload_has_exactly_seven_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-1")
    monkeypatch.setattr(
        gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: []
    )
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-1", "superseded_incumbent": None},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert set(result.keys()) == {
        "nomination", "roster", "roster_considered", "digest", "baseline", "teammates",
        "watch_liveness"
    }


def test_mutating_classification_registered():
    assert OP_CLASSIFICATION["groupem.enter"] is OpClass.MUTATING


def test_all_four_registration_points_resolve():
    assert "groupem.enter" in _registry_map.OP_MODULE_MAP
    assert _registry_map.OP_MODULE_MAP["groupem.enter"] == "coordinator_core.ops.group_em_enter"
    assert OP_KEY_SCOPE["groupem.enter"] == "none"
    assert OP_CLASSIFICATION["groupem.enter"] is OpClass.MUTATING
    assert ipc._REGISTRY.get("groupem.enter") is not None


def test_each_leg_degrades_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-2")
    monkeypatch.setattr(
        gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: []
    )
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )

    def _boom(*a, **k):
        raise RuntimeError("nomination boom")

    monkeypatch.setattr(gee.group_em_nomination, "claim", _boom)

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"] is None
    assert "nomination_error" in result
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}
    assert result["baseline"] == {
        "spawned": [],
        "exited": [],
        "changed": [],
        "first_tick": True,
    }


def test_roster_failure_degrades_digest_but_not_baseline(tmp_path, monkeypatch):
    """Digest cascades from the roster; baseline does NOT.

    Baseline consumes the peer ENUMERATION, not the classified roster, so a
    classifier failure must not blind the spawn/exit diff. Until 2026-08-30
    baseline was fed the roster and this test asserted it cascaded too --
    which is what let a roster that oscillated 0 -> 3 -> 0 be reported as
    three spawns followed by three exits when only one session had left.
    """
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-3")

    def _boom(*a, **k):
        raise RuntimeError("roster boom")

    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", _boom)
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-3", "superseded_incumbent": None},
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["roster"] is None
    assert "roster_error" in result
    assert result["digest"] is None
    assert result["digest_error"] == "roster-leg-failed"
    assert result["baseline"] is not None
    assert "baseline_error" not in result
    assert result["nomination"]["claimed"] is True


def test_baseline_tracks_the_peer_set_not_the_candidate_roster(tmp_path, monkeypatch):
    """A peer that stops being a nudge candidate has not exited.

    The regression this pins: `_run_baseline` used to build `current_peers`
    from the roster, so a peer dropping out of the PAUSED candidate set --
    by resuming work, or by the classifier failing to reach a verdict --
    was reported under `exited`.
    """
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-4")
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-4", "superseded_incumbent": None},
    )

    agents = [
        {"sessionId": "peer-busy", "status": "busy", "cwd": str(tmp_path)},
        {"sessionId": "peer-idle", "status": "idle", "cwd": str(tmp_path)},
    ]
    monkeypatch.setattr(gee.group_em_read_pass, "fetch_live_agents", lambda *a, **k: agents)
    # Roster admits ONLY the idle peer as a candidate; the busy one is not a
    # candidate but is emphatically still present.
    monkeypatch.setattr(
        gee.group_em_read_pass,
        "build_candidate_roster",
        lambda *a, **k: [{"session_id": "peer-idle", "state": "PAUSED", "candidate": True}],
    )

    first = gee._group_em_enter({"repo_root": str(tmp_path)})
    assert first["baseline"]["first_tick"] is True

    # Second tick: the idle peer picks work back up. It leaves the roster, but
    # it has NOT exited -- and its state transition is what the diff reports.
    agents[1]["status"] = "busy"
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])

    second = gee._group_em_enter({"repo_root": str(tmp_path)})
    assert second["baseline"]["exited"] == []
    assert second["baseline"]["spawned"] == []
    assert second["baseline"]["changed"] == ["peer-idle"]


def test_live_incumbent_refusal_stops_before_digest(tmp_path, monkeypatch):
    """The load-bearing assertion: a REFUSED Group-EM (live incumbent) must never call
    `build_send_digest` -- the bug is the side effect (cooldown arming), not the return
    shape. Spy on the digest builder rather than only checking its absence in the
    payload."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-7")

    roster_spy_calls: list = []
    digest_spy_calls: list = []
    baseline_spy_calls: list = []

    monkeypatch.setattr(
        gee.group_em_read_pass,
        "build_candidate_roster",
        lambda *a, **k: roster_spy_calls.append((a, k)) or [],
    )
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: digest_spy_calls.append((a, k)) or {"entries": []},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: baseline_spy_calls.append((a, k)) or {"first_tick": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": False,
            "holder": "incumbent-sid",
            "already_held": False,
            "superseded_incumbent": {
                "session_id": "incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-30T00:00:00Z",
                "nominated_by": "someone",
                "live": True,
                "live_reason": "live",
            },
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert digest_spy_calls == [], "build_send_digest must NEVER be called on a refused Group-EM"
    assert roster_spy_calls == [], "roster leg must not be built on a refused Group-EM either"
    assert baseline_spy_calls == []

    assert result["nomination"]["claimed"] is False
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["superseded_incumbent"]["live"] is True
    assert result["nomination"]["superseded_incumbent"]["live_reason"] == "live"

    # ABSENT, not null: an empty roster is a fact ("looked, found nobody"); an
    # absent one means "had no standing to look". Assert via `not in`, never
    # via `is None`, or this regresses to the exact bug the constraint exists
    # to prevent.
    assert "roster" not in result
    assert "digest" not in result
    assert "baseline" not in result
    assert "roster_error" not in result
    assert "digest_error" not in result
    assert "baseline_error" not in result


def test_unaccounted_incumbent_refusal_also_stops_before_digest(tmp_path, monkeypatch):
    """An incumbent with only ABSENCE of registry evidence (`no_registry_record`) is
    still a refusal -- this fleet is multi-machine, so no registry row for that
    session_id does not mean it is gone, only that we cannot see it from here. Same
    refusal shape as a live incumbent: roster/digest/baseline absent, no digest call.
    (Contrast `test_pid_not_running_incumbent_is_auto_replaced_not_refused` below --
    `pid_not_running` is POSITIVE evidence of death and is NOT this case.)"""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-8")

    digest_spy_calls: list = []
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: digest_spy_calls.append((a, k)) or {"entries": []},
    )
    monkeypatch.setattr(
        gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: []
    )
    monkeypatch.setattr(
        gee.group_em_baseline, "diff_and_persist", lambda *a, **k: {"first_tick": True}
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": False,
            "holder": "unaccounted-incumbent-sid",
            "already_held": False,
            "superseded_incumbent": {
                "session_id": "unaccounted-incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-29T00:00:00Z",
                "nominated_by": "someone",
                "live": False,
                "live_reason": "no_registry_record",
            },
            "replaced_holder": None,
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert digest_spy_calls == [], "absence of registry evidence must not auto-yield the Group-EM"
    assert result["nomination"]["claimed"] is False
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["superseded_incumbent"]["live"] is False
    assert result["nomination"]["superseded_incumbent"]["live_reason"] == "no_registry_record"
    assert result["nomination"]["replaced_holder"] is None
    assert "roster" not in result
    assert "digest" not in result
    assert "baseline" not in result
    assert "roster_error" not in result
    assert "digest_error" not in result
    assert "baseline_error" not in result


def test_pid_not_running_incumbent_is_auto_replaced_not_refused(tmp_path, monkeypatch):
    """AUTO-REPLACE: `live_reason == "pid_not_running"` is POSITIVE evidence of death
    (a registry row exists for the incumbent and its pid is confirmed not running).
    `claimed` is True, roster/digest/baseline all run, and the replaced holder is named
    in its OWN `replaced_holder` field -- never folded into `superseded_incumbent`,
    whose presence would wrongly signal a refusal. A test that only checked
    `claimed is True` would pass a silent replacement; assert `replaced_holder` is
    populated and distinct from `superseded_incumbent` (which must be None here)."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-11")

    digest_spy_calls: list = []
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: digest_spy_calls.append((a, k))
        or {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": False},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-11",
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": {
                "session_id": "dead-incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-29T00:00:00Z",
                "nominated_by": "someone",
                "live": False,
                "live_reason": "pid_not_running",
            },
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["superseded_incumbent"] is None
    replaced = result["nomination"]["replaced_holder"]
    assert replaced is not None, "a silent replacement must not pass this test"
    assert replaced != result["nomination"]["superseded_incumbent"]
    assert replaced["session_id"] == "dead-incumbent-sid"
    assert replaced["live_reason"] == "pid_not_running"

    # Group-EM was successfully claimed -- roster/digest/baseline all run, keys present.
    assert digest_spy_calls != [], "an auto-replace must proceed to build the digest"
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}
    assert result["baseline"]["first_tick"] is False
    assert "roster_error" not in result
    assert "digest_error" not in result
    assert "baseline_error" not in result


def test_successful_claim_still_returns_all_five_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-9")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-9",
            "already_held": False,
            "superseded_incumbent": None,
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert set(result.keys()) >= {
        "nomination", "roster", "roster_considered", "digest", "baseline", "teammates",
        "watch_liveness"
    }
    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is False
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}
    assert result["baseline"]["first_tick"] is True


def test_reentry_by_holder_is_distinguishable_from_fresh_claim(tmp_path, monkeypatch):
    """CONSTRAINT A: `already_held` must be surfaced verbatim, not collapsed. A fresh
    claim and a refreshed re-entry by the same holder are two different lines to a
    human operator and must be distinguishable from the payload alone."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-10")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": False},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-10",
            "already_held": True,
            "superseded_incumbent": None,
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is True
    assert result["nomination"]["superseded_incumbent"] is None
    assert result["roster"] == []


def test_auto_replace_group_em_is_not_a_refusal_and_runs_roster(tmp_path, monkeypatch):
    """`replaced_holder` (case 4 -- pid_not_running) is NOT a refusal: `claimed` is True,
    so roster/digest/baseline must all run, unlike the two refusal cases above."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-11")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": False},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-11",
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": {
                "session_id": "dead-incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-29T00:00:00Z",
                "nominated_by": "someone",
                "live": False,
                "live_reason": "pid_not_running",
            },
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["replaced_holder"]["session_id"] == "dead-incumbent-sid"
    assert result["nomination"]["replaced_holder"]["live_reason"] == "pid_not_running"
    # Not a refusal -- roster/digest/baseline all ran, none absent.
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": False}
    assert result["baseline"]["first_tick"] is True


def test_baseline_leg_writes_under_the_acted_on_repo_root_not_claude_klabauter(tmp_path, monkeypatch):
    """Regression for the P1: `_run_baseline` used to call `diff_and_persist`
    without a `repo_root`, so its default (`baseline._repo_root()` == the
    claude-klabauter checkout) swallowed every `groupem.enter` call against another
    repo. This test deliberately does NOT monkeypatch `diff_and_persist` --
    it exercises the real function, over a real `tmp_path` `repo_root`, and
    asserts the baseline file lands under THAT root."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-6")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": False},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-6", "superseded_incumbent": None},
    )

    claude_klabauter_root = Path(__file__).resolve().parents[3]
    claude_klabauter_store_glob = list(
        (claude_klabauter_root / "state" / "subagent-share" / "caller-sid-6").glob(
            "group-em-baseline-*.json"
        )
    )
    assert not claude_klabauter_store_glob, "pre-existing stray fixture would corrupt this assertion"

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["baseline"] is not None
    assert result.get("baseline_error") is None
    assert result["baseline"]["first_tick"] is True

    target_dir = tmp_path / "state" / "subagent-share" / "caller-sid-6"
    written = list(target_dir.glob("group-em-baseline-*.json"))
    assert len(written) == 1, "baseline snapshot must land under the acted-on repo_root"

    claude_klabauter_store_after = list(
        (claude_klabauter_root / "state" / "subagent-share" / "caller-sid-6").glob(
            "group-em-baseline-*.json"
        )
    )
    assert not claude_klabauter_store_after, "baseline snapshot must NOT land under the claude-klabauter checkout"


def _group_em_with_teammates(tmp_path, monkeypatch, metas, session_id):
    """Plant `metas` as `.meta.json` sidecars in a fake home's subagents dir for
    `session_id`, and return the repo root to enter with."""
    import json

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    repo_root = str(tmp_path / "repo")
    # The repo root has to EXIST: `watch_heartbeat.stamp` refuses to mint one
    # (a writer that conjures a repo tree put a stray directory inside a publish
    # mirror on 2026-09-01), so a test that stamps into a path nothing created
    # is testing the refusal, not the leg.
    Path(repo_root).mkdir(parents=True, exist_ok=True)
    directory = Path(gee.group_em_teammates.subagents_dir(repo_root, session_id))
    directory.mkdir(parents=True, exist_ok=True)
    for index, meta in enumerate(metas):
        (directory / f"agent-astub{index}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
    return repo_root


def _stub_legs(monkeypatch, session_id, claimed=True):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: session_id)
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": claimed,
            "holder": session_id if claimed else "someone-else",
            "already_held": False,
            "superseded_incumbent": None if claimed else {"live_reason": "live"},
        },
    )


def test_teammates_leg_reports_both_agents_present(tmp_path, monkeypatch):
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _stub_legs(monkeypatch, session_id)
    repo_root = _group_em_with_teammates(
        tmp_path,
        monkeypatch,
        [
            {"agentType": "coordinator:group-em-assistant", "name": "gem-assistant"},
            {"agentType": "general-purpose", "name": "fleet-watch"},
        ],
        session_id,
    )

    result = gee._group_em_enter({"repo_root": repo_root})

    assert result["teammates"]["dispatch_required"] is False
    assert result["teammates"]["missing"] == []
    assert "teammates_error" not in result


def test_teammates_leg_reports_a_group_em_holding_neither_agent(tmp_path, monkeypatch):
    """The regression this op exists to end: a Group-EM that skipped the dispatch
    used to produce no error, no warning, and no record. It now carries an
    unmet obligation on every tick, with the fleet watcher named first."""
    session_id = "aaaaaaaa-bbbb-cccc-dddd-ffffffffffff"
    _stub_legs(monkeypatch, session_id)
    repo_root = _group_em_with_teammates(
        tmp_path, monkeypatch, [{"agentType": "coordinator:staff-eng"}], session_id
    )

    result = gee._group_em_enter({"repo_root": repo_root})

    assert result["teammates"]["dispatch_required"] is True
    assert result["teammates"]["missing"] == ["fleet_watch", "group_em_assistant"]
    assert result["teammates"]["agents"]["fleet_watch"]["present"] is False
    assert result["teammates"]["agents"]["group_em_assistant"]["present"] is False


def test_teammates_leg_reports_the_watcher_missing_on_its_own(tmp_path, monkeypatch):
    session_id = "aaaaaaaa-bbbb-cccc-dddd-999999999999"
    _stub_legs(monkeypatch, session_id)
    repo_root = _group_em_with_teammates(
        tmp_path,
        monkeypatch,
        [{"agentType": "coordinator:group-em-assistant", "name": "gem-assistant"}],
        session_id,
    )

    result = gee._group_em_enter({"repo_root": repo_root})

    assert result["teammates"]["missing"] == ["fleet_watch"]
    assert result["teammates"]["agents"]["group_em_assistant"]["present"] is True


def test_teammates_absent_entirely_on_a_refused_group_em(tmp_path, monkeypatch):
    """A session with no standing to hold the Group-EM owes no teammates -- the key
    is OMITTED, never reported as an unmet obligation it does not carry."""
    session_id = "aaaaaaaa-bbbb-cccc-dddd-777777777777"
    _stub_legs(monkeypatch, session_id, claimed=False)

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert "teammates" not in result
    assert "teammates_error" not in result


def test_teammates_leg_degrades_without_taking_the_others(tmp_path, monkeypatch):
    session_id = "aaaaaaaa-bbbb-cccc-dddd-888888888888"
    _stub_legs(monkeypatch, session_id)

    def _boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(gee.group_em_teammates, "presence", _boom)

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["teammates"] is None
    assert result["teammates_error"] == "RuntimeError: probe exploded"
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}


# --- watch_liveness: dispatched is not ticking -----------------------------


def test_watch_liveness_reports_absent_when_nothing_ever_stamped(tmp_path, monkeypatch):
    """The live failure this leg exists for, from the outside: the Group-EM holds a
    dispatch record for a watcher whose subprocess never started, so the
    teammates leg is satisfied and nothing is watching. Measured 2026-09-01 in
    example-game-workbench-repo -- `ListAgents` read `idle` for thirteen minutes."""
    session_id = "aaaaaaaa-bbbb-cccc-dddd-111111111111"
    _stub_legs(monkeypatch, session_id)
    repo_root = _group_em_with_teammates(
        tmp_path,
        monkeypatch,
        [
            {"agentType": "coordinator:group-em-assistant", "name": "gem-assistant"},
            {"agentType": "general-purpose", "name": "fleet-watch"},
        ],
        session_id,
    )

    result = gee._group_em_enter({"repo_root": repo_root})

    assert result["teammates"]["dispatch_required"] is False
    assert result["watch_liveness"]["verdict"] == "absent"
    assert "group-em-watch" in result["watch_liveness"]["remedy"]
    assert "watch_liveness_error" not in result


def test_watch_liveness_reports_armed_on_a_fresh_stamp(tmp_path, monkeypatch):
    session_id = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
    _stub_legs(monkeypatch, session_id)
    repo_root = _group_em_with_teammates(tmp_path, monkeypatch, [], session_id)
    gee.group_em_watch_heartbeat.stamp(
        repo_root, holder_session_id=session_id, declinations=[], interval_seconds=1380.0
    )

    result = gee._group_em_enter({"repo_root": repo_root})

    assert result["watch_liveness"]["verdict"] == "armed"
    assert result["watch_liveness"]["holder_session_id"] == session_id
    assert "remedy" not in result["watch_liveness"]


def test_watch_liveness_reports_stale_past_the_deadline_the_tick_set_itself(tmp_path, monkeypatch):
    """Not an mtime read: the deadline is one the previous tick wrote for
    itself off its own cadence. Missing a deadline you set is evidence."""
    session_id = "aaaaaaaa-bbbb-cccc-dddd-333333333333"
    _stub_legs(monkeypatch, session_id)
    repo_root = _group_em_with_teammates(tmp_path, monkeypatch, [], session_id)
    gee.group_em_watch_heartbeat.stamp(
        repo_root,
        holder_session_id=session_id,
        declinations=[],
        interval_seconds=5.0,
        now_epoch=time.time() - 3600,
    )

    result = gee._group_em_enter({"repo_root": repo_root})

    assert result["watch_liveness"]["verdict"] == "stale"
    assert result["watch_liveness"]["seconds_overdue"] > 0


def test_roster_considered_separates_looked_from_found(tmp_path, monkeypatch):
    """An EMPTY roster over a POPULATED enumeration is not a quiet fleet.

    The defect this closes (measured 2026-09-01, 11 peers enumerated / 2 kept):
    `roster` is candidates UNION unclassifiable, so a consumer reading its
    length as the peer population reports a busy repo as empty. The count of
    what was classified has to be in the payload, or the two states are
    indistinguishable from it.
    """
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-rc")
    monkeypatch.setattr(gee.group_em_read_pass, "fetch_live_agents", lambda *a, **k: [
        {"sessionId": "peer-a", "cwd": str(tmp_path), "status": "busy"},
        {"sessionId": "peer-b", "cwd": str(tmp_path), "status": "busy"},
        {"sessionId": "peer-c", "cwd": str(tmp_path), "status": "idle"},
    ])
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-rc", "superseded_incumbent": None},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["roster"] == []
    assert result["roster_considered"] == 3


def test_roster_considered_survives_a_raising_roster_leg(tmp_path, monkeypatch):
    """The count is the answer to "did anything look", so it must outlive the
    leg whose failure raises that question. A roster leg that raised leaves
    `roster` None with an error sibling; `roster_considered` still reports."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-rc2")
    monkeypatch.setattr(gee.group_em_read_pass, "fetch_live_agents", lambda *a, **k: [
        {"sessionId": "peer-a", "cwd": str(tmp_path), "status": "busy"},
    ])

    def _boom(*a, **k):
        raise RuntimeError("roster leg blew up")

    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", _boom)
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-rc2", "superseded_incumbent": None},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["roster"] is None
    assert "roster_error" in result
    assert result["roster_considered"] == 1


def test_roster_considered_is_absent_on_a_refused_group_em(tmp_path, monkeypatch):
    """Same rule as `roster`: a leg that never ran is ABSENT, not zero. A
    `roster_considered` of 0 under a refused Group-EM would assert an empty fleet
    this op had no standing to enumerate."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-rc3")
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": False,
            "superseded_incumbent": {"session_id": "other", "live_reason": "live"},
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert "roster_considered" not in result
    assert "roster" not in result
