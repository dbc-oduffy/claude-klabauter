"""C10's dual-read env accessor — the AC14/AC24 pins.

Chunk: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C10

WHY THESE ASSERTIONS AND NOT AN INSPECTION. AC14 says a reader gets the right
answer from EITHER name and writers export BOTH, "asserted by test over the
accessor, not by inspection" — because the whole point of the dual-read window
is behaviour across a version skew nobody can see by reading one tree. AC24
additionally requires the fallback be OBSERVABLE, since C14's exit condition is
"N days of zero fallback reads" cited as evidence rather than asserted.

NEGATIVE SPEC. These tests must not be read as blessing the old name as API.
`CLAUDE_KLABAUTER_ROOT` is only ever READ here and is never the spelling anything is told
to write; the window closes in C14. A test added here that asserts the old name
is *exported* by anything other than the dual-read write helper would invert
that and is the thing to refuse.
"""
from __future__ import annotations

import pathlib

import pytest

import coordinator_core.engine_root as engine_root

_NEW = "COORDINATOR_ENGINE_ROOT"
_OLD = "CLAUDE_KLABAUTER_ROOT"


@pytest.fixture(autouse=True)
def _clear_env_and_advisories(monkeypatch):
    """Both names unset and both once-per-process memos cleared per test.

    The memos are module-global and deliberately fire once per process, so
    without this reset the emission tests would pass or fail depending on
    execution order — the order-dependent green this plan names as its own
    headline risk.
    """
    monkeypatch.delenv(_NEW, raising=False)
    monkeypatch.delenv(_OLD, raising=False)
    engine_root._reset_engine_root_env_advisories()
    yield
    engine_root._reset_engine_root_env_advisories()


# --- AC14: a reader gets the right answer from EITHER name -----------------

def test_reads_the_new_name_when_only_it_is_set(monkeypatch):
    monkeypatch.setenv(_NEW, "/engines/new")
    assert engine_root.coordinator_engine_root_env("test") == "/engines/new"


def test_old_name_no_longer_answers_and_says_so(monkeypatch, capsys):
    """C14 CLOSED THE WINDOW. Until C14 this asserted the opposite — that a
    parent running from a pre-rename mirror exporting only `CLAUDE_KLABAUTER_ROOT` still
    resolved. It no longer does, and the inversion is the deliverable.

    The old name is still READ, for one purpose: to name itself as retired.
    Returning None silently would surface several rungs downstream as an
    unresolvable-root failure against whatever surface happened to need the
    root first; naming it at the point of the stale read is the difference
    between a named cause and a bisect."""
    monkeypatch.setenv(_OLD, "/engines/old")
    assert engine_root.coordinator_engine_root_env("test") is None
    err = capsys.readouterr().err
    assert _OLD in err and "NO LONGER HONOURED" in err
    assert _NEW in err, "the advisory must name the replacement, not just the retirement"


def test_new_name_wins_when_both_are_set(monkeypatch):
    """PRECEDENCE IS LOAD-BEARING, not a tie-break preference: a stale
    `CLAUDE_KLABAUTER_ROOT` inherited from an ancestor process must never override a
    fresh `COORDINATOR_ENGINE_ROOT` set by the immediate parent."""
    monkeypatch.setenv(_NEW, "/engines/fresh")
    monkeypatch.setenv(_OLD, "/engines/stale")
    assert engine_root.coordinator_engine_root_env("test") == "/engines/fresh"


def test_returns_none_when_neither_is_set():
    """Deliberately unchanged from pre-rename behaviour — this accessor does
    not invent a value the caller did not previously have."""
    assert engine_root.coordinator_engine_root_env("test") is None


def test_empty_string_is_treated_as_unset(monkeypatch):
    """An exported-but-empty variable is how a shell hands over "no value";
    treating it as a real answer would resolve the engine to the empty path.

    Post-C14 the empty NEW name no longer falls through to the old one — it
    falls through to None, like any other unset state."""
    monkeypatch.setenv(_NEW, "")
    monkeypatch.setenv(_OLD, "/engines/old")
    assert engine_root.coordinator_engine_root_env("test") is None


