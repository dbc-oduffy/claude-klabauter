"""peer_hunk_gate holds a path an unresolved critical finding or a ledger conflict names, while a foreign hunk is on it."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import hunk_provenance, peer_hunk_gate, run_token
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import Finding
from coordinator_core.ops.dispatch_emit.tests.test_peer_hunk_gate import (  # noqa: F401
    OWN, PATH, SELF, _declare, _halt, _record, _stamped, repo,
)

FOREIGN = "committed lines 3 were not produced by run ask-own; probable owner run-z"
CONFLICT = "memo says execute_review.py stays untouched; the ask edits it"


def _critical(repo, severity="critical", status=None, stem="rev-1", file=PATH) -> None:
    _record(repo, OWN, stem)
    row = {"id": "f1", "file": file, "summary": "peer hunks ride along", "severity": severity}
    if status:
        row["status"] = status
    (repo / ".coordinator-local" / "subagent-share" / SELF / f"{stem}.md").write_text(
        f"---\nagent_type: reviewer\n---\n\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n", encoding="utf-8"
    )


def _stub_unproduced(monkeypatch, details: dict) -> None:
    monkeypatch.setattr(
        hunk_provenance, "unproduced_hunks",
        lambda root, run_id, paths, **k: [Finding(p, "foreign-hunk-unproduced", details[p]) for p in paths if p in details],
    )


def _landing(repo, conflicts=()) -> dict:
    return peer_hunk_gate.landing_holds(repo, SELF, OWN, None, [PATH], [], [PATH], [PATH], None, ledger_conflicts=conflicts)


@pytest.mark.parametrize("severity", ["critical", "P0"])
def test_a_critical_row_naming_the_path_is_an_out_of_ask_hold(repo, severity):
    _critical(repo, severity)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == [(PATH, "rev-1", "peer hunks ride along")]


@pytest.mark.parametrize("severity", ["major", "P1", "minor", "nit", "bogus"])
def test_a_non_critical_row_holds_nothing(repo, severity):
    _critical(repo, severity)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


@pytest.mark.parametrize("status", ["applied", "em-rejected"])
def test_a_resolved_critical_row_holds_nothing(repo, status):
    _critical(repo, status=status)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_a_critical_row_on_a_path_with_a_foreign_hunk_is_held_under_its_owner(repo, monkeypatch):
    _critical(repo)
    _stub_unproduced(monkeypatch, {PATH: FOREIGN})
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda *a, **k: set())
    assert _landing(repo) == {PATH: "run-z"}


def test_a_critical_row_on_a_foreign_hunk_path_carries_the_own_hunk_split(repo, monkeypatch):
    _critical(repo)
    _stub_unproduced(monkeypatch, {PATH: FOREIGN})
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda root, run_id, plan, held, split_only=frozenset(): set(held))
    assert _landing(repo) == {}


def test_a_critical_row_on_an_own_only_path_is_not_held(repo, monkeypatch):
    _critical(repo)
    _stub_unproduced(monkeypatch, {})
    assert _landing(repo) == {}


def test_a_critical_row_on_a_path_a_live_peer_co_declares_is_held_and_released_only_by_the_split(repo, monkeypatch):
    _critical(repo)
    _declare(repo, "run-b", PATH)
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    _stub_unproduced(monkeypatch, {})
    seen: list = []

    def landable(root, run_id, plan, held, split_only=frozenset()):
        seen.append((dict(held), split_only))
        return set()

    monkeypatch.setattr(hunk_provenance, "landable_under_hold", landable)
    assert _landing(repo) == {PATH: "run-b"}
    assert seen == [({PATH: "run-b"}, frozenset({PATH}))]


def test_an_out_of_ask_row_still_holds_a_path_with_no_foreign_hunk(repo, monkeypatch):
    _critical(repo, severity="major")
    path = repo / ".coordinator-local" / "subagent-share" / SELF / "rev-1.md"
    path.write_text(path.read_text(encoding="utf-8").replace('"severity"', '"out_of_ask": true, "severity"'), encoding="utf-8")
    _stub_unproduced(monkeypatch, {})
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda *a, **k: set())
    assert _landing(repo) == {PATH: "rev-1"}


def test_a_ledger_conflict_naming_a_path_with_a_foreign_hunk_is_held(repo, monkeypatch):
    _stub_unproduced(monkeypatch, {PATH: FOREIGN})
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda *a, **k: set())
    assert _landing(repo, [CONFLICT]) == {PATH: "run-z"}


def test_a_ledger_conflict_carries_the_own_hunk_split_when_one_settles(repo, monkeypatch):
    _stub_unproduced(monkeypatch, {PATH: FOREIGN})
    seen: list = []

    def landable(root, run_id, plan, held, split_only=frozenset()):
        seen.append(split_only)
        return set(held)

    monkeypatch.setattr(hunk_provenance, "landable_under_hold", landable)
    assert _landing(repo, [CONFLICT]) == {}
    assert seen == [frozenset({PATH})]


def test_a_ledger_conflict_naming_an_own_only_path_or_another_file_holds_nothing(repo, monkeypatch):
    _stub_unproduced(monkeypatch, {})
    assert _landing(repo, [CONFLICT]) == {}
    assert peer_hunk_gate._conflict_paths(["memo about pkg/other.py only"], [PATH]) == []
    assert peer_hunk_gate._conflict_paths([CONFLICT], [PATH, "x/execute_review.py"]) == []
    assert peer_hunk_gate._conflict_paths([f"see {PATH}"], [PATH, "x/execute_review.py"]) == [PATH]


def test_a_halted_owner_with_a_live_junior_refire_declaring_the_path_is_held_under_the_refire(repo, monkeypatch):
    _declare(repo, "run-b", PATH)
    _halt(repo / "state" / "warp" / "run-b")
    refire = _stamped("5b4c54")
    _declare(repo, refire, PATH)
    run_token.mint(repo, refire, "c.mjs", session_id=SELF)
    _stub_unproduced(monkeypatch, {PATH: "committed lines 3 were not produced by run ask-own; probable owner run-b"})
    junior = ("0000", OWN)  # every sibling minted later than the landing run is junior and claims nothing
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], minted_before=junior) != [(PATH, refire)]
    assert peer_hunk_gate._live_sibling_holds(repo, OWN, None, [PATH], [PATH], junior) == {PATH: refire}


def test_a_halted_owner_with_no_live_refire_holds_nothing_live(repo, monkeypatch):
    _declare(repo, "run-b", PATH)
    _halt(repo / "state" / "warp" / "run-b")
    _stub_unproduced(monkeypatch, {PATH: "committed lines 3 were not produced by run ask-own; probable owner run-b"})
    assert peer_hunk_gate._live_sibling_holds(repo, OWN, None, [PATH], [PATH], None) == {}
