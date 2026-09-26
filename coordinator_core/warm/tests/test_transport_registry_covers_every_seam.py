
from __future__ import annotations

from coordinator_core.warm import transport_registry as tr


def test_transports_json_loads():
    rows = tr.load_transports()
    assert rows, "transports.json must enumerate at least one transport"


def test_every_row_has_required_fields_and_valid_schema():
    rows = tr.load_transports()
    errors = tr.validate_transports(rows)
    assert errors == [], "transports.json schema violations:\n" + "\n".join(errors)


def test_every_degrading_row_is_observable_or_excused():
    rows = tr.load_transports()
    for row in rows:
        if row.get("degrades") is not True:
            continue
        observable = row.get("degrade_observable")
        assert observable in (True, False), (
            f"{row.get('name')}: degrades=true rows must set degrade_observable "
            "to true or false, never omitted"
        )
        if observable is True:
            assert row.get("degrade_signal"), (
                f"{row.get('name')}: degrade_observable=true requires a non-empty "
                "degrade_signal naming the file :: function that emits it"
            )
        else:
            assert row.get("cannot_observe_reason"), (
                f"{row.get('name')}: degrade_observable=false with an empty "
                "cannot_observe_reason -- this is exactly the escape clause "
                "AC15 exists to close (state/lessons/2026-08-26-naming-an-"
                "artifact-is-not-evaluating-it.yaml)"
            )


def test_row_names_are_unique():
    rows = tr.load_transports()
    names = [row.get("name") for row in rows]
    assert len(names) == len(set(names)), f"duplicate transport names: {names}"


def test_seam_markers_cover_the_four_named_seams():
    """Pins the brief's own enumeration: pipe_name derivation, invoke.from_argv
    request construction, the warm HTTP endpoint, and warm.client dispatch
    entry. A future edit that renames or drops one of transport_registry's
    SEAM_MARKERS keys without updating this test is the signal this pin
    exists to catch."""
    assert tr.KNOWN_SEAM_KEYS == frozenset(
        {
            "pipe_name_derivation",
            "invoke_from_argv_request",
            "http_endpoint",
            "client_dispatch_entry",
        }
    )


def test_no_seam_construction_site_is_unclaimed():
    rows = tr.load_transports()
    offenders = tr.find_unclaimed_construction_sites(rows)
    assert offenders == [], (
        "unclaimed warm-engine transport construction site(s) -- add a row to "
        "coordinator_core/warm/transports.json claiming each:\n"
        + "\n".join(offenders)
    )


def test_every_seam_files_entry_still_exists_on_disk():
    rows = tr.load_transports()
    missing = []
    for row in rows:
        for rel in row.get("seam_files", []):
            if not (tr.REPO_ROOT / rel).is_file():
                missing.append(f"{row.get('name')}: {rel}")
    assert missing == [], "transports.json seam_files pointing at missing paths:\n" + "\n".join(
        missing
    )


def test_ps1_policy_status_file_is_not_a_transport_row():
    rows = tr.load_transports()
    for row in rows:
        assert "ps1-policy-gate-status" not in row.get("entry_site", "")
        for f in row.get("seam_files", []):
            assert "ps1-policy-gate-status" not in f
