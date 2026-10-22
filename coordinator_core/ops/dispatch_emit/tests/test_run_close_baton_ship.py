"""Tests for the baton-ship close leg: ran, not-applicable, and a no-spawn discharged rerun."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg
from coordinator_core.ops.tests.test_handoff_correct_body import (
    _AUTHOR_SESSION,
    _make_git_repo,
    _seed_claimed_handoff,
)
from coordinator_core.session_ledger.aggregate_chain_loe import unparseable_ledger_rows

SHA = "0123456789abcdef0123456789abcdef01234567"
MARKER = "run run-1 shipped in " + SHA[:8]
BODY = (
    "\n## What this covers\n\n"
    "A realistically sized section standing in for the prose ahead of the ledger block, so the "
    "ledger append's disambiguation window stays far under the body-size ratio bound.\n\n"
    "## Session Ledger\n\n"
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
)


@pytest.fixture
def session_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _AUTHOR_SESSION)


def _baton(repo, name):
    extra = (
        "deployment_state: in_flight\n"
        f"claimed_by: {_AUTHOR_SESSION}\n"
        "created: 2026-07-05\n"
        "branch: main\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: fixture baton"
    )
    _seed_claimed_handoff(repo, name, body=BODY, extra_fm=extra)
    return f"state/handoffs/{name}"


def _forbid_spawn(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a process was spawned")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_no_claimed_baton_is_not_applicable(tmp_path):
    assert baton_ship_leg(tmp_path, "run-1", [], SHA).status == "not-applicable"


def test_ran_stamps_and_appends_one_parseable_row_per_baton(tmp_path, session_env, monkeypatch):
    repo = _make_git_repo(tmp_path)
    batons = [_baton(repo, "a.md"), _baton(repo, "b.md")]
    _forbid_spawn(monkeypatch)

    result = baton_ship_leg(repo, "run-1", batons, SHA)

    assert result.status == "ran", result.detail
    for rel in batons:
        text = (repo / rel).read_text(encoding="utf-8")
        assert "deployment_state: shipped" in text
        assert f"shipped_in: '{SHA[:8]}'" in text
        assert text.count(MARKER) == 1
        row = next(line for line in text.splitlines() if MARKER in line)
        assert unparseable_ledger_rows("## Session Ledger\n\n" + row + "\n") == []


def test_open_baton_is_shipped_with_its_ledger_row_without_correct_body(tmp_path, session_env):
    repo = _make_git_repo(tmp_path)
    rel = _baton(repo, "a.md")
    path = repo / rel
    text = path.read_text(encoding="utf-8")
    assert "status: claimed" in text
    path.write_text(text.replace("status: claimed", "status: open").replace(f"claimed_by: {_AUTHOR_SESSION}\n", ""),
                    encoding="utf-8")

    result = baton_ship_leg(repo, "run-1", [rel], SHA)

    assert result.status == "ran", result.detail
    shipped = path.read_text(encoding="utf-8")
    assert "status: open" in shipped and "deployment_state: shipped" in shipped
    assert shipped.count(MARKER) == 1 and "Correction Log" not in shipped
    assert baton_ship_leg(repo, "run-1", [rel], SHA).status == "discharged"


def test_rerun_is_discharged_and_adds_no_second_row(tmp_path, session_env, monkeypatch):
    repo = _make_git_repo(tmp_path)
    batons = [_baton(repo, "a.md")]
    assert baton_ship_leg(repo, "run-1", batons, SHA).status == "ran"
    before = (repo / batons[0]).read_text(encoding="utf-8")
    _forbid_spawn(monkeypatch)

    again = baton_ship_leg(repo, "run-1", batons, SHA)

    assert again.status == "discharged", again.detail
    assert (repo / batons[0]).read_text(encoding="utf-8") == before


def test_unreadable_baton_is_failed(tmp_path):
    result = baton_ship_leg(tmp_path, "run-1", ["state/handoffs/missing.md"], SHA)
    assert result.status == "failed"
    assert "missing.md" in result.detail
