"""coordinator_core.warm.tests.test_transport_registry_covers_every_seam

The drift guard for coordinator_core/warm/transports.json, modelled on
coordinator_core/test_bin_launcher_parity.py::
test_raw_cmdline_entrypoints_matches_substrate_targets -- a committed
allowlist plus a test that goes RED the moment reality drifts from it.

Two independent things are pinned here, and a failure in either is the
manifest going stale, not a false alarm:

  1. Schema (AC15): every row that can degrade says, in the manifest
     itself, whether that degrade is observable and (if not) why not --
     see transport_registry.validate_transports.
  2. Coverage: the four named warm-engine seams (pipe_name derivation,
     invoke.from_argv request construction, the warm HTTP endpoint,
     warm.client dispatch entry) are each claimed, file by file, by some
     row in the manifest -- see transport_registry.
     find_unclaimed_construction_sites. A new transport construction site
     lands here RED until its author writes a row for it.
"""

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
    """AC15's own assertion, isolated from the general schema check above so
    a failure here reads unambiguously as the loud-degrade guarantee being
    broken, not a generic malformed-row failure."""
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
    """THE drift guard: scans coordinator_core/warm/ for each of the four
    seam markers and fails, naming every offender, if a file constructs one
    without any manifest row claiming it. This is the assertion the whole
    module exists to make possible -- a new transport is red until its
    author writes a row."""
    rows = tr.load_transports()
    offenders = tr.find_unclaimed_construction_sites(rows)
    assert offenders == [], (
        "unclaimed warm-engine transport construction site(s) -- add a row to "
        "coordinator_core/warm/transports.json claiming each:\n"
        + "\n".join(offenders)
    )


def test_every_seam_files_entry_still_exists_on_disk():
    """A row's seam_files pointing at a deleted/renamed file would make
    find_unclaimed_construction_sites silently under-claim (the file can no
    longer match a marker, so its absence looks like compliance rather than
    manifest rot). Caught here directly rather than relying on the scanner
    to notice a gap it cannot see."""
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
    """The C9 brief's own determination (transport_registry module
    docstring, 'ON ps1-policy-gate-status.json'): that file is an
    install-time status record, never a warm-engine transport, so no row
    should claim it. Pinned so a future edit does not silently absorb it
    into this manifest without revisiting that determination."""
    rows = tr.load_transports()
    for row in rows:
        assert "ps1-policy-gate-status" not in row.get("entry_site", "")
        for f in row.get("seam_files", []):
            assert "ps1-policy-gate-status" not in f
