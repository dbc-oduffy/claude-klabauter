"""Pins `coordinator_core/conftest.py`'s live cross-repo-inbox write guard.

The guard exists because eighteen synthetic fixture memos, sent from
`claude-klabauter-engine` with names matching this repo's own `test_memo_list.py`
fixtures, were delivered into example-retrieval-repo's REAL `state/cross-repo/inbox/` on
2026-09-04 — the `COORDINATOR_SETTINGS_HOME` vector `_quarantine_real_home`
did not close until two days later. See
`docs/plans/2026-09-12-stop-engine-memo-fixtures-reaching-a-liv.md` (C2).

Same shape as `test_live_session_hub_litter_guard.py`: both verdicts pinned
via the fixture's `.__wrapped__()` generator, plus a third check on the
import-time root-resolution leg itself — a deliberate write into a STUBBED
root only proves the diff logic, never the resolution that actually leaked.
"""

from __future__ import annotations

import pytest

from coordinator_core import conftest as cc_conftest


def test_a_write_into_a_stubbed_live_inbox_root_fails_loudly(tmp_path, monkeypatch):
    """Red verdict: a deliberate write through the `_live_inbox_roots()` seam
    trips the guard."""
    root = tmp_path / "peer-inbox"
    root.mkdir()
    monkeypatch.setattr(cc_conftest, "_live_inbox_roots", lambda: (str(root),))
    gen = cc_conftest._no_live_inbox_writes_from_suite.__wrapped__()
    next(gen)
    (root / "leaked-memo.md").write_text("x", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="leaked-memo.md"):
        try:
            next(gen)
        except StopIteration:
            pass


def test_a_clean_test_does_not_trip_the_guard(tmp_path, monkeypatch):
    """Green verdict: a test that writes nothing into the stubbed root passes
    through untouched. A liveness instrument armed without its clean leg
    proven is not proven — an always-red guard would look identical to this
    plan's fix from the outside."""
    root = tmp_path / "peer-inbox"
    root.mkdir()
    monkeypatch.setattr(cc_conftest, "_live_inbox_roots", lambda: (str(root),))
    gen = cc_conftest._no_live_inbox_writes_from_suite.__wrapped__()
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)


def test_an_absent_root_is_not_an_error(tmp_path, monkeypatch):
    """A stubbed (or real) root that does not exist on disk is a no-op, not a
    failure — mirrors `_no_live_state_corpus_writes`'s tolerance for an
    absent live dir."""
    monkeypatch.setattr(
        cc_conftest, "_live_inbox_roots", lambda: (str(tmp_path / "nope"),)
    )
    gen = cc_conftest._no_live_inbox_writes_from_suite.__wrapped__()
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)


def test_import_time_root_set_includes_this_repos_own_inbox():
    """The resolution leg itself, not just the diff logic: the import-time
    root set must contain THIS repo's own `state/cross-repo/inbox/` (or the
    legacy `cross-repo/inbox/`) — the leg a stubbed-root test can never
    exercise, and the one that actually leaked (example-retrieval-repo's inbox, reached
    through registry resolution, not a repo-local write)."""
    roots = cc_conftest._LIVE_INBOX_ROOTS
    assert any(root.endswith(("cross-repo/inbox", "cross-repo\\inbox")) for root in roots), (
        f"import-time root set {roots!r} does not include this repo's own inbox"
    )


def test_import_time_root_set_includes_a_peer_when_a_registry_exists():
    """SKIPPED (not passed) when no machine-local registry is configured on
    this box — a registry-less box must not be able to silently satisfy this
    check by having nothing to resolve."""
    from coordinator_core.ops.fleet import _memo_resolver

    try:
        registered = _memo_resolver.read_registry_repos()
    except Exception:
        registered = {}
    if not registered:
        pytest.skip("no machine-local registry configured on this box")
    roots = cc_conftest._LIVE_INBOX_ROOTS
    # This repo's own inbox is always roots[0]; at least one more root beyond
    # it means a peer receiver root resolved.
    assert len(roots) > 1, (
        f"registry has {len(registered)} repo(s) but import-time root set "
        f"{roots!r} resolved no peer receiver root"
    )