def test_empty_new_name_still_resolves_nothing_rather_than_empty_string(monkeypatch):
    """The original hazard this row guarded still has to hold on its own:
    an empty NEW name must never resolve the engine to \"\"."""
    monkeypatch.setenv(_NEW, "")
    assert engine_root.coordinator_engine_root_env("test") is None


# --- AC14: writers export the NEW NAME ONLY (C14 closed the dual-write) ----

def test_write_helper_exports_the_new_name_only():
    """C14 INVERTED THIS ROW TOO. Exporting the old name is what KEPT stale
    readers working, and therefore what held the precondition open — every one
    of the 26 fallback reads measured 2026-08-20 traced to the old name being
    exported or pinned, never to a consumer that could not have used the new
    one. Stop exporting it and the stale-reader population drains itself."""
    exports = engine_root.coordinator_engine_root_env_exports("/engines/x")
    assert exports == {_NEW: "/engines/x"}
    assert _OLD not in exports


def test_write_helper_exports_exactly_one_key():
    """Pins the ladder's width at its post-C14 value. A second name
    reappearing here — the old one re-added "just for safety", or a third
    spelling — is the failure mode C10's negative spec names explicitly, and
    is now also a reopening of a window this plan closed."""
    assert set(engine_root.coordinator_engine_root_env_exports("/x")) == {_NEW}


# --- AC24: the fallback is observable, once per reading site ---------------

def test_fallback_read_emits_once_per_site(monkeypatch, capsys):
    monkeypatch.setenv(_OLD, "/engines/old")

    engine_root.coordinator_engine_root_env("site-a")
    first = capsys.readouterr().err
    assert "site-a" in first and _OLD in first

    engine_root.coordinator_engine_root_env("site-a")
    assert capsys.readouterr().err == "", "second read from the same site must be silent"


def test_each_site_gets_its_own_emission(monkeypatch, capsys):
    """Per-site, not global: C14's exit cites zero fallback reads across
    every reading site, so one noisy site must not mask a second one."""
    monkeypatch.setenv(_OLD, "/engines/old")

    engine_root.coordinator_engine_root_env("site-a")
    capsys.readouterr()
    engine_root.coordinator_engine_root_env("site-b")
    assert "site-b" in capsys.readouterr().err


def test_no_emission_when_the_new_name_answered(monkeypatch, capsys):
    """The advisory means "the old name is still load-bearing somewhere". If
    it fired on the converged path it would be noise, and C14's
    zero-fallback-reads evidence would never reach zero."""
    monkeypatch.setenv(_NEW, "/engines/new")
    engine_root.coordinator_engine_root_env("site-a")
    assert capsys.readouterr().err == ""


def test_disagreement_between_the_two_names_is_reported_once(monkeypatch, capsys):
    """Both set and disagreeing is the genuinely dangerous state — it means
    two parties in one process tree hold different engine roots."""
    monkeypatch.setenv(_NEW, "/engines/fresh")
    monkeypatch.setenv(_OLD, "/engines/stale")

    engine_root.coordinator_engine_root_env("site-a")
    err = capsys.readouterr().err
    assert "/engines/fresh" in err and "/engines/stale" in err

    engine_root.coordinator_engine_root_env("site-b")
    assert capsys.readouterr().err == "", "conflict advisory is once per process, not per site"


def test_agreeing_values_do_not_report_a_conflict(monkeypatch, capsys):
    monkeypatch.setenv(_NEW, "/engines/same")
    monkeypatch.setenv(_OLD, "/engines/same")
    engine_root.coordinator_engine_root_env("site-a")
    assert capsys.readouterr().err == ""


def test_advisories_go_to_stderr_not_stdout(monkeypatch, capsys):
    """Several callers parse this process's stdout. An advisory printed there
    would corrupt a machine-readable answer rather than merely being noisy."""
    monkeypatch.setenv(_OLD, "/engines/old")
    engine_root.coordinator_engine_root_env("site-a")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


