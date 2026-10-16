"""Tests for _gem_cross_repo_commit: the scoped-git-write predicate and standing read."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import _gem_cross_repo_commit as mod
from coordinator_core.bash_guards._gem_cross_repo_commit import (
    git_write_is_gem_scoped,
    session_holds_live_gem_standing,
)
from coordinator_core.group_em import nomination


@pytest.mark.parametrize(
    "sub,args",
    [
        ("commit", ["-m", "x", "--", "a.py"]),
        ("commit", ["-m", "x", "a.py"]),
        ("commit", ["-mfix", "a.py"]),
        ("commit", ["--message=x", "a.py"]),
        ("commit", ["--message", "x", "a.py"]),
        ("add", ["a.py", "b.py"]),
        ("add", ["--", "a.py"]),
        ("mv", ["a", "b"]),
    ],
)
def test_scoped(sub, args):
    assert git_write_is_gem_scoped(sub, args) is True


@pytest.mark.parametrize(
    "sub,args",
    [
        ("commit", ["-m", "x"]),
        ("commit", ["-a", "-m", "x", "f"]),
        ("commit", ["-am", "x", "f"]),
        ("commit", ["--all", "-m", "x", "f"]),
        ("commit", ["--amend", "f"]),
        ("commit", ["--amen", "-m", "x", "f"]),
        ("commit", ["--al", "-m", "x", "f"]),
        ("commit", ["-m", "x", "--"]),
        ("add", ["--upd", "a.py"]),
        ("commit", []),
        ("add", ["-A"]),
        ("add", ["--all"]),
        ("add", ["-u"]),
        ("add", ["."]),
        ("add", [":/"]),
        ("add", ["a.py", "."]),
        ("add", ["a.py", ":(top)x"]),
        ("add", []),
        ("push", []),
        ("reset", ["f"]),
        ("rm", ["f"]),
        ("rebase", ["main"]),
    ],
)
def test_unscoped(sub, args):
    assert git_write_is_gem_scoped(sub, args) is False


def _patch(monkeypatch, verdict):
    monkeypatch.setattr(nomination, "identify", lambda root: verdict)


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ({"state": "live", "holder": {"session_id": "S1"}}, True),
        ({"state": "live", "holder": {"session_id": "S2"}}, False),
        ({"state": "ended", "holder": {"session_id": "S1"}}, False),
        ({"state": "unverified", "holder": {"session_id": "S1"}}, False),
        ({"state": "vacant", "holder": None}, False),
    ],
)
def test_standing_matrix(monkeypatch, tmp_path, verdict, expected):
    _patch(monkeypatch, verdict)
    assert session_holds_live_gem_standing("S1", str(tmp_path)) is expected


def test_standing_empty_inputs(monkeypatch, tmp_path):
    _patch(monkeypatch, {"state": "live", "holder": {"session_id": "S1"}})
    assert session_holds_live_gem_standing("", str(tmp_path)) is False
    assert session_holds_live_gem_standing("S1", "") is False
    assert session_holds_live_gem_standing("S1", None) is False


def test_standing_exception_is_false(monkeypatch, tmp_path):
    def boom(root):
        raise RuntimeError("x")

    monkeypatch.setattr(nomination, "identify", boom)
    assert session_holds_live_gem_standing("S1", str(tmp_path)) is False


def test_module_has_no_subprocess():
    assert "subprocess" not in Path(mod.__file__).read_text(encoding="utf-8")
