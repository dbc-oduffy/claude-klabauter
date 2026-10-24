"""A revise withhold_paths entry outside a staged run's frozen footprint is refused with the follow-up-ask
route; note prose never counts toward the footprint, and a refire is not derived from stored notes."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-10-24-x-7c8544-20261024T063410-182ece"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"
UNDER = "pkg/sub/under.py"


@pytest.fixture
def repo(tmp_path):
    for rel in (INSIDE, OUTSIDE, UNDER):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    return tmp_path


def _stage(repo, writes=(INSIDE,), under=()):
    manifest = {"doc_writes": [], "rows": [{"id": "X1", "writes": list(writes), "writes_under": list(under)}]}
    (rl.run_dir(repo, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_declared_withhold_outside_is_refused_with_the_follow_up_ask_route(repo):
    _stage(repo)
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(repo, RUN, withhold_paths=[{"path": OUTSIDE, "reason": "peer hunks"}])
    assert OUTSIDE in str(exc.value) and "follow-up ask" in str(exc.value)
    assert not (rl.run_dir(repo, RUN) / "withhold.jsonl").exists()


def test_declared_withhold_inside_is_accepted(repo):
    _stage(repo)
    rl.revise(repo, RUN, withhold_paths=[{"path": INSIDE, "reason": "peer hunks"}])
    assert (rl.run_dir(repo, RUN) / "withhold.jsonl").is_file()


def test_note_citing_an_outside_file_as_provenance_is_accepted(repo):
    _stage(repo)
    rl.revise(repo, RUN, note=f"source text came from `{OUTSIDE}` (provenance only)")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_covered_path_is_accepted(repo):
    _stage(repo)
    rl.revise(repo, RUN, note=f"also fix `{INSIDE}`", withhold_paths=[{"path": INSIDE, "reason": "r"}])
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_writes_under_covers_a_nested_file(repo):
    _stage(repo, under=("pkg/sub/",))
    rl.revise(repo, RUN, note=f"also fix {UNDER}")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_unstaged_run_has_no_frozen_footprint(repo):
    rl.revise(repo, RUN, note=f"also fix {OUTSIDE}")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_refire_is_offered_despite_a_stored_note_naming_an_outside_path(repo):
    rl.revise(repo, RUN, note=f"also fix {OUTSIDE}")
    _stage(repo)
    reply = rl._refire(None, repo, (None, []), halted=True)
    assert reply["next"]["kind"] == "refire"


def test_refire_is_still_offered_when_every_noted_path_is_covered(repo):
    rl.revise(repo, RUN, note=f"also fix {INSIDE}")
    _stage(repo)
    reply = rl._refire(None, repo, (None, []), halted=True)
    assert reply["next"]["kind"] == "refire"
