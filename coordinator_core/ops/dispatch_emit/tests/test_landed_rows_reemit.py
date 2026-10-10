"""Re-emitting a run that ended incomplete: landed rows drop, their edges are satisfied."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import _drop_landed_rows, landed_rows_from_text
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow


def _row(id_, deps=()):
    return EmitterRow(
        id=id_, title=id_, surface="s", writes=[f"p/{id_}.py"], reads=(),
        depends_on=[{"chunk": d} for d in deps],
    )


def test_landed_ids_parsed_from_checkpoint_subjects():
    text = "abc checkpoint(wave 1): 2 rows — A, B\ndef checkpoint(wave 2): 1 rows — C\nnoise"
    assert landed_rows_from_text(text) == {"A", "B", "C"}


def test_drop_landed_strips_edges_and_keeps_rest():
    rows = [_row("A"), _row("B", ["A"]), _row("C", ["A", "B"])]
    kept = _drop_landed_rows(rows, frozenset({"A"}))
    assert [r.id for r in kept] == ["B", "C"]
    assert kept[0].depends_on == []
    assert kept[1].depends_on == [{"chunk": "B"}]


def test_unknown_landed_id_refused():
    with pytest.raises(ValueError):
        _drop_landed_rows([_row("A")], frozenset({"Z"}))


def test_landed_id_closed_in_the_spine_is_skipped_not_refused():
    kept = _drop_landed_rows([_row("B")], frozenset({"A", "B"}), frozenset({"A"}))
    assert kept == []


def _git_repo(tmp_path):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    r = tmp_path / "repo"
    r.mkdir()

    def git(*a):
        subprocess.run(["git", *a], cwd=str(r), check=True, capture_output=True, **no_console_creationflags())

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    return r, git


def _commit_with(r, git, name, subject, *trailers):
    (r / name).write_text(name, encoding="utf-8")
    git("add", "--", name)
    git("commit", "-q", "-m", subject, "-m", "\n".join(trailers))


@pytest.mark.spawns_process
def test_leading_id_commit_with_plan_trailer_counts_as_landed(tmp_path):
    from coordinator_core.ops.dispatch_emit.emit import checkpoint_landed_rows

    r, git = _git_repo(tmp_path)
    _commit_with(r, git, "a", "C1: did it", "Checkpoint-Plan: docs/plans/a.md")
    _commit_with(r, git, "b", "C2, C3: other plan", "Checkpoint-Plan: docs/plans/b.md")
    _commit_with(r, git, "c", "C4: no trailer")

    assert checkpoint_landed_rows(r, "docs/plans/a.md") == {"C1"}


@pytest.mark.spawns_process
def test_gate_owed_trailer_is_read_in_the_same_spawn(tmp_path):
    from coordinator_core.ops.dispatch_emit.emit import checkpoint_landed_state

    r, git = _git_repo(tmp_path)
    _commit_with(
        r, git, "a", "checkpoint(wave 1): 2 rows — C1, C2",
        "Checkpoint-Plan: docs/plans/a.md", "Checkpoint-Gate-Owed: C2",
    )

    assert checkpoint_landed_state(r, "docs/plans/a.md") == (frozenset({"C1", "C2"}), frozenset({"C2"}))


def test_landed_id_absent_from_rows_and_closed_set_still_refused():
    with pytest.raises(ValueError):
        _drop_landed_rows([_row("B")], frozenset({"A", "Z"}), frozenset({"A"}))
