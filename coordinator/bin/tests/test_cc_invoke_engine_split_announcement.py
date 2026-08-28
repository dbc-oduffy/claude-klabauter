"""test_cc_invoke_engine_split_announcement.py — the one line that tells an
operator which tree is actually deciding.

`require_dispatch_engine_on_path` resolves the DISPATCH axis (the published
mirror on a conformant box); every CLI-root resolver answers the LOCATOR axis
(the live working tree). Its own docstring records that the two ladders return
different roots BY DESIGN. Nothing said so out loud, and that silence has a
measured cost: on 2026-08-28 a repair to `/handoff` landed in the working tree
and `/handoff` stayed broken through this seam for three runs, because the
mirror was behind and the CLI binds the mirror's `coordinator_core`. Two
sessions read it as a bad fix. The diagnostic an operator reaches for —
"which tree am I running" — answers `live-working-tree`, truthfully, about the
CLI, and so confirms the wrong conclusion.

What is pinned here, and the two halves are the point:

- when the roots DIFFER, exactly one line naming BOTH reaches stderr; and
- when they AGREE, nothing is emitted at all.

The second half is what keeps this from becoming noise a reader learns to skip.
A single-tree box must see nothing, or the line stops carrying information on
the box where it matters. Both live in one module so a later edit that makes
the emitter unconditional turns the negative red rather than passing quietly.

Also pinned: emission is once per process, and any failure inside the emitter is
swallowed. This runs on the dispatch hot path of ~200 CLIs; a broken stderr, an
unresolvable locator root, or a surprise from the resolver must never take a
dispatch down for the sake of an advisory.

Negative-spec: says nothing about `ProvenanceDivergenceError`, which is a
DIFFERENT divergence on the same seam (coordinator_core already bound from a
third tree, which IS a defect and DOES raise) — that is
test_cc_invoke_provenance_hardening.py's subject.

Run: pytest coordinator/bin/tests/test_cc_invoke_engine_split_announcement.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = pytest.mark.cadence


@pytest.fixture(autouse=True)
def _rearm(monkeypatch):
    """The emitter fires at most once per PROCESS, so the module-level latch
    has to be cleared between cases or every test after the first would pass by
    silence — which is exactly the failure the agree-case asserts, making it
    indistinguishable from a real pass."""
    monkeypatch.setattr(_mod, "_ENGINE_SPLIT_ANNOUNCED", False, raising=False)


def _announce(monkeypatch, capsys, cli_root, dispatch_root):
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: cli_root)
    _mod._announce_engine_cli_split(dispatch_root)
    return capsys.readouterr().err


class TestItSpeaksOnlyWhenTheTreesDisagree:
    def test_a_split_names_both_roots_on_one_line(self, monkeypatch, capsys):
        err = _announce(monkeypatch, capsys, r"X:\a-working-tree", r"X:\a-mirror")  # abs-path-ok: synthetic fixture, never resolved on disk

        assert err.count("\n") == 1, f"exactly one line, got: {err!r}"
        assert r"X:\a-working-tree" in err, "the CLI root must be named"  # abs-path-ok: synthetic fixture, never resolved on disk
        assert r"X:\a-mirror" in err, "the engine root must be named"  # abs-path-ok: synthetic fixture, never resolved on disk

    def test_agreement_is_silent(self, monkeypatch, capsys):
        """A single-tree box sees nothing. If this goes red, the line has become
        noise on every box and stops being read on the one box where the split
        is real."""
        assert _announce(monkeypatch, capsys, r"X:\same", r"X:\same") == ""  # abs-path-ok: synthetic fixture, never resolved on disk

    def test_agreement_is_silent_across_separator_and_case_spelling(
        self, monkeypatch, capsys
    ):
        """The two ladders answer the same tree in different spellings — one
        returns backslashes, the other forward slashes, and the drive letter's
        case is not stable between them — so a naive string compare would
        announce a split on a single-tree box every single time."""
        assert _announce(monkeypatch, capsys, "X:/Same/Tree", r"X:\same\tree") == ""  # abs-path-ok: synthetic fixture, never resolved on disk

    def test_it_speaks_once_per_process(self, monkeypatch, capsys):
        monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: r"X:\cli")  # abs-path-ok: synthetic fixture, never resolved on disk
        _mod._announce_engine_cli_split(r"X:\engine")  # abs-path-ok: synthetic fixture, never resolved on disk
        first = capsys.readouterr().err
        _mod._announce_engine_cli_split(r"X:\engine")  # abs-path-ok: synthetic fixture, never resolved on disk
        second = capsys.readouterr().err

        assert first.strip(), "the first call must speak"
        assert second == "", "the second must not — one line per process, not per call"


class TestItNeverTakesADispatchDown:
    """The safety half. This sits on the dispatch path of ~200 CLIs and is
    advisory; a raise here would convert a cosmetic problem into an outage."""

    def test_a_resolver_that_raises_is_swallowed(self, monkeypatch, capsys):
        def _boom(_f):
            raise RuntimeError("no checkout found")

        monkeypatch.setattr(_mod, "resolve_engine_root", _boom)
        _mod._announce_engine_cli_split(r"X:\engine")  # abs-path-ok: synthetic fixture, never resolved on disk

        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("cli_root, dispatch_root", [(None, r"X:\e"), (r"X:\c", "")])  # abs-path-ok: synthetic fixture, never resolved on disk
    def test_an_unresolvable_root_says_nothing_rather_than_guessing(
        self, monkeypatch, capsys, cli_root, dispatch_root
    ):
        """Half an answer is worse than none here: naming one root and a blank
        would read as a split against an empty tree."""
        assert _announce(monkeypatch, capsys, cli_root, dispatch_root) == ""
