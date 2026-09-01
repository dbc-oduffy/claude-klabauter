"""Tests for `group_em.teammates` -- the crown's standing-teammate assertion.

Both teammates get a present arm and an absent arm, and the two are asserted
SEPARATELY: a crown missing the fleet watcher is the worse of the two
failures (it makes a stopped fleet look healthy), so a test that only checked
"some teammate is there" would pass on exactly the case that matters most.

The sidecar fixtures below are copied from real `.meta.json` files in this
machine's own projects tree, including the asymmetry that motivates the
two-namespace matcher: the assistant is dispatched by agent TYPE, the fleet
watcher as a NAMED `general-purpose` agent.

Spec backlink: state/sizings/2026-08-31-a-crowned-group-em-always-has-a-warm-assistant.yaml
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.group_em import teammates


ASSISTANT_META = {
    "agentType": "coordinator:group-em-assistant",
    "description": "Standing GEM assistant",
    "name": "gem-assistant",
    "toolUseId": "toolu_stub_assistant",
    "spawnDepth": 1,
}

WATCH_META = {
    "agentType": "general-purpose",
    "description": "Standing fleet watcher",
    "name": "fleet-watch",
    "toolUseId": "toolu_stub_watch",
    "spawnDepth": 1,
    "model": "haiku",
}

UNRELATED_META = {
    "agentType": "coordinator:staff-eng",
    "description": "the Staff Engineer plan review",
    "toolUseId": "toolu_stub_patrik",
    "spawnDepth": 1,
}

SESSION_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def crown(tmp_path, monkeypatch):
    """A fake home whose projects tree holds one crowned session's subagents dir.

    Returns `(repo_root, write_sidecar)` -- call `write_sidecar(stem, meta)` to
    plant one `.meta.json`.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    repo_root = str(tmp_path / "repo")

    def write_sidecar(stem: str, meta) -> None:
        directory = Path(teammates.subagents_dir(repo_root, SESSION_ID))
        directory.mkdir(parents=True, exist_ok=True)
        if meta is None:
            (directory / f"{stem}.meta.json").write_text("{not json", encoding="utf-8")
        else:
            (directory / f"{stem}.meta.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
        (directory / f"{stem}.jsonl").write_text("", encoding="utf-8")

    return repo_root, write_sidecar


def test_both_teammates_present_discharges_the_obligation(crown):
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa1", ASSISTANT_META)
    write_sidecar("agent-aaaa2", WATCH_META)
    write_sidecar("agent-aaaa3", UNRELATED_META)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["agents"]["group_em_assistant"]["present"] is True
    assert result["agents"]["fleet_watch"]["present"] is True
    assert result["missing"] == []
    assert result["dispatch_required"] is False
    assert result["unreadable"] is False
    assert result["probe"] == "subagent-dispatch-record"


def test_absent_assistant_is_reported_alone(crown):
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa2", WATCH_META)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["agents"]["fleet_watch"]["present"] is True
    assert result["agents"]["group_em_assistant"]["present"] is False
    assert result["missing"] == ["group_em_assistant"]
    assert result["dispatch_required"] is True


def test_absent_fleet_watch_is_reported_alone(crown):
    """The worse of the two failures, and the one a single boolean would hide."""
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa1", ASSISTANT_META)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["agents"]["group_em_assistant"]["present"] is True
    assert result["agents"]["fleet_watch"]["present"] is False
    assert result["missing"] == ["fleet_watch"]
    assert result["dispatch_required"] is True


def test_neither_teammate_reports_the_watcher_first(crown):
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa3", UNRELATED_META)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["missing"] == ["fleet_watch", "group_em_assistant"]
    assert result["dispatch_required"] is True
    assert result["unreadable"] is False


def test_unnamed_assistant_matches_on_agent_type(crown):
    """A dispatch with no `name` still satisfies the obligation via `agentType`."""
    repo_root, write_sidecar = crown
    meta = dict(ASSISTANT_META)
    del meta["name"]
    write_sidecar("agent-aaaa1", meta)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["agents"]["group_em_assistant"]["present"] is True


def test_fleet_watch_matches_on_name_despite_generic_agent_type(crown):
    """`coordinator:fleet-watch` is not a registered agent type on this machine;
    the watcher is dispatched as a NAMED general-purpose agent, and a matcher
    keyed on `agentType` alone would report it permanently absent."""
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa2", WATCH_META)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["agents"]["fleet_watch"]["present"] is True
    assert result["agents"]["fleet_watch"]["dispatch_records"] == ["agent-aaaa2"]


def test_missing_subagents_dir_is_unreadable_not_a_verified_absence(crown):
    repo_root, _write_sidecar = crown

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["unreadable"] is True
    assert result["dispatch_required"] is True
    assert result["missing"] == ["fleet_watch", "group_em_assistant"]


def test_no_session_id_is_unreadable(crown):
    repo_root, _write_sidecar = crown

    result = teammates.presence(repo_root, None)

    assert result["unreadable"] is True
    assert result["subagents_dir"] is None
    assert result["dispatch_required"] is True


def test_malformed_sidecar_never_satisfies_the_obligation(crown):
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa1", None)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["missing"] == ["fleet_watch", "group_em_assistant"]


def test_presence_reads_no_clock(crown, monkeypatch):
    """Presence is keyed on a dispatch record, never on freshness. A sidecar
    stamped far in the past is still evidence, and no mtime/stat-time call is
    made at all -- an obligation that discharged on recency would re-derive
    the very mtime lie this probe exists to avoid."""
    repo_root, write_sidecar = crown
    write_sidecar("agent-aaaa1", ASSISTANT_META)
    write_sidecar("agent-aaaa2", WATCH_META)
    directory = Path(teammates.subagents_dir(repo_root, SESSION_ID))
    for child in directory.iterdir():
        os.utime(child, (0, 0))

    def _no_clock(*_args, **_kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("teammates.presence must not read a clock")

    monkeypatch.setattr(teammates.os.path, "getmtime", _no_clock)
    monkeypatch.setattr(teammates.os.path, "getctime", _no_clock)

    result = teammates.presence(repo_root, SESSION_ID)

    assert result["dispatch_required"] is False
