"""Guards for the mechanically-derived kill-ledger inventory.

The point of `kill_ledger_inventory` is that a roadmap built on it cannot quietly
omit a K-entry. These tests pin the three ways that guarantee could rot: a parser
that drops sections, a classifier that invents an UNCLASSIFIED bucket, and a
status-line substring test wide enough to misread authority prose as a status.
"""

from __future__ import annotations

import pytest

from coordinator_core.op_census import kill_ledger_inventory as kli


def _entry(status: str, *, key: str = "K-900", title: str = "a thing") -> str:
    return f"## {key} — {title}\n\n- **Status:** {status}\n\n**What is removed.** stuff.\n\n"


def test_parse_count_matches_heading_count() -> None:
    text = _entry("**LANDED**") + _entry("CANDIDATE — NOT YET CONVICTED", key="K-901")
    entries = kli.parse_ledger(text)
    assert [e.key for e in entries] == ["K-900", "K-901"]


def test_parser_drop_is_an_exception_not_a_silent_omission(tmp_path) -> None:
    ledger = tmp_path / "kill-ledger.md"
    ledger.write_text(_entry("**LANDED**"), encoding="utf-8")
    original = kli.parse_ledger
    try:
        kli.parse_ledger = lambda _text: []  # type: ignore[assignment]
        with pytest.raises(AssertionError, match="dropped a section"):
            kli.build(ledger)
    finally:
        kli.parse_ledger = original  # type: ignore[assignment]


def test_no_entry_is_left_unclassified() -> None:
    entries = kli.parse_ledger(_entry("something the rules have never seen"))
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "CONTESTED"
    assert entries[0].notes  # a CONTESTED row always states why


def test_authority_prose_does_not_override_a_landed_status() -> None:
    """`closed out by C1g` is authority prose, not a CLOSED status — the K-012
    misclassification this window guards against."""
    entries = kli.parse_ledger(
        _entry("**LANDED** - **Date:** 2026-08-21 - **Authority:** plan F-1; chunks C1a-C1j, closed out by C1g")
    )
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "LANDED"


