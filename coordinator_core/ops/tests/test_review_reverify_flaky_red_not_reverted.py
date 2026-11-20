"""review.reverify reruns the implicated reds once alone: a red that does not reproduce is flaky, never reverted."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_reverify as rr
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


def test_red_that_passes_on_the_isolated_rerun_is_flaky_and_nothing_is_reverted(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    sidecars = _edit(root, monkeypatch)
    reruns: list = []
    monkeypatch.setattr(rr, "_rerun_alone", lambda _root, ids: (reruns.append(list(ids)), list(ids))[1])
    out = _revert(root, [NODE], sidecars=sidecars)
    assert out["error"] is None
    assert out["reverted"] == [] and out["unattributed"] == []
    assert out["flaky"] == [NODE]
    assert reruns == [[NODE]]
    assert (root / "tests/test_plain.py").read_bytes().endswith(b"undefined_local\n")


def test_red_that_reproduces_is_still_reverted(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, orig, _declared = world
    sidecars = _edit(root, monkeypatch)
    monkeypatch.setattr(rr, "_rerun_alone", lambda _root, _ids: [])
    out = _revert(root, [NODE], sidecars=sidecars)
    assert [e["path"] for e in out["reverted"]] == ["tests/test_plain.py"]
    assert out["flaky"] == []
    assert (root / "tests/test_plain.py").read_bytes() == orig["tests/test_plain.py"]


def test_rerun_alone_vouches_only_the_ids_that_pass(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops import test_leg_run

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text("", encoding="utf-8")
    calls: list = []

    def fake(targets, cwd, remaining):
        calls.append(list(targets))
        return 1, ["FAILED tests/test_a.py::test_bad - boom"], "", ""

    monkeypatch.setattr(test_leg_run, "_pytest", fake)
    ids = ["tests/test_a.py::test_ok", "tests/test_a.py::test_bad", "tests/missing.py::test_x"]
    assert rr._rerun_alone(tmp_path, ids) == ["tests/test_a.py::test_ok"]
    assert calls == [ids[:2]]


def test_rerun_alone_fault_vouches_nothing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops import test_leg_run

    (tmp_path / "t.py").write_text("", encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("no runner")

    monkeypatch.setattr(test_leg_run, "_pytest", boom)
    assert rr._rerun_alone(tmp_path, ["t.py::test_a"]) == []
