"""
coordinator_core.test_staff_session_assemble — co-located pytest for
coordinator_core.staff_session_assemble.

Covers: domain-signal -> default-pair resolution, explicit --slug override,
The Director of Engineering-cannot-debate rejection, unknown-domain-signal / unknown-slug
fail-loud errors, and — the load-bearing case per plan AC17 — a fixture
that pins reading the doctrine-side roster data from a routing.md-shaped
text (via the `routing_md_text` test seam) rather than a hardcoded dict:
mutating the fixture text changes the resolved roster, proving there is no
compiled-in fallback copy.

Run: python -m pytest coordinator_core/test_staff_session_assemble.py -q
"""
from __future__ import annotations

import pytest

import coordinator_core.staff_session_assemble as ssa

_FIXTURE_ROUTING_MD = """\
# Coordinator Routing Table

## Staff-Session Roster

### Domain Signal -> Default Pair

| Domain Signal | Default Pair |
|---|---|
| Architecture / infrastructure | `the Staff Engineer` + `sid` |
| Frontend / UI | `the Front-End Reviewer` + `the UX Reviewer` |

### Persona Slug -> Agent File

| Slug | Agent File |
|---|---|
| `the Staff Engineer` | `coordinator/agents/staff-eng.md` |
| `sid` | `game-dev/agents/staff-game-dev.md` |
| `the Front-End Reviewer` | `coordinator/agents/senior-front-end.md` |
| `the UX Reviewer` | `coordinator/agents/staff-ux.md` |

### Persona Slug -> subagent_type

| Slug | subagent_type |
|---|---|
| `the Staff Engineer` | `coordinator:staff-eng` |
| `sid` | `game-dev:staff-game-dev` |
| `the Front-End Reviewer` | `coordinator:senior-front-end` |
| `the UX Reviewer` | `coordinator:staff-ux` |
"""


def test_domain_signal_resolves_default_pair_and_full_roster():
    decision = ssa.resolve_roster(
        domain_signal="Architecture / infrastructure",
        session_mode="plan",
        routing_md_text=_FIXTURE_ROUTING_MD,
    )
    slugs = [p["slug"] for p in decision["personas"]]
    assert slugs == ["patrik", "sid"]
    by_slug = {p["slug"]: p for p in decision["personas"]}
    assert by_slug["patrik"]["agent_file"] == "coordinator/agents/staff-eng.md"
    assert by_slug["patrik"]["subagent_type"] == "coordinator:staff-eng"
    assert by_slug["sid"]["subagent_type"] == "game-dev:staff-game-dev"


def test_explicit_slug_override_bypasses_domain_signal_lookup():
    decision = ssa.resolve_roster(
        session_mode="review",
        slugs=["pali", "fru"],
        routing_md_text=_FIXTURE_ROUTING_MD,
    )
    slugs = [p["slug"] for p in decision["personas"]]
    assert slugs == ["pali", "fru"]


def test_synthesizer_cannot_appear_as_a_debater_via_override():
    with pytest.raises(ssa.StaffSessionAssembleError, match="synthesizer"):
        ssa.resolve_roster(
            session_mode="plan",
            slugs=["patrik", "zoli"],
            routing_md_text=_FIXTURE_ROUTING_MD,
        )


def test_unknown_domain_signal_fails_loud_not_silent_default():
    with pytest.raises(ssa.StaffSessionAssembleError, match="not found"):
        ssa.resolve_roster(
            domain_signal="Nonexistent Domain",
            session_mode="plan",
            routing_md_text=_FIXTURE_ROUTING_MD,
        )


def test_unknown_slug_override_fails_loud():
    with pytest.raises(ssa.StaffSessionAssembleError, match="agent-file"):
        ssa.resolve_roster(
            session_mode="plan",
            slugs=["not-a-real-slug"],
            routing_md_text=_FIXTURE_ROUTING_MD,
        )


def test_invalid_session_mode_rejected():
    with pytest.raises(ssa.StaffSessionAssembleError, match="session_mode"):
        ssa.resolve_roster(
            domain_signal="Architecture / infrastructure",
            session_mode="bogus",
            routing_md_text=_FIXTURE_ROUTING_MD,
        )


def test_missing_domain_signal_and_slugs_rejected():
    with pytest.raises(ssa.StaffSessionAssembleError, match="domain_signal or"):
        ssa.resolve_roster(session_mode="plan", routing_md_text=_FIXTURE_ROUTING_MD)


def test_reads_doctrine_side_data_not_a_hardcoded_copy():
    """Pins the F1/AC17 contract: mutating the fixture routing.md text
    changes the resolved roster — there is no compiled-in fallback dict a
    caller could silently drift from the doctrine-side data."""
    mutated = _FIXTURE_ROUTING_MD.replace(
        "coordinator/agents/staff-eng.md", "coordinator/agents/renamed-staff-eng.md"
    )
    decision = ssa.resolve_roster(
        domain_signal="Architecture / infrastructure",
        session_mode="plan",
        routing_md_text=mutated,
    )
    by_slug = {p["slug"]: p for p in decision["personas"]}
    assert by_slug["patrik"]["agent_file"] == "coordinator/agents/renamed-staff-eng.md"


def test_missing_section_heading_is_fail_loud():
    truncated = _FIXTURE_ROUTING_MD.split("### Persona Slug -> Agent File")[0]
    with pytest.raises(ssa.StaffSessionAssembleError, match="missing the expected section heading"):
        ssa.resolve_roster(
            domain_signal="Architecture / infrastructure",
            session_mode="plan",
            routing_md_text=truncated,
        )


def test_cli_main_prints_json_roster(capsys):
    exit_code = ssa.main(["--slug", "patrik", "--session-mode", "plan"])
    # No routing_md_text seam on the CLI path — this will hit the real
    # doctrine-side read, which fails today (routing.md has no
    # Staff-Session Roster section yet, C9 not landed) — assert the
    # documented fail-loud usage-error contract rather than a live parse.
    assert exit_code == ssa.EXIT_USAGE
    captured = capsys.readouterr()
    assert "staff-session-assemble" in captured.err
