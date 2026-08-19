"""test_expand_untracked — fast-tier coverage of
`coordinator_core.workstream_complete._expand_untracked`, the untracked-path
expansion that feeds the review-scale measurement.

Separate file, not a section of `test_directives_review_scale.py`: that module
carries a module-level `pytestmark` of `cadence` + `spawns_process` because most
of its cases build a real git repo, and a module-level marker applies per FILE.
`_expand_untracked` is pure directory logic with no spawn site, so pinning it
there would fire this regression net only at cadence gates. The cases that do
need a git repo (the end-to-end measurement assertions) stay in that file.

Spec backlink: `_expand_untracked`'s own docstring — an untracked DIRECTORY
must never reach `_count_lines`, which opens its argument as a file, converts
the resulting `OSError` to `None`, and collapses the whole review-scale
four-tuple to unresolved. That failure direction (less review, not more) is
what `_measure_session_review_scale_inputs`'s negative-spec forbids.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core import workstream_complete as wsc


def test_ordinary_file_passes_through_as_itself(tmp_path):
    """A plain untracked file expands to exactly itself — the expansion is a
    directory carve-out, not a rewrite of the ordinary path."""
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    assert wsc._expand_untracked(tmp_path, "real.py") == ["real.py"]


def test_missing_path_stays_a_populated_singleton(tmp_path):
    """A path that does not exist must stay a one-entry list so `_count_lines`
    still resolves it to `None` and the four-tuple still collapses.

    Negative-spec: NOT `[]`. An empty expansion is the shape a naive
    `if not target.exists(): return []` would produce, and it would silently
    score an unreadable path as zero LOC instead of propagating the
    measurement failure."""
    assert wsc._expand_untracked(tmp_path, "ghost.py") == ["ghost.py"]


def test_directory_expands_to_the_files_beneath_it(tmp_path):
    """`git status --porcelain` reports a wholly-untracked directory as one
    trailing-slash entry; that entry must become its member files."""
    brief = tmp_path / "briefs" / "a-brief"
    brief.mkdir(parents=True)
    (brief / "one.py").write_text("a = 1\n", encoding="utf-8")
    (brief / "two.py").write_text("b = 2\n", encoding="utf-8")
    (brief / "nested").mkdir()
    (brief / "nested" / "three.py").write_text("c = 3\n", encoding="utf-8")

    assert wsc._expand_untracked(tmp_path, "briefs/a-brief/") == [
        "briefs/a-brief/nested/three.py",
        "briefs/a-brief/one.py",
        "briefs/a-brief/two.py",
    ]


def test_walk_stops_at_the_shared_budget(tmp_path):
    """The budget is a single counter shared across one measurement's whole
    untracked list, so several large sibling directories compound against ONE
    bound rather than each getting a fresh one."""
    for name in ("first", "second"):
        target = tmp_path / name
        target.mkdir()
        for i in range(4):
            (target / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")

    budget = [6]
    first = wsc._expand_untracked(tmp_path, "first", budget=budget)
    second = wsc._expand_untracked(tmp_path, "second", budget=budget)
    assert first is not None and second is not None

    assert len(first) == 4
    assert len(second) == 2, "the second directory did not draw on the same budget"
    assert budget[0] == 0


def test_absent_budget_walks_the_whole_directory(tmp_path):
    """`budget=None` is unbounded — the parameter is opt-in, so callers that
    do not pass one are unaffected."""
    target = tmp_path / "many"
    target.mkdir()
    for i in range(20):
        (target / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")

    members = wsc._expand_untracked(tmp_path, "many")
    assert members is not None
    assert len(members) == 20


def test_symlinked_subdirectory_is_not_followed(tmp_path):
    """A symlink loop must terminate rather than walk forever.

    Pinned rather than inherited: `Path.rglob`'s symlink behaviour is
    CPython-version-sensitive, which is why the implementation states
    `os.walk(followlinks=False)` explicitly."""
    target = tmp_path / "tree"
    target.mkdir()
    (target / "real.py").write_text("x = 1\n", encoding="utf-8")
    try:
        os.symlink(target, target / "loop", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink creation not permitted on this host")

    members = wsc._expand_untracked(tmp_path, "tree")

    assert members == ["tree/real.py"], members


def test_unwalkable_directory_returns_none(tmp_path, monkeypatch):
    """An `OSError` raised DURING the walk (permission denied on a
    subdirectory, or the tree vanishing mid-iteration) must return `None` so
    the caller collapses the measurement rather than under-counting it."""
    target = tmp_path / "tree"
    target.mkdir()
    (target / "real.py").write_text("x = 1\n", encoding="utf-8")

    def _raising_walk(*_args, **_kwargs):
        yield str(target), [], ["real.py"]
        raise PermissionError("denied mid-walk")

    monkeypatch.setattr(wsc.os, "walk", _raising_walk)

    assert wsc._expand_untracked(tmp_path, "tree") is None