# --- AC13: the bootstrap carve-out sites are pinned to the accessor --------


def test_cc_invoke_precedence_is_pinned_to_the_accessor():
    """`cc_invoke.py` cannot IMPORT the accessor — resolving the engine is what
    it does — so it duplicates the precedence by hand. A duplicate that drifts
    is worse than no duplicate: the two would disagree only in the skew case
    nobody exercises until it breaks fleet-wide on the commit hot path.

    Asserted against source text rather than by calling it, because the module
    is loaded by path off the install tree and importing it here would resolve
    a DIFFERENT copy than the one this repo ships. Requested by doe-claude-em
    (2026-08-20) as the shape to pin after C14 removed the fallback.
    """
    src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "coordinator" / "bin" / "lib" / "cc_invoke.py"
    ).read_text(encoding="utf-8")

    # The rung-1 environment read must consult the NEW name and NOT fall back
    # to the old one — the exact edit C14 made in the accessor.
    assert 'existing = os.environ.get(_ENGINE_ROOT_NEW_VAR, "")\n' in src, (
        "cc_invoke's rung-1 read drifted from the accessor's post-C14 precedence"
    )
    assert 'os.environ.get(_ENGINE_ROOT_NEW_VAR, "") or os.environ.get(_ENGINE_ROOT_OLD_VAR' not in src, (
        "cc_invoke still falls back to the retired name; C14 removed that rung"
    )
    # And the child-env write must export the new name only.
    assert "_ENGINE_ROOT_OLD_VAR: claude_klabauter_root" not in src, (
        "cc_invoke still exports the retired name into child environments"
    )


def test_locator_axis_never_answers_from_the_retired_name(monkeypatch):
    """The LOCATOR accessor must not resolve a checkout from `CLAUDE_KLABAUTER_ROOT`.

    THE GAP THIS CLOSES, and why it survived C14's first two passes. C14 was
    reported complete, a tripwire then caught two further dual-read rungs in
    `cc_invoke`, and this THIRD one outlived both sweeps — inside
    `engine_root.py` itself, the one module legitimately allowed to name the
    retired variable. It read

        os.environ.get(_ENGINE_ROOT_NEW_VAR, "") or os.environ.get(_ENGINE_ROOT_OLD_VAR, "")

    and RETURNED the result. Two properties made it invisible: it consulted the
    new name FIRST, so every precedence-ORDER check passed straight over it,
    and it answered without routing through `_maybe_emit_engine_root_retired`,
    so the census sink built to make exactly this observable never saw it.
    Search by PRESENCE of the retired name, never by ordering — this test is
    that search, made permanent.

    The governing ruling is in this module's own C18 block: DR-326's 2026-08-20
    amendment, "the name is eliminated outright and no axis inherits it."
    """
    monkeypatch.delenv("COORDINATOR_ENGINE_SOURCE_ROOT", raising=False)
    monkeypatch.delenv(_NEW, raising=False)
    monkeypatch.setenv(_OLD, "/retired/should/never/answer")

    assert engine_root.coordinator_engine_source_root_env("test.locator") is None, (
        "the locator axis answered from the retired name; DR-326's amendment "
        "says no axis inherits it"
    )


def test_locator_axis_still_falls_back_to_the_dispatch_variable(monkeypatch):
    """The DISPATCH-variable fallback is deliberate and must survive.

    Guards the fix above from being over-applied: removing the retired-name leg
    must not also remove the transitional `COORDINATOR_ENGINE_ROOT` leg, which
    exists so an unrouted locator caller keeps working and emits the axis-misread
    advisory when it does. Deleting that would be a different regression wearing
    this fix's clothes.
    """
    monkeypatch.delenv("COORDINATOR_ENGINE_SOURCE_ROOT", raising=False)
    monkeypatch.delenv(_OLD, raising=False)
    monkeypatch.setenv(_NEW, "/dispatch/root")

    assert engine_root.coordinator_engine_source_root_env("test.locator") == "/dispatch/root"
