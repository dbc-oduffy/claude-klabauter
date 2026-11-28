"""A {sizing} fire naming no stop_after parks only on a halted run it truly resumes; a parked run's refire (also
the rebind onto its own id) stages; the parked halt's next_action names that refire. Drives `dispatch.emit` and
`dispatch.ask_stage` at default arguments over a lineage built under tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage, halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PARKED,
    OP_ASK_STAGE,
    PARKED_REPLY_KEY,
    RESUME_PARK_CUT_FILE,
    STOP_AFTER_FILE,
    STOP_AFTER_KEY,
    STOP_AFTER_PLAN,
    parked_next_action,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN as STAGEABLE_PLAN, PLAN_REL as STAGEABLE_REL
from coordinator_core.ops.dispatch_emit.tests.test_stop_after_survives_chained_runs_e2e import (
    _BLITZ,
    _SIZING_DOC,
    REL,
)

_BASE = "a" * 40
_FUTURE_STAMP = "20991231T235959-abcdef"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_SIZING_DOC, sort_keys=False), encoding="utf-8", newline="\n")
    (tmp_path / STAGEABLE_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / STAGEABLE_REL).write_text(STAGEABLE_PLAN, encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)
    return tmp_path


def _script(reply: dict) -> str:
    return Path(reply["path"]).read_text(encoding="utf-8")


def _fire(repo: Path, **extra) -> dict:
    reply = _dispatch_emit({"ask": True, "sizing_path": REL, "target_root": str(repo), **extra})
    assert "error" not in reply, reply
    return reply


def _ancestor_ask_with_live_marker(repo: Path) -> str:
    reply = _dispatch_emit({"ask": "add a thing", STOP_AFTER_KEY: STOP_AFTER_PLAN, "target_root": str(repo)})
    assert "error" not in reply, reply
    ask_id = reply["run_id"]
    assert (record_dir(repo, ask_id) / STOP_AFTER_FILE).is_file()
    return ask_id


def _halt(repo: Path, run_id: str, kind: str, base: str | None = _BASE) -> None:
    """`base=None` records no run base: a refire stage then baselines the live tree, not a commit the tmp repo lacks."""
    halted_run.write_halt_marker(
        record_dir(repo, run_id), halted=kind, sizing_rel=REL, run_base_sha=base, plan_rel=STAGEABLE_REL
    )


def _supersede_by_newer_warp_dir(repo: Path) -> None:
    """The fresh fire's own premint dir: a newer `warp-<stem>-` dir makes `halted_ask_run_for` see the ask superseded."""
    stem = Path(REL).stem
    d = record_dir(repo, f"warp-{stem}-{_FUTURE_STAMP}")
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": REL}), encoding="utf-8")


def _stage(repo: Path, run_id: str) -> dict:
    return ask_stage._handler({"run_id": run_id, "plan_path": STAGEABLE_REL, "writes": []}, repo_root=repo)


def test_a_fresh_fire_over_a_superseded_ancestor_marker_neither_parks_nor_stage_parks(repo) -> None:
    ancestor = _ancestor_ask_with_live_marker(repo)
    _halt(repo, ancestor, "criterion")
    _supersede_by_newer_warp_dir(repo)

    fresh = _fire(repo)

    assert OP_ASK_STAGE in _script(fresh)
    fresh_dir = record_dir(repo, fresh["run_id"])
    assert not (fresh_dir / STOP_AFTER_FILE).exists()
    assert json.loads((fresh_dir / "run.json").read_text(encoding="utf-8"))["resumed_from"] == ancestor
    assert (fresh_dir / RESUME_PARK_CUT_FILE).is_file(), "the fresh fire cuts the inherited marker at emit"
    assert PARKED_REPLY_KEY not in _stage(repo, fresh["run_id"])


def test_a_fresh_fire_over_an_unhalted_ancestor_marker_does_not_park(repo) -> None:
    ancestor = _ancestor_ask_with_live_marker(repo)
    assert not (record_dir(repo, ancestor) / "halted.json").exists()

    fresh = _fire(repo, chain_parent=None)

    assert OP_ASK_STAGE in _script(fresh)
    assert PARKED_REPLY_KEY not in _stage(repo, fresh["run_id"])


