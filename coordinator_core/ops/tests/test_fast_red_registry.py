"""Tests for coordinator_core.ops.fast_red_registry."""
from __future__ import annotations

from datetime import date

import pytest

from coordinator_core.ops.fast_red_registry import (
    compute_delta,
    load_registry,
    registry_problems,
)

TODAY = date(2026, 10, 1)


def _cluster(**kw):
    c = {
        "shape": "assertion",
        "disposition": "fix",
        "owner": "owner.md",
        "reason": "r",
        "review_by": "2026-12-01",
        "first_seen": "2026-10-01",
    }
    c.update(kw)
    return c


def _reg(**cluster_kw):
    return {"clusters": {"c": _cluster(**cluster_kw)}, "entries": {"a/t.py::t1": "c", "b/t.py::t2": "c"}}


def _delta(failing, reg, platform="win32", scope=None):
    return compute_delta(failing, reg, platform=platform, scope=scope)


def test_unregistered_is_new():
    assert _delta(["z.py::t"], _reg()).new == ("z.py::t",)


def test_registered_is_standing():
    d = _delta(["a/t.py::t1"], _reg())
    assert d.standing == ("a/t.py::t1",) and d.new == ()


def test_win32_cluster_as_linux_is_new():
    d = _delta(["a/t.py::t1"], _reg(platforms=["win32"]), platform="linux")
    assert d.new == ("a/t.py::t1",)


def test_intermittent_excluded_from_fixed():
    assert _delta([], _reg(intermittent=True)).fixed == ()


def test_scope_prefix_limits_fixed():
    assert _delta([], _reg(), scope=["a/"]).fixed == ("a/t.py::t1",)


def test_scope_none_computes_fixed_over_all():
    assert _delta([], _reg()).fixed == ("a/t.py::t1", "b/t.py::t2")


def test_missing_registry_raises(tmp_path):
    with pytest.raises(ValueError):
        load_registry(tmp_path / "nope.yaml")


def _probs(reg, tmp_path):
    return registry_problems(reg, today=TODAY, repo_root=tmp_path)


def test_clean_registry(tmp_path):
    (tmp_path / "owner.md").write_text("x")
    assert _probs(_reg(), tmp_path) == []


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"shape": "bogus"}, "shape"),
        ({"disposition": "bogus"}, "disposition"),
        ({"owner": ""}, "owner is empty"),
        ({"owner": "missing.md"}, "does not resolve"),
        ({"reason": " "}, "reason is empty"),
        ({"review_by": "2026-09-30"}, "past"),
    ],
)
def test_problem_arms(tmp_path, kw, needle):
    (tmp_path / "owner.md").write_text("x")
    assert any(needle in p for p in _probs(_reg(**kw), tmp_path))


def test_unknown_cluster_and_empty_cluster(tmp_path):
    (tmp_path / "owner.md").write_text("x")
    reg = {"clusters": {"c": _cluster()}, "entries": {"n": "ghost"}}
    out = _probs(reg, tmp_path)
    assert any("unknown cluster" in p for p in out)
    assert any("no entries" in p for p in out)
