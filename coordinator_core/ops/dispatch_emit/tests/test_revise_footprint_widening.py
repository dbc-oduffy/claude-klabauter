"""A revise note naming a file outside a staged run's frozen footprint is refused at submit with the
follow-up-ask route; a covered or manifest-less run takes it; a refire is never offered for it."""

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


def test_outside_path_is_refused_with_the_follow_up_ask_route(repo):
    _stage(repo)
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(repo, RUN, note=f"also fix `{OUTSIDE}`")
    assert OUTSIDE in str(exc.value) and "follow-up ask" in str(exc.value)
    assert rl.read_notes(rl.run_dir(repo, RUN)) == []


def test_covered_path_is_accepted(repo):
    _stage(repo)
    rl.revise(repo, RUN, note=f"also fix `{INSIDE}`")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_writes_under_covers_a_nested_file(repo):
    _stage(repo, under=("pkg/sub/",))
    rl.revise(repo, RUN, note=f"also fix {UNDER}")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_unstaged_run_has_no_frozen_footprint(repo):
    rl.revise(repo, RUN, note=f"also fix {OUTSIDE}")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_refire_is_not_offered_for_a_stored_outside_note(repo):
    d = rl.run_dir(repo, RUN)
    rl.revise(repo, RUN, note=f"also fix {OUTSIDE}")
    _stage(repo)
    reply = rl._refire(None, repo, (None, []), halted=True, dir_=d, run_id=RUN)
    assert reply["next"]["kind"] == "follow-up-ask"
    assert reply["next"]["paths"] == [OUTSIDE]
    assert "follow-up ask" in reply["note"]


def test_refire_is_still_offered_when_every_noted_path_is_covered(repo):
    d = rl.run_dir(repo, RUN)
    rl.revise(repo, RUN, note=f"also fix {INSIDE}")
    _stage(repo)
    reply = rl._refire(None, repo, (None, []), halted=True, dir_=d, run_id=RUN)
    assert reply["next"]["kind"] == "refire"
