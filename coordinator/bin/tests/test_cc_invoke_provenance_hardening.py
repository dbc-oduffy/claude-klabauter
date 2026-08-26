"""test_cc_invoke_provenance_hardening.py — AC for C9, "Harden the seam, and
fix the carriers that cannot take it".

Chunk: docs/plans/2026-08-26-the-seam-reports-what-it-got.md § C9

C9's second half turns a surviving `divergent` verdict at the DISPATCH seam
(`require_dispatch_engine_on_path` — the collapse target C8's ten inspected
carriers all call) into a loud `RuntimeError`, landable because C8
(docs/research/engine-provenance-carrier-dependence.md) read every one of
those ten carriers as `does-not-depend`: none of their touched
`coordinator_core` surface differs behaviourally between the working tree
and the published mirror, so hardening this seam cannot silently break any
of them. This file pins that raise (and its scope): it fires only on
`divergent`, only at `require_dispatch_engine_on_path`, never at
`ensure_engine_on_path` (hard constraint — its documented contract is to
degrade to `None` on an engine-less install), and never at the locator-axis
wrappers (`require_engine_on_path`, `require_colocated_engine_on_path`),
which this chunk's evidence does not cover.

Negative-spec: does not re-test `provenance_against`'s own verdict logic
(C2's job) or the reporting-seam wiring itself (C4's job,
test_cc_invoke_provenance_reporting_seams.py) — this file only tests the
NEW raise this chunk adds on top of that existing wiring.

Run: pytest coordinator/bin/tests/test_cc_invoke_provenance_hardening.py -q
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


@pytest.fixture
def clean_sys_path():
    """Restores `sys.path` to its pre-test contents afterward — the wrapper
    under test inserts a resolved root onto `sys.path`
    (`_front_insert_on_path`) before the divergence check runs, and several
    cases here feed it sentinel/fake roots that must not leak into sibling
    tests."""
    before = list(sys.path)
    try:
        yield
    finally:
        sys.path[:] = before


def _fake_provenance(verdict, imported_file="/some/other/tree/coordinator_core/__init__.py", engine_root=None):
    def _fake(*, root):
        return _mod.EngineProvenance(verdict, imported_file, engine_root if engine_root is not None else root)

    return _fake


# ---------------------------------------------------------------------------
# The raise itself: require_dispatch_engine_on_path fails loud on divergent.
# ---------------------------------------------------------------------------


def test_require_dispatch_engine_on_path_raises_on_divergent(monkeypatch, tmp_path, clean_sys_path):
    root = tmp_path / "dispatch-root-divergent"
    root.mkdir()
    monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: str(root))
    monkeypatch.setattr(
        _mod,
        "provenance_against",
        _fake_provenance(_mod.PROVENANCE_DIVERGENT, engine_root=str(root)),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _mod.require_dispatch_engine_on_path()

    message = str(excinfo.value)
    assert "require_dispatch_engine_on_path" in message
    assert str(root) in message


def test_require_dispatch_engine_on_path_raise_names_both_paths(monkeypatch, tmp_path, clean_sys_path):
    """The raised message must carry both the divergent imported_file and the
    resolved root — an operator debugging this needs both trees named, not
    just one."""
    root = tmp_path / "dispatch-root-divergent-2"
    root.mkdir()
    imported = str(tmp_path / "other-tree" / "coordinator_core" / "__init__.py")
    monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: str(root))
    monkeypatch.setattr(
        _mod,
        "provenance_against",
        _fake_provenance(_mod.PROVENANCE_DIVERGENT, imported_file=imported, engine_root=str(root)),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _mod.require_dispatch_engine_on_path()

    message = str(excinfo.value)
    assert imported in message
    assert str(root) in message


# ---------------------------------------------------------------------------
# Every other verdict is unaffected — the raise is divergent-only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["match", "unimported", "unresolved"])
def test_require_dispatch_engine_on_path_does_not_raise_on_non_divergent_verdicts(
    monkeypatch, tmp_path, clean_sys_path, verdict
):
    verdict_const = {
        "match": _mod.PROVENANCE_MATCH,
        "unimported": _mod.PROVENANCE_UNIMPORTED,
        "unresolved": _mod.PROVENANCE_UNRESOLVED,
    }[verdict]
    root = tmp_path / f"dispatch-root-{verdict}"
    root.mkdir()
    monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: str(root))
    monkeypatch.setattr(_mod, "provenance_against", _fake_provenance(verdict_const, engine_root=str(root)))

    returned = _mod.require_dispatch_engine_on_path()

    assert returned == str(root)


def test_require_dispatch_engine_on_path_still_returns_root_on_match(monkeypatch, tmp_path, clean_sys_path):
    root = tmp_path / "dispatch-root-match"
    root.mkdir()
    monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: str(root))
    monkeypatch.setattr(
        _mod, "provenance_against", _fake_provenance(_mod.PROVENANCE_MATCH, imported_file=str(root), engine_root=str(root))
    )

    returned = _mod.require_dispatch_engine_on_path()

    assert returned == str(root)


# ---------------------------------------------------------------------------
# Hard constraint: ensure_engine_on_path is NOT hardened — its degrade-to-
# None contract for the engine-less scaffold case must survive unchanged.
# ---------------------------------------------------------------------------


def test_ensure_engine_on_path_does_not_raise_on_divergent(monkeypatch, tmp_path, clean_sys_path):
    root = tmp_path / "locator-root-divergent"
    root.mkdir()
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda script_file: str(root))
    monkeypatch.setattr(
        _mod,
        "provenance_against",
        _fake_provenance(_mod.PROVENANCE_DIVERGENT, engine_root=str(root)),
    )

    returned = _mod.ensure_engine_on_path("irrelevant.py")

    assert returned == str(root)


# ---------------------------------------------------------------------------
# The locator-axis wrappers are not hardened by this chunk either — its
# evidence (C8) only covers the dispatch-axis carriers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper_name,resolver_name,call_kwargs",
    [
        ("require_engine_on_path", "resolve_engine_root", {"script_file": "x.py"}),
        (
            "require_colocated_engine_on_path",
            "resolve_colocated_claude_klabauter_root",
            {"script_file": "x.py"},
        ),
    ],
)
def test_locator_axis_wrappers_do_not_raise_on_divergent(
    monkeypatch, tmp_path, clean_sys_path, wrapper_name, resolver_name, call_kwargs
):
    root = tmp_path / f"locator-divergent-{wrapper_name}"
    root.mkdir()
    monkeypatch.setattr(_mod, resolver_name, lambda script_file, _r=str(root): _r)
    monkeypatch.setattr(
        _mod,
        "provenance_against",
        _fake_provenance(_mod.PROVENANCE_DIVERGENT, engine_root=str(root)),
    )

    wrapper = getattr(_mod, wrapper_name)
    returned = wrapper(**call_kwargs)

    assert returned == str(root)
