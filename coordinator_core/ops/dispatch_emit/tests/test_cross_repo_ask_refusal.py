"""Branch coverage for cross_repo_ask_refusal.cross_repo_run_refusal."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import cross_repo_ask_refusal as m


@pytest.fixture
def roots(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    return a, b


@pytest.fixture
def armed(monkeypatch):
    from coordinator_core.bash_guards import _write_bump_applicability as app
    from coordinator_core.bash_guards import _write_bump_stand_down as sd

    monkeypatch.setattr(app, "bump_applies", lambda s, c, e: True)
    monkeypatch.setattr(sd, "environment_stands_the_bump_down", lambda e: None)
    return app, sd


def _call(run, sess, **kw):
    base = dict(research_route=False, session_id="s", cwd=None, env=None)
    base.update(kw)
    return m.cross_repo_run_refusal(run, sess, **base)


def test_refuses_names_both_roots_and_route(roots, armed):
    a, b = roots
    text = _call(a, b)
    assert text and str(a) in text and str(b) in text
    assert "Fire it from a session rooted in" in text


def test_admit_no_session_root(roots, armed):
    assert _call(roots[0], None) is None


def test_admit_same_checkout_and_variant(roots, armed):
    a, _ = roots
    assert _call(a, a) is None
    assert _call(a, a / "sub" / "..") is None
    assert _call(a, type(a)(str(a) + "/")) is None


def test_admit_research_route(roots, armed):
    assert _call(*roots, research_route=True) is None


def test_admit_no_session_id(roots, armed):
    assert _call(*roots, session_id=None) is None


def test_admit_bump_not_applicable(roots, armed, monkeypatch):
    monkeypatch.setattr(armed[0], "bump_applies", lambda s, c, e: False)
    assert _call(*roots) is None


def test_admit_environment_stands_down(roots, armed, monkeypatch):
    monkeypatch.setattr(armed[1], "environment_stands_the_bump_down", lambda e: object())
    assert _call(*roots) is None