def test_landed_status_on_a_live_op_is_contested() -> None:
    entries = kli.parse_ledger(_entry("removed", title="`hooks.example_op` (cut elsewhere)"))
    kli.classify(entries, live_ops=frozenset({"hooks.example_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CONTESTED"


def test_candidate_status_on_a_live_op_stays_a_candidate() -> None:
    entries = kli.parse_ledger(
        _entry("CANDIDATE — MEASURED ON WALL CLOCK, NOT YET CONVICTED", title="`fleet.example_op`")
    )
    kli.classify(entries, live_ops=frozenset({"fleet.example_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CANDIDATE"


def test_a_file_path_is_not_read_as_an_op_name() -> None:
    entries = kli.parse_ledger(_entry("**LANDED**", title="`coverage.py`'s orphaned surface"))
    assert entries[0].op_name is None


def test_real_ledger_classifies_every_entry() -> None:
    entries, heading_count = kli.build()
    assert len(entries) == heading_count
    assert not [e for e in entries if e.population == "UNCLASSIFIED"]


def test_killed_is_a_landing_word() -> None:
    """`KILLED, PM ruling` is how the ledger records a PM-ordered cut. Absent a
    marker for it, the entry fell through to CONTESTED and read as a defect."""
    entries = kli.parse_ledger(_entry("KILLED, PM ruling 2026-08-26.", title="`fleet.gone_op`"))
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "LANDED"


def test_acquitted_is_not_a_cut() -> None:
    entries = kli.parse_ledger(
        _entry("ACQUITTED 2026-08-26 — returns-when met on its own stated terms.", title="`session.kept_op`")
    )
    kli.classify(entries, live_ops=frozenset({"session.kept_op"}), suspended_ops=frozenset())
    assert entries[0].population == "NON_CUT"


def test_convicted_but_unlanded_is_its_own_population_not_a_disagreement() -> None:
    """A convicted op is *expected* to still be registered until the cut lands;
    reading that as CONTESTED buries the entries where liveness is a real defect."""
    entries = kli.parse_ledger(
        _entry("CONVICTED ON PROCESS TIME — rebuild from first principles.", title="`handoff.doomed_op`")
    )
    kli.classify(entries, live_ops=frozenset({"handoff.doomed_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CONVICTED"


def test_a_convicted_op_already_gone_is_contested_not_silently_landed() -> None:
    """No population is ever inferred from liveness — not even the plausible
    one. An op gone while its entry still reads CONVICTED is a ledger that has
    not caught up, which is a disagreement to surface, not a landing to assume."""
    entries = kli.parse_ledger(
        _entry("CONVICTED ON PROCESS TIME — rebuild from first principles.", title="`handoff.doomed_op`")
    )
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "CONTESTED"
    assert "already absent from the live registry" in " ".join(entries[0].notes)


def test_a_marker_late_in_a_status_field_does_not_earn_a_population() -> None:
    """A status runs to 260 chars. A phrase that far in is prose about some
    other entry, not this entry's own disposition."""
    late = "removed" + (" filler" * 30) + " then rebuilt elsewhere, not by this repo"
    entries = kli.parse_ledger(_entry(late, title="`fleet.late_marker_op`"))
    kli.classify(entries, live_ops=frozenset({"fleet.late_marker_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CONTESTED"


def test_convicted_does_not_steal_the_candidate_population() -> None:
    """Every CANDIDATE status says NOT YET CONVICTED further along the line."""
    entries = kli.parse_ledger(
        _entry("CANDIDATE — MEASURED ON WALL CLOCK, NOT YET CONVICTED", title="`fleet.example_op`")
    )
    kli.classify(entries, live_ops=frozenset({"fleet.example_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CANDIDATE"


def test_cross_plane_cut_must_say_so_in_the_status() -> None:
    """The escape from CONTESTED is earned by the entry stating the cross-plane
    fact — never by the classifier inferring one from liveness alone."""
    stated = kli.parse_ledger(
        _entry("removed — by DoE-claude, not by this repo, reconstructed here after the fact.",
               title="`hooks.example_op`")
    )
    kli.classify(stated, live_ops=frozenset({"hooks.example_op"}), suspended_ops=frozenset())
    assert stated[0].population == "CUT_ELSEWHERE"

    unstated = kli.parse_ledger(_entry("removed", title="`hooks.example_op`"))
    kli.classify(unstated, live_ops=frozenset({"hooks.example_op"}), suspended_ops=frozenset())
    assert unstated[0].population == "CONTESTED"


def test_rebuilt_must_say_so_and_must_actually_be_live() -> None:
    text = _entry("**LANDED** (`abc1234`), then rebuilt and live again.", title="`memo.example_send`")
    live = kli.parse_ledger(text)
    kli.classify(live, live_ops=frozenset({"memo.example_send"}), suspended_ops=frozenset())
    assert live[0].population == "REBUILT"

    # A rebuild the registry cannot corroborate is a defect report, not a pass.
    absent = kli.parse_ledger(text)
    kli.classify(absent, live_ops=frozenset(), suspended_ops=frozenset())
    assert absent[0].population == "CONTESTED"
    assert "absent from the live registry" in " ".join(absent[0].notes)


def test_gravestone_is_a_landing_word() -> None:
    """`GRAVESTONE.` is how the ledger records a cut with the requirement
    retired outright (K-059). Absent a marker for it, the entry fell through
    to CONTESTED — the classifier's own defect, not the ledger's."""
    entries = kli.parse_ledger(_entry("GRAVESTONE.", title="`session.gone_op`"))
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "LANDED"


def _range_entry(status: str, *, start: int = 200, end: int = 202) -> str:
    rows = "\n".join(f"| K-{n} | `fleet.op_{n}` | " for n in range(start, end + 1))
    return (
        f"## K-{start}..K-{end} — a range sweep\n\n"
        f"- **Status:** {status}\n\n"
        "| # | op | disposition |\n|---|---|---|\n"
        f"{rows}\n\n"
    )


def test_range_heading_yields_one_entry_per_id_not_one() -> None:
    entries = kli.parse_ledger(_range_entry("**CUT.**"))
    assert [e.key for e in entries] == ["K-200", "K-201", "K-202"]


def test_single_id_heading_still_yields_exactly_one() -> None:
    entries = kli.parse_ledger(_entry("**LANDED**"))
    assert len(entries) == 1


def test_range_entries_attribute_op_names_from_the_table() -> None:
    entries = kli.parse_ledger(_range_entry("**CUT.**"))
    assert [e.op_name for e in entries] == ["fleet.op_200", "fleet.op_201", "fleet.op_202"]


def test_range_heading_count_reconciles_with_entries_parsed() -> None:
    ledger = _range_entry("**CUT.**") + _entry("**LANDED**", key="K-900")
    heading_count = kli._heading_count(ledger)
    entries = kli.parse_ledger(ledger)
    assert heading_count == len(entries) == 4


def test_cut_is_a_landing_word() -> None:
    """`CUT.` is how the 200ms-sweep range heading (K-103..K-115) records a
    landing. Absent this marker the entries fell through to CONTESTED — the
    same classifier gap `gravestone` was added to close."""
    entries = kli.parse_ledger(_entry("CUT.", title="`fleet.gone_op`"))
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "LANDED"


def test_the_real_ledger_has_no_contested_rows() -> None:
    """AC-7 of the-meter-02: `--fail-on-contested` exits 0 against the real
    ledger. A CONTESTED row here is a real ledger/registry disagreement to
    reconcile in `state/kill-ledger.md`, never by widening a marker tuple."""
    entries, _ = kli.build(kli.KILL_LEDGER)
    contested = [f"{e.key}: {'; '.join(e.notes)}" for e in entries if e.population == "CONTESTED"]
    assert not contested, contested


def test_withdrawn_is_its_own_population_and_the_op_must_survive() -> None:
    """A nomination retracted with the op left standing is neither a landing
    nor a rebuild: nothing was cut, so there is no dead predecessor and no
    replacement. K-040 (`records.history`) is the worked case — nominated on a
    requirement leg a repo-local consumer census could not establish, because
    the caller was in another repo.

    Before this population existed the entry matched no rule and fell to
    CONTESTED, which is this module's own stated defect: a status vocabulary
    the rules cannot place is the classifier's fault, not the ledger's.
    """
    text = _entry(
        "WITHDRAWN 2026-08-28 — not a chop. The nomination was wrong and the "
        "surface stays.",
        title="`records.example_history`",
    )
    live = kli.parse_ledger(text)
    kli.classify(live, live_ops=frozenset({"records.example_history"}), suspended_ops=frozenset())
    assert live[0].population == "WITHDRAWN"

    # The invariant runs the opposite way to REBUILT's: a withdrawn nomination
    # asserts the op SURVIVED, so an absent op means something cut it anyway.
    absent = kli.parse_ledger(text)
    kli.classify(absent, live_ops=frozenset(), suspended_ops=frozenset())
    assert absent[0].population == "CONTESTED"
    assert "something cut it anyway" in " ".join(absent[0].notes)


def test_withdrawn_is_not_reachable_by_prose_late_in_a_status() -> None:
    """The marker is windowed to the status opening. A landed entry whose prose
    mentions a withdrawn objection must stay LANDED — otherwise the population
    is reachable by any entry that discusses one."""
    entries = kli.parse_ledger(
        _entry(
            "**LANDED** (`abc1234`) — the objection raised at review was later "
            "withdrawn, and the cut proceeded.",
            title="`session.gone_op`",
        )
    )
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "LANDED"


def test_report_renders_every_population_the_classifier_produced() -> None:
    """The section order is a preference list, not an allowlist. A population
    missing from it used to be dropped from the report entirely, so an entry
    that vanished read exactly like an entry that did not exist."""
    text = _entry(
        "WITHDRAWN — the nomination was wrong.", title="`records.example_history`"
    ) + _entry("**LANDED**", key="K-901", title="`session.gone_op`")
    entries = kli.parse_ledger(text)
    kli.classify(
        entries,
        live_ops=frozenset({"records.example_history"}),
        suspended_ops=frozenset(),
    )
    report = kli.render(entries, heading_count=len(entries))
    assert "## WITHDRAWN (1)" in report
    assert "## LANDED (1)" in report
