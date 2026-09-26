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
    monkeypatch.setattr(_mod, "_ENGINE_SPLIT_ANNOUNCED", False, raising=False)


def _announce(monkeypatch, capsys, cli_root, dispatch_root):
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: cli_root)
    _mod._announce_engine_cli_split(dispatch_root)
    return capsys.readouterr().err


class TestItSpeaksOnlyWhenTheTreesDisagree:
    def test_a_split_names_both_roots_on_one_line(self, monkeypatch, capsys):
        err = _announce(monkeypatch, capsys, r"X:\a-working-tree", r"X:\a-mirror")

        assert err.count("\n") == 1, f"exactly one line, got: {err!r}"
        assert r"X:\a-working-tree" in err, "the CLI root must be named"
        assert r"X:\a-mirror" in err, "the engine root must be named"

    def test_agreement_is_silent(self, monkeypatch, capsys):
        assert _announce(monkeypatch, capsys, r"X:\same", r"X:\same") == ""

    def test_agreement_is_silent_across_separator_and_case_spelling(
        self, monkeypatch, capsys
    ):
        assert _announce(monkeypatch, capsys, "X:/Same/Tree", r"X:\same\tree") == ""

    def test_realpath_class_spellings_resolve_to_the_same_tree(
        self, monkeypatch, capsys
    ):
        canonical = r"X:\canonical-tree"
        fake_names = {
            r"X:\PROGRA~1\short-name-tree": canonical,
            r"X:\junction-to-tree": canonical,
        }
        monkeypatch.setattr(
            _mod.os.path, "realpath", lambda p: fake_names.get(p, p)
        )
        err = _announce(
            monkeypatch,
            capsys,
            r"X:\PROGRA~1\short-name-tree",
            r"X:\junction-to-tree",
        )
        assert err == "", f"realpath-equivalent trees must not announce a split, got: {err!r}"

    def test_it_speaks_once_per_process(self, monkeypatch, capsys):
        monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: r"X:\cli")
        _mod._announce_engine_cli_split(r"X:\engine")
        first = capsys.readouterr().err
        _mod._announce_engine_cli_split(r"X:\engine")
        second = capsys.readouterr().err

        assert first.strip(), "the first call must speak"
        assert second == "", "the second must not — one line per process, not per call"


class TestItNeverTakesADispatchDown:

    def test_a_resolver_that_raises_is_swallowed(self, monkeypatch, capsys):
        def _boom(_f):
            raise RuntimeError("no checkout found")

        monkeypatch.setattr(_mod, "resolve_engine_root", _boom)
        _mod._announce_engine_cli_split(r"X:\engine")

        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("cli_root, dispatch_root", [(None, r"X:\e"), (r"X:\c", "")])
    def test_an_unresolvable_root_says_nothing_rather_than_guessing(
        self, monkeypatch, capsys, cli_root, dispatch_root
    ):
        assert _announce(monkeypatch, capsys, cli_root, dispatch_root) == ""
