"""A red only the clean-overlay mirror produces is re-verified in the real tree before it may block, and the mirror carries an untracked test's untracked imports."""
from __future__ import annotations

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import test_reach
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401 -- `world` is the shared fixture
    _ledger,
    _revert,
    _row,
    world,
)

NODE = "tests/test_plain.py::test_p"


def _edit(root, monkeypatch):
    (root / "tests/test_plain.py").write_bytes(b"def test_p():\n    assert undefined_local\n")
    return _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert undefined_local\n"))


def _passes_in_tree(monkeypatch, green):
    seen: list = []
    monkeypatch.setattr(rr, "_rerun_alone", lambda _root, ids: (seen.append(list(ids)), [n for n in ids if n in green])[1])
    return seen


def test_empty_message_red_that_passes_in_the_tree_does_not_block(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    sidecars = _edit(root, monkeypatch)
    seen = _passes_in_tree(monkeypatch, {NODE})
    out = _revert(root, [NODE], sidecars=sidecars, failing_output={NODE: ""})
    assert out["error"] is None
    assert out["mirror_only_red"] == [NODE]
    assert NODE in out["foreign_red"] and NODE not in out["unattributed"] and out["reverted"] == []
    assert seen == [[NODE]]


def test_red_the_runs_own_test_leg_passed_does_not_block_despite_a_message(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    sidecars = _edit(root, monkeypatch)
    _passes_in_tree(monkeypatch, {NODE})
    out = _revert(root, [NODE], sidecars=sidecars, failing_output={NODE: "ImportError: boom"}, leg_passed=[NODE])
    assert out["mirror_only_red"] == [NODE] and NODE in out["foreign_red"]


def test_red_that_is_also_red_in_the_tree_still_blocks(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, orig, _declared = world
    sidecars = _edit(root, monkeypatch)
    _passes_in_tree(monkeypatch, set())
    out = _revert(root, [NODE], sidecars=sidecars, failing_output={NODE: ""})
    assert out["mirror_only_red"] == [] and NODE not in out["foreign_red"]
    assert [e["path"] for e in out["reverted"]] == ["tests/test_plain.py"]
    assert (root / "tests/test_plain.py").read_bytes() == orig["tests/test_plain.py"]


def test_a_real_message_without_a_leg_pass_is_not_waived_by_a_tree_pass(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    sidecars = _edit(root, monkeypatch)
    _passes_in_tree(monkeypatch, {NODE})
    out = _revert(root, [NODE], sidecars=sidecars, failing_output={NODE: "AssertionError: real"})
    assert out["mirror_only_red"] == [] and NODE not in out["foreign_red"]
    assert out["flaky"] == [NODE]


def _repo(tmp_path):
    for rel, text in {
        "pkg/__init__.py": "",
        "pkg/tests/__init__.py": "",
        "pkg/tests/test_new.py": "from pkg import helper_new\nfrom pkg.tracked import T\n\n\ndef test_x():\n    assert helper_new.V and T\n",
        "pkg/helper_new.py": "from . import deeper_new\nV = deeper_new.D\n",
        "pkg/deeper_new.py": "D = 1\n",
        "pkg/tracked.py": "T = 1\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return {"pkg/__init__.py", "pkg/tests/__init__.py", "pkg/tracked.py"}


def test_mirror_carries_an_untracked_tests_untracked_imports_transitively(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracked = _repo(tmp_path)
    monkeypatch.setattr(test_reach, "_tracked", lambda _root: tracked)
    argv = ["python", "-m", "pytest", "pkg/tests/test_new.py::test_x"]
    named = cov.argv_named_untracked(tmp_path, argv, {"pkg/helper_new.py", "pkg/deeper_new.py"})
    assert sorted(named) == ["pkg/deeper_new.py", "pkg/helper_new.py", "pkg/tests/test_new.py"]


def test_a_peers_untracked_import_stays_out_of_the_mirror(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracked = _repo(tmp_path)
    monkeypatch.setattr(test_reach, "_tracked", lambda _root: tracked)
    argv = ["python", "-m", "pytest", "pkg/tests/test_new.py"]
    assert cov.argv_named_untracked(tmp_path, argv, {"pkg/helper_new.py"}) == ["pkg/tests/test_new.py", "pkg/helper_new.py"]
    assert cov.argv_named_untracked(tmp_path, argv) == ["pkg/tests/test_new.py"]


def test_no_untracked_test_names_no_import(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracked = _repo(tmp_path) | {"pkg/tests/test_new.py"}
    monkeypatch.setattr(test_reach, "_tracked", lambda _root: tracked)
    assert cov.argv_named_untracked(tmp_path, ["pytest", "pkg/tests/test_new.py"]) == []


def test_collection_failure_on_an_untracked_module_is_named_a_mirror_gap(tmp_path) -> None:
    _repo(tmp_path)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    out = "E   ModuleNotFoundError: No module named 'pkg.helper_new'"
    assert "pkg/helper_new.py" in cov.mirror_gap(out, mirror, set(), tmp_path)
    assert cov.mirror_gap(out, mirror, set()) == ""
    assert cov.mirror_gap("E   ModuleNotFoundError: No module named 'nowhere'", mirror, set(), tmp_path) == ""