def test_a_refire_of_a_halted_stop_after_run_it_resumes_still_parks(repo) -> None:
    ancestor = _ancestor_ask_with_live_marker(repo)
    _halt(repo, ancestor, "criterion")

    refire = _fire(repo)

    assert json.loads((record_dir(repo, refire["run_id"]) / "run.json").read_text(encoding="utf-8"))["resumed_from"] == ancestor
    assert OP_ASK_STAGE not in _script(refire)
    assert json.loads((record_dir(repo, refire["run_id"]) / STOP_AFTER_FILE).read_text(encoding="utf-8"))[STOP_AFTER_KEY] == STOP_AFTER_PLAN


def test_an_explicit_stop_after_still_parks_over_a_superseded_ancestor(repo) -> None:
    ancestor = _ancestor_ask_with_live_marker(repo)
    _halt(repo, ancestor, "criterion")
    _supersede_by_newer_warp_dir(repo)

    explicit = _fire(repo, **{STOP_AFTER_KEY: STOP_AFTER_PLAN})

    assert OP_ASK_STAGE not in _script(explicit)


def test_lineage_replay_ancestor_marker_then_fresh_fire_then_refire_after_certification_stages(repo) -> None:
    ancestor = _ancestor_ask_with_live_marker(repo)
    _halt(repo, ancestor, "criterion")
    _supersede_by_newer_warp_dir(repo)

    fresh = _fire(repo)
    assert OP_ASK_STAGE in _script(fresh)
    fresh_id = fresh["run_id"]

    _halt(repo, fresh_id, HALT_PARKED)
    refire = _fire(repo)

    assert OP_ASK_STAGE in _script(refire), "a parked run's refire must stage, not park again"
    assert PARKED_REPLY_KEY not in _stage(repo, refire["run_id"])


def test_a_parked_run_whose_own_marker_is_live_stages_on_the_rebind_onto_its_own_id(repo) -> None:
    first = _dispatch_emit({"ask": True, "sizing_path": REL, STOP_AFTER_KEY: STOP_AFTER_PLAN, "target_root": str(repo)})
    assert "error" not in first, first
    parked_id = first["run_id"]
    assert (record_dir(repo, parked_id) / STOP_AFTER_FILE).is_file()
    _halt(repo, parked_id, HALT_PARKED)

    refire = _fire(repo)

    assert refire["run_id"] == parked_id, "the refire rebinds onto the parked run"
    assert OP_ASK_STAGE in _script(refire)
    assert PARKED_REPLY_KEY not in _stage(repo, parked_id)


def test_a_staged_executed_run_halted_review_blocked_refires_without_an_inherited_ancestor_marker(repo) -> None:
    ancestor = _ancestor_ask_with_live_marker(repo)
    _halt(repo, ancestor, "criterion", base=None)
    _supersede_by_newer_warp_dir(repo)
    fresh = _fire(repo)
    run_id = fresh["run_id"]
    staged = _stage(repo, run_id)
    assert PARKED_REPLY_KEY not in staged and "error" not in staged, staged
    assert (record_dir(repo, run_id) / "manifest.json").is_file()
    assert (record_dir(repo, ancestor) / STOP_AFTER_FILE).is_file(), "the ancestor's marker stays live"
    _halt(repo, run_id, "review_blocked", base=None)

    refire = _dispatch_emit({"ask": True, "sizing_path": REL, "target_root": str(repo)})

    assert "error" not in refire, refire
    assert "already staged" not in json.dumps(refire)
    assert OP_ASK_STAGE in _script(refire)


def test_the_parked_next_action_names_the_sizing_refire_not_the_baton_intake(repo) -> None:
    text = parked_next_action(STAGEABLE_REL)
    assert STAGEABLE_REL in text and "re-fire the sizing" in text
    assert "mise-prep" not in text and "mise-en-place" not in text
