"""workflow.revise refuses, at revise time, an add_paths entry or a note citing a repo file outside the
run's write set, naming both routes; in-scope notes, withhold_paths and credit_chunks still pass."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-10-26-x-c826e4-20261026T030349-c826e4"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"


@pytest.fixture
def repo(tmp_path):
    for rel in (INSIDE, OUTSIDE):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    return tmp_path


def _stage(repo):
    manifest = {"doc_writes": [], "rows": [{"id": "X1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(repo, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _assert_both_routes(msg: str, path: str) -> None:
    assert path in msg and "new ask" in msg and "resize the sizing" in msg


def test_add_paths_outside_is_refused_naming_both_routes(repo):
    _stage(repo)
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(repo, RUN, add_paths=[OUTSIDE], note="also edit it")
    _assert_both_routes(str(exc.value), OUTSIDE)
    assert rl.read_notes(rl.run_dir(repo, RUN)) == []


def test_add_paths_outside_is_refused_on_an_unstaged_run(repo):
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(repo, RUN, add_paths=[{"path": OUTSIDE}], note="n")
    _assert_both_routes(str(exc.value), OUTSIDE)


def test_add_paths_outside_through_the_op_replies_an_error(repo):
    _stage(repo)
    (repo / ".git").mkdir()
    reply = rl._handler({"run_id": RUN, "add_paths": [OUTSIDE]}, repo)
    assert OUTSIDE in reply["error"] and "new ask" in reply["error"]


def test_note_citing_an_outside_file_is_refused_on_a_staged_run(repo):
    _stage(repo)
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(repo, RUN, note=f"please also change `{OUTSIDE}`")
    _assert_both_routes(str(exc.value), OUTSIDE)
    assert rl.read_notes(rl.run_dir(repo, RUN)) == []


def test_note_citing_an_inside_file_is_accepted(repo):
    _stage(repo)
    rl.revise(repo, RUN, note=f"also fix `{INSIDE}`")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_note_without_a_path_is_accepted(repo):
    _stage(repo)
    rl.revise(repo, RUN, note="tighten the wording")
    assert len(rl.read_notes(rl.run_dir(repo, RUN))) == 1


def test_withhold_inside_is_unaffected(repo):
    _stage(repo)
    rl.revise(repo, RUN, withhold_paths=[{"path": INSIDE, "reason": "peer hunks"}])
    assert (rl.run_dir(repo, RUN) / "withhold.jsonl").is_file()


def test_credit_chunks_is_unaffected(repo):
    _stage(repo)
    rl.revise(repo, RUN, credit_chunks=[{"chunk": "X1", "evidence": "landed"}])
    assert (rl.run_dir(repo, RUN) / "credit.jsonl").is_file()
