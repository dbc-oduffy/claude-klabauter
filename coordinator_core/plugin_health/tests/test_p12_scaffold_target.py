"""
coordinator_core.plugin_health.tests.test_p12_scaffold_target — P-12 retirement.

Spec: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (IBMDT-C2, item 27)

Doctor probe P-12 (`coordinator_core/plugin_health/sentinel.py::probe_p12`) used to
report amber whenever the Claude home's project canonical-structure entries
(`coordinator/canonical-structure.yaml`) were incomplete there, and pointed the
operator at restoring them. DR-072 (accepted) classifies that scaffold as
NO-PLACEMENT-CLAIM residue — sweep, never relocate — because the Claude home is not
a project root and carries no canonical structure of its own. Flagging its absence
re-triggered the exact evict -> flag -> restore -> refused-commit loop DR-072 exists
to close. P-12 is retired outright: it must never report the Claude home as a
scaffold target, regardless of how incomplete a dry-run scaffold there would be.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.plugin_health.sentinel import probe_p12


class _FakeScaffoldResult:
    """Would-create a non-trivial count, as the pre-retirement live probe saw
    on an evicted Claude home."""

    def would_create_count(self) -> int:
        return 7


def test_p12_returns_empty_even_when_scaffold_would_create_entries(tmp_path, monkeypatch):
    claude_home = tmp_path / ".claude"
    sibling_bin_dir = tmp_path / "settings-home" / "bin"
    sibling_bin_dir.mkdir(parents=True)

    def _fake_scaffold_canonical_structure(*_args, **_kwargs):
        return _FakeScaffoldResult()

    monkeypatch.setattr(
        "coordinator_core.install.scaffold_structure.scaffold_canonical_structure",
        _fake_scaffold_canonical_structure,
    )

    notes = probe_p12(sibling_bin_dir, claude_home)

    assert notes == [], (
        "P-12 is retired: it must never report the Claude home as an "
        "incomplete scaffold target, per DR-072's sweep-never-relocate "
        "disposition (item 27)"
    )


def test_p12_returns_empty_with_no_sibling_bin_dir(tmp_path):
    assert probe_p12(None, tmp_path / ".claude") == []


def test_p12_never_names_claude_home_or_canonical_structure_in_a_note():
    """Even a probe wired to something surprising must not resurrect the
    old amber wording naming the Claude home as a restore target."""
    notes = probe_p12(Path("/nonexistent/bin"), Path("/nonexistent/.claude"))
    assert notes == []
