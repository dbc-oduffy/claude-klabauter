"""An ask run left at stage `minted` with quiet own dirs is not reaped while a descendant warp run
is active within `MINTED_STALE_SECONDS`; with no active descendant it is still halted
`abandoned_at_minted`."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
ASK = "ask-20261102T000000-aaaaaa"
SIZING = "state/sizings/2026-11-02-x.yaml"
STALE = wrd.MINTED_STALE_SECONDS + 600


def _stamp(age: float) -> str:
    return datetime.fromtimestamp(time.time() - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_dir(directory: Path, age: float) -> None:
    when = time.time() - age
    for entry in directory.iterdir():
        os.utime(entry, (when, when))
    os.utime(directory, (when, when))


def _make_run(repo: Path, run_id: str, *, stage: str, age: float, run_json: dict, held: bool = False) -> Path:
    d = record_dir(repo, run_id)
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    if held:
        (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": ["src/held.py"]}]}), encoding="utf-8")
        run_token.mint(repo, run_id, "ask.mjs", session_id=PEER)
    write_run_stage_at(d, stage, now=_stamp(age))
    _age_dir(d, age)
    return d


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    session_core.init(PEER, cwd=str(root))
    return root


def _ask(repo: Path) -> Path:
    return _make_run(repo, ASK, stage="minted", age=STALE, run_json={"sizing_rel": SIZING}, held=True)


def _live(repo: Path) -> bool:
    record = next(r for r in run_token.held_records(repo) if r["run_id"] == ASK)
    return wrd._live_peer_run(repo, record, SELF, str(repo))  # cwd scopes session_live


def _no_markers(ask: Path) -> bool:
    return not (ask / "halted.json").exists() and not (ask / "resume.json").exists()


def test_ask_with_active_resumed_child_is_live_and_unmarked(repo):
    ask = _ask(repo)
    _make_run(repo, "warp-child-1", stage="staged", age=60, run_json={"resumed_from": ASK, "sizing_rel": "other.yaml"})
    assert _live(repo)
    assert _no_markers(ask)
    assert not wrd._halted(repo, ASK)


def test_ask_with_active_child_bound_to_its_sizing_is_live(repo):
    ask = _ask(repo)
    _make_run(repo, "warp-child-2", stage="staged", age=60, run_json={"sizing_rel": SIZING})
    assert _live(repo)
    assert _no_markers(ask)


def test_active_grandchild_through_a_stale_child_counts(repo):
    ask = _ask(repo)
    _make_run(repo, "warp-child-3", stage="staged", age=STALE, run_json={"resumed_from": ASK})
    _make_run(repo, "warp-grandchild", stage="staged", age=60, run_json={"resumed_from": "warp-child-3"})
    assert _live(repo)
    assert _no_markers(ask)


def test_claim_readers_keep_the_ask_runs_claims_while_child_is_active(repo):
    ask = _ask(repo)
    _make_run(repo, "warp-child-4", stage="staged", age=60, run_json={"resumed_from": ASK})
    path = f"{wrd.RECORD_DIR_ROOT}/{ASK}/manifest.json"
    assert wrd.live_record_dir_owners(repo, [path], exclude_session=SELF, cwd=str(repo)) == {path: [PEER]}
    claims = wrd._live_claim_scan(repo, ["src/held.py"], SELF, str(repo), None, None)
    assert [c[1] for c in claims["src/held.py"]] == [ASK]
    assert _no_markers(ask)


def test_ask_without_a_child_is_still_halted_abandoned(repo):
    ask = _ask(repo)
    assert not _live(repo)
    marker = json.loads((ask / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == wrd.ABANDONED_AT_MINTED


def test_ask_with_only_a_stale_child_is_still_halted(repo):
    ask = _ask(repo)
    _make_run(repo, "warp-child-5", stage="staged", age=STALE, run_json={"resumed_from": ASK})
    assert not _live(repo)
    assert (ask / "halted.json").is_file()


def test_unrelated_active_run_does_not_spare_the_ask(repo):
    ask = _ask(repo)
    _make_run(repo, "warp-unrelated", stage="staged", age=60, run_json={"resumed_from": "ask-other", "sizing_rel": "z.yaml"})
    assert not _live(repo)
    assert (ask / "halted.json").is_file()


def test_halted_child_does_not_spare_the_ask(repo):
    ask = _ask(repo)
    child = _make_run(repo, "warp-child-6", stage="staged", age=60, run_json={"resumed_from": ASK})
    (child / "halted.json").write_text(json.dumps({"status": "halted", "halted": "x"}), encoding="utf-8")
    assert not _live(repo)
    assert (ask / "halted.json").is_file()
