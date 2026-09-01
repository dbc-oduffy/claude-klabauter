# test_name_ladder — pins coordinator_core.session.name_ladder.resolve_name
# in isolation, plus the DRIFT-IMPOSSIBLE property this extraction exists
# for: session-claim-cli.py's `_render_claimant_name` and dispatch_checks.
# py's `_resolve_owner_writer_name` both delegate rung/reason resolution to
# this module, so they cannot answer differently for the same input again
# (state/debt-backlog/2026-09-01-shared-name-resolution-ladder-for-sessio-
# 026b33fcd43d.yaml). Each surface's own RENDERING (markers, prose, byte
# budget) is pinned by its own test suite, not here.
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from coordinator_core.session import name_ladder
from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session.scope import OwnerFact


_BIN_DIR = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


def _load_cli_module():
    # session-claim-cli.py doesn't sit on sys.path as an importable module
    # (hyphenated filename) -- same load idiom as
    # coordinator/bin/tests/test_session_claim_cli.py's `_load_cli_module`.
    loader = importlib.machinery.SourceFileLoader(
        "session_claim_cli_for_ladder_test", str(_BIN_DIR / "session-claim-cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _LookupResult:
    def __init__(self, path, sid, recorded_name):
        self.recorded_name = {path: {sid: recorded_name}} if recorded_name else {}
        self.edit_ts = {}


class _Record:
    def __init__(self, name):
        self.name = name


def test_rung1_recorded_name_wins_without_calling_lookup():
    def _boom(sid):
        raise AssertionError("rung 2 must not be consulted when rung 1 resolves")

    name, rung, reason = name_ladder.resolve_name("alice", "sid-1", _boom)
    assert (name, rung, reason) == ("alice", name_ladder.RUNG_RECORDED, None)


def test_rung2_live_lookup_when_no_recorded_name():
    name, rung, reason = name_ladder.resolve_name(
        None, "sid-2", lambda sid: _Record("bob")
    )
    assert (name, rung, reason) == ("bob", name_ladder.RUNG_LIVE_LOOKUP, None)


def test_rung3_no_registry_record():
    name, rung, reason = name_ladder.resolve_name(None, "sid-3", lambda sid: None)
    assert (name, rung, reason) == (
        None,
        name_ladder.RUNG_UNRESOLVED,
        name_ladder.REASON_NO_REGISTRY_RECORD,
    )


def test_rung3_lookup_raises_degrades_never_propagates():
    def _raise(sid):
        raise RuntimeError("registry unavailable")

    name, rung, reason = name_ladder.resolve_name(None, "sid-4", _raise)
    assert (name, rung, reason) == (
        None,
        name_ladder.RUNG_UNRESOLVED,
        name_ladder.REASON_LOOKUP_FAILED,
    )


def test_rung3_record_resolves_but_carries_no_name():
    name, rung, reason = name_ladder.resolve_name(
        None, "sid-5", lambda sid: _Record(None)
    )
    assert (name, rung, reason) == (
        None,
        name_ladder.RUNG_UNRESOLVED,
        name_ladder.REASON_UNNAMED_RECORD,
    )


@pytest.mark.parametrize(
    "recorded_name,lookup,expect_name_resolves,expected_name",
    [
        ("carol", lambda sid: (_ for _ in ()).throw(AssertionError("unreachable")),
         True, "carol"),
        (None, lambda sid: _Record("dave"), True, "dave"),
        (None, lambda sid: None, False, None),
    ],
)
def test_both_surfaces_agree_on_rung_and_reason_for_the_same_input(
    monkeypatch, recorded_name, lookup, expect_name_resolves, expected_name
):
    """The deliverable this extraction exists for: drive the SAME input
    through BOTH real call sites -- `session-claim-cli._render_claimant_name`
    and `dispatch_checks._resolve_owner_writer_name` -- rather than calling
    the shared resolver directly (a tautology that pins nothing about
    either surface actually delegating to it), and assert they agree on
    whether a name resolves and, when it does, on the exact name."""
    sid = "sid-shared"
    path = "/some/path"

    class _StubHarnessRegistry:
        def lookup(self, sid):
            return lookup(sid)

    monkeypatch.setattr(_cli, "_import_harness_registry_module", lambda: _StubHarnessRegistry())
    cli_rendering = _cli._render_claimant_name(sid, path, _LookupResult(path, sid, recorded_name))

    fact = OwnerFact(owner=sid, liveness="live", claim_source="session", writer_name=recorded_name)
    monkeypatch.setattr("coordinator_core.session.harness_registry.lookup", lookup)
    guard_name = dispatch_checks._resolve_owner_writer_name(fact)

    if expect_name_resolves:
        assert expected_name in cli_rendering, cli_rendering
        assert guard_name == expected_name
    else:
        assert guard_name is None
        # CLI rendering carries one of its three distinct rung-3 markers --
        # never a resolved name -- for the same "nothing resolved" input.
        assert cli_rendering in (
            _cli._NO_REGISTRY_RECORD_MARKER,
            _cli._NAME_UNRESOLVED_MARKER,
            _cli._UNNAMED_MARKER,
        )
