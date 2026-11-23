"""Branch coverage for cross_repo_ask_refusal.cross_repo_run_refusal."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import cross_repo_ask_refusal as m
from pathlib import Path


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    return a, b


def _call(run, sess, **kw):
    base = dict(research_route=False, approved=False)
    base.update(kw)
    return m.cross_repo_run_refusal(run, sess, **base)


def test_refuses_names_both_roots_and_route(roots) -> None:
    a, b = roots
    text = _call(a, b)
    assert text and str(a) in text and str(b) in text
    assert "a session rooted in" not in text, "where a session sits is never a remedy"
    assert "clone of one" in text
    assert "cross_repo_approved: true" in text
    assert "by hand" not in text.lower()


def test_admit_no_session_root(roots) -> None:
    assert _call(roots[0], None) is None


def test_admit_same_checkout_and_variant(roots) -> None:
    a, _ = roots
    assert _call(a, a) is None
    assert _call(a, a / "sub" / "..") is None
    assert _call(a, type(a)(str(a) + "/")) is None


def test_admit_research_route(roots) -> None:
    assert _call(*roots, research_route=True) is None


def test_admit_approved(roots) -> None:
    assert _call(*roots, approved=True) is None


def test_refused_when_not_approved(roots) -> None:
    assert _call(*roots, approved=False)
