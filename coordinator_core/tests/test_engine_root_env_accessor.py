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


def test_reads_the_old_name_when_only_it_is_set(monkeypatch):
    """The skew case the window exists for: a parent running from a
    pre-rename mirror exports only `CLAUDE_KLABAUTER_ROOT`, and this tree still has to
    resolve."""
    monkeypatch.setenv(_OLD, "/engines/old")
    assert engine_root.coordinator_engine_root_env("test") == "/engines/old"


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
    treating it as a real answer would resolve the engine to the empty path."""
    monkeypatch.setenv(_NEW, "")
    monkeypatch.setenv(_OLD, "/engines/old")
    assert engine_root.coordinator_engine_root_env("test") == "/engines/old"


# --- AC14: writers export BOTH ---------------------------------------------

def test_write_helper_exports_both_names_with_the_same_value():
    exports = engine_root.coordinator_engine_root_env_exports("/engines/x")
    assert exports == {_NEW: "/engines/x", _OLD: "/engines/x"}


def test_write_helper_exports_exactly_two_keys():
    """Pins the window's width. A third name added to the ladder is the
    failure mode C10's negative spec names explicitly."""
    assert set(engine_root.coordinator_engine_root_env_exports("/x")) == {_NEW, _OLD}


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
