"""Engine banner: names the published engine, or says to fetch it; never a claude-klabauter tree."""

from __future__ import annotations

from coordinator_core.hooks import project_orientation as po

_FETCH = "Engine: none - fetch claude-klabauter (github.com/dbc-oduffy/claude-klabauter)"


def _banner(monkeypatch, root, klass) -> str:
    monkeypatch.setattr(po, "_resolve_engine_root_with_class", lambda: (root, klass))
    monkeypatch.setattr(po, "_branch_boot", lambda _r: "main")
    out: list = []
    po._engine_resolution_banner(out)
    return "".join(out)


def test_published_engine_names_the_mirror(monkeypatch):
    text = _banner(monkeypatch, "/x/klabauter", po.RESOLUTION_RESOLVED_ENGINE)
    assert "published mirror" in text
    assert "/x/klabauter" in text
    assert "fetch claude-klabauter" not in text


def test_live_tree_is_never_reported_as_the_engine(monkeypatch):
    text = _banner(monkeypatch, "/x/live", po.RESOLUTION_LIVE_WORKING_TREE)
    assert _FETCH in text
    assert "live tree" not in text
    assert "/x/live" not in text


def test_unresolved_engine_says_how_to_fetch(monkeypatch):
    assert _FETCH in _banner(monkeypatch, None, None)


def test_resolver_raising_degrades_to_fetch_banner(monkeypatch):
    def boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(po, "coordinator_engine_root_with_class", boom)
    out: list = []
    po._engine_resolution_banner(out)
    assert _FETCH in "".join(out)

