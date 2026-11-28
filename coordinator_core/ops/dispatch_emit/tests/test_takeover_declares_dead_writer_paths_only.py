"""A takeover refire's staged declaration leaves out a path a live session holds and keeps a dead-held or unheld one."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.resume_gate import TAKEOVER_LOG
from coordinator_core.session import scope

PLAN_REL = "docs/plans/2026-11-24-takeover-declares.md"
LIVE, DEAD, UNHELD = "pkg/live_held.py", "pkg/dead_held.py", "pkg/unheld.py"
RUN_ID = "r1"
SESSION = "taker"

PLAN = (
    "---\ntitle: fixture\n---\n# Fixture\n\n## Tasks\n\n"
    "```yaml plan-tasks\n- id: A1\n  title: row\n  change_kind: code-edit\n"
    f"  surface: {LIVE}\n  writes:\n    - {LIVE}\n    - {DEAD}\n    - {UNHELD}\n```\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    # Only LIVE has a live holder; the dead holder's claim is dropped by the read itself.
    monkeypatch.setattr(
        scope, "contested_by_live_peers",
        lambda paths, sid, cwd=None: {p: ["example-game-repo-a4"] for p in paths if p == LIVE},
    )
    return tmp_path


def _takeover(repo: Path) -> None:
    rd = record_dir(repo, RUN_ID)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / TAKEOVER_LOG).write_text(
        '{"from_session": "other", "reason": "peer died", "to_session": "taker"}\n', encoding="utf-8"
    )


def _declared(repo: Path) -> set[str]:
    manifest = ask_stage.stage(repo, run_id=RUN_ID, plan_rel=PLAN_REL, session_id=SESSION)
    return {w for row in manifest.rows for w in row.writes}


def test_a_takeover_refire_declares_the_dead_held_and_unheld_paths_not_the_live_held_one(repo: Path) -> None:
    _takeover(repo)
    declared = _declared(repo)
    assert LIVE not in declared
    assert {DEAD, UNHELD} <= declared


def test_a_stranded_adopt_row_with_no_takeover_drops_the_live_held_path_and_a_plain_row_keeps_it(repo: Path) -> None:
    stranded = PLAN.replace(
        "  surface:", "  authorization:\n    source: em-inferred\n    rationale: adopt the stranded files\n  surface:"
    )
    (repo / PLAN_REL).write_text(stranded, encoding="utf-8", newline="\n")
    declared = _declared(repo)
    assert LIVE not in declared
    assert {DEAD, UNHELD} <= declared


def test_a_run_that_is_not_a_takeover_still_declares_every_path(repo: Path) -> None:
    assert {LIVE, DEAD, UNHELD} <= _declared(repo)


def test_a_takeover_with_no_session_id_keeps_every_path(repo: Path) -> None:
    _takeover(repo)
    manifest = ask_stage.stage(repo, run_id=RUN_ID, plan_rel=PLAN_REL)
    assert {LIVE, DEAD, UNHELD} <= {w for row in manifest.rows for w in row.writes}


@pytest.fixture
def claimed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The paths the stage hands the live-run index to claim (one claim marker each)."""
    from coordinator_core.ops.dispatch_emit import live_run_index

    seen: list[str] = []

    def spy(root, run_id, writes, ended=None):
        seen.extend(writes)
        return {}

    monkeypatch.setattr(live_run_index, "claim", spy)
    return seen


def _withhold(repo: Path, *paths: str) -> None:
    from coordinator_core.ops.dispatch_emit import run_withhold

    run_withhold.record(record_dir(repo, RUN_ID), [{"path": p, "reason": "peer holds it"} for p in paths], "t")


def test_a_path_the_run_withheld_is_never_claimed_but_a_non_withheld_path_still_is(repo: Path, claimed: list[str]) -> None:
    _withhold(repo, DEAD)
    _declared(repo)
    assert DEAD not in claimed
    assert {LIVE, UNHELD} <= set(claimed)


def test_a_run_withholding_nothing_claims_every_declared_path(repo: Path, claimed: list[str]) -> None:
    _declared(repo)
    assert {LIVE, DEAD, UNHELD} <= set(claimed)


def test_a_takeover_asks_who_holds_the_paths_once_not_once_per_row(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def contest(paths, sid, cwd=None):
        calls.append(list(paths))
        return {}

    monkeypatch.setattr(scope, "contested_by_live_peers", contest)
    (repo / PLAN_REL).write_text(
        PLAN.rstrip("`\n") + "\n- id: A2\n  title: row2\n  change_kind: code-edit\n"
        "  surface: pkg/other.py\n  writes:\n    - pkg/other.py\n```\n",
        encoding="utf-8", newline="\n",
    )
    _takeover(repo)
    _declared(repo)
    assert len(calls) == 1


def test_a_contest_read_that_raises_keeps_every_path(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(paths, sid, cwd=None):
        raise OSError("sink unreadable")

    _takeover(repo)
    monkeypatch.setattr(scope, "contested_by_live_peers", boom)
    assert {LIVE, DEAD, UNHELD} <= _declared(repo)


def test_a_withheld_directory_excludes_the_paths_under_it_and_not_a_sibling() -> None:
    from coordinator_core.ops.dispatch_emit import live_run_writes

    kept = live_run_writes.without_withheld(["a/b/x.py", "a/c.py", "a/b"], ["a/b"])
    assert kept == ["a/c.py"]


def test_an_unestablishable_contest_keeps_every_path(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _takeover(repo)
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: None)
    assert {LIVE, DEAD, UNHELD} <= _declared(repo)
