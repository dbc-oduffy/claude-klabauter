"""Tests for the pure post_exec_tail interface."""

from __future__ import annotations

import dataclasses

import pytest

from coordinator_core.ops.review_mint import post_exec_tail as pet


def test_three_wave_shared_file_quiescence_includes_later_row():
    waves = [["A", "B"], ["C"], ["D"]]
    writes = {"A": ["x/a.py", "shared.py"], "B": ["x/b.py"], "C": ["c.py"], "D": ["shared.py"]}
    g = pet.review_groups(waves, writes)
    assert [x.index for x in g] == [0, 1, 2]
    assert g[0].rows == ("A", "B")
    assert g[0].paths == ("shared.py", "x/a.py", "x/b.py")
    assert set(g[0].quiescence_rows) == {"A", "B", "D"}
    assert g[1].quiescence_rows == ("C",)
    assert set(g[2].quiescence_rows) == {"A", "D"}


def test_group_with_all_paths_in_later_wave_still_emitted():
    g = pet.review_groups([["A"], ["B"]], {"A": ["f.py"], "B": ["f.py"]})
    assert len(g) == 2
    assert set(g[0].quiescence_rows) == {"A", "B"}


def test_empty_waves_skipped_and_indices_dense():
    g = pet.review_groups([[], ["A"], [], ["B"]], {"A": ["a"], "B": ["b"]})
    assert [x.index for x in g] == [0, 1]
    assert [x.rows for x in g] == [("A",), ("B",)]


def test_windows_separators_normalised():
    g = pet.review_groups([["A"], ["B"]], {"A": ["pkg\\m.py"], "B": ["pkg/m.py"]})
    assert g[0].paths == ("pkg/m.py",)
    assert set(g[0].quiescence_rows) == {"A", "B"}


def test_group_is_frozen():
    g = pet.review_groups([["A"]], {"A": ["a"]})[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.index = 5  # type: ignore[misc]


def test_bounded_js_budget_once_and_label():
    js = pet.bounded_js("agent('t')", 300, "test leg")
    assert js.count("300000") == 1
    assert "test leg" in js
    assert "verify budget exceeded (300s): test leg" in js
    assert "setTimeout" in js and "Promise.race" in js
    assert "_t = setTimeout" in js and ".finally(() => clearTimeout(_t))" in js
    assert "agent('t')" in js
    assert "reject" not in js


def test_bounded_js_escapes_label():
    assert '\\"q\\"' in pet.bounded_js("x", 1, 'a "q"')


def test_group_slice_prefix_names_the_range_it_freezes():
    a, b = "a" * 40, "b" * 40
    assert pet.group_slice_prefix("k", 0, a) == f"k.g0-{'a' * 12}-prep"
    assert pet.group_slice_prefix("k", 0, a) == pet.group_slice_prefix("k", 0, a)
    assert pet.group_slice_prefix("k", 0, a) != pet.group_slice_prefix("k", 0, b)


def _git(root, *args):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


def test_group_prefix_refreezes_over_its_own_prior_attempt_but_a_generic_id_still_refuses(tmp_path):
    from coordinator_core.ops import review_freeze_diff as rfd

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "seed")
    base = _head(root)

    gid = pet.group_slice_prefix("k", 0, base)
    generic = "k.g0-generic"
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert rfd._freeze_diff_worktree(root, base, gid, ["a.py"])["error"] is None
    assert rfd._freeze_diff_worktree(root, base, generic, ["a.py"])["error"] is None

    (root / "a.py").write_text("x = 3\n", encoding="utf-8")
    assert rfd._freeze_diff_worktree(root, base, gid, ["a.py"])["error"] is None
    assert list((root / "state" / "review-trail" / "diffs").glob(f"{gid}.attempt1.diff"))
    assert "already names a frozen diff" in rfd._freeze_diff_worktree(root, base, generic, ["a.py"])["error"]


def _head(root):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_budget_exceeds_invocation_budget_when_importable():
    try:
        from coordinator_core.ops.hung_test_budget import INVOCATION_BUDGET_S
    except ImportError:
        pytest.skip("hung_test_budget not present")
    assert pet.VERIFY_WALL_BUDGET_S > INVOCATION_BUDGET_S
