# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""bin/tests/test_publish_allowlist_sibling_shellouts.py

Purpose: regression guard for the class of defect behind the 2026-08-16
`check-pcli-drift-gate.py` incident — `workweek-complete-drift-guards.py`
`_sibling("check-pcli-drift-gate.py")`-shells out to a callee CLI that was
absent from the `claude-klabauter-coordinator-bin` row's allowlist in
`setup/publish-targets.portable`, so the callee never reached the
Claude-klabauter mirror. A caller that DOES ship a `_sibling(...)` reference
to a callee that does NOT ship is a permanent exit-2 cannot-run on whatever
ceremony gate the caller backs, discovered only downstream in a sibling
repo's ceremony (example-retrieval-repo-em, example-cockpit-repo-em, example-store-repo-em all hit
it independently on 2026-08-16) rather than here where it originates.

This test parses the row itself (by NAME, never by line number — the file
is edited often and line numbers drift) and, for every `_sibling("<name>")`
call site in a bin file that IS itself allowlisted by that row, asserts the
referenced `<name>` is ALSO allowlisted. A `_sibling(...)` reference living
in a file that is NOT itself published is not a violation — an unpublished
caller cannot strand anything in the mirror it never reaches.

Coverage:
  test_row_exists_and_is_well_formed
  test_known_sibling_call_sites_are_all_allowlisted
  test_every_published_callers_sibling_refs_are_allowlisted
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_REPO_ROOT = _BIN_DIR.parent.parent
_TARGETS_FILE = _REPO_ROOT / "setup" / "publish-targets.portable"

_ROW_NAME = "claude-klabauter-coordinator-bin"

_SIBLING_CALL_RE = re.compile(r'_sibling\(\s*["\']([^"\']+)["\']\s*\)')

# Known ground truth as of this guard's authoring (2026-08-18) — asserted
# separately from the general sweep below so a change to either the call
# sites or the row surfaces as a specific, legible failure rather than only
# a generic "something in the sweep failed".
_EXPECTED_CALL_SITES = {
    "workday-start-health-probes.py": {
        "coordinator-ceremony-hook.py",
        "stitch-observer-sidecar.py",
    },
    "workweek-complete-drift-guards.py": {
        "audit-enabled-plugins.py",
        "check-description-length.py",
        "check-multi-event-hook-hardcoded-event.py",
        "check-pcli-drift-gate.py",
        "schema-drift-gate.py",
        "verify-no-console-flash.py",
    },
}


def _find_row(name: str) -> str:
    """Locate a `publish-targets.portable` row BY NAME (field 0), never by
    line number — the file is edited often enough that line numbers drift
    between when a test is written and when it next runs."""
    text = _TARGETS_FILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(name + "|"):
            return line
    raise AssertionError(
        f"no row named {name!r} found in {_TARGETS_FILE}"
    )


def _row_allowlist(row: str) -> "set[str]":
    """Field 6 (0-indexed) of the pipe-delimited row is the comma-separated
    allowlist. Entries beginning `!` are negative (exclusion) entries —
    excluded from the positive set this guard checks membership against."""
    fields = row.split("|")
    assert len(fields) > 6, (
        f"row {fields[0]!r} has {len(fields)} fields, expected >6 "
        f"(allowlist is field index 6): {row!r}"
    )
    allowlist_csv = fields[6]
    entries = {e for e in allowlist_csv.split(",") if e}
    return {e for e in entries if not e.startswith("!")}


def _sibling_call_sites() -> "dict[str, set[str]]":
    """Every `_sibling("<name>")` call site under `coordinator/bin/*.py`
    (non-recursive — `tests/` siblings are test fixtures, not shipped
    callers), keyed by the calling file's basename."""
    result: "dict[str, set[str]]" = {}
    for path in sorted(_BIN_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        names = set(_SIBLING_CALL_RE.findall(text))
        if names:
            result[path.name] = names
    return result


def test_row_exists_and_is_well_formed() -> None:
    row = _find_row(_ROW_NAME)
    fields = row.split("|")
    assert fields[0] == _ROW_NAME
    assert len(fields) > 6
    assert _row_allowlist(row), "allowlist field parsed empty — row shape may have drifted"


def test_known_sibling_call_sites_are_all_allowlisted() -> None:
    """Ground-truth assertion: the exact call-site inventory this guard was
    authored against. If this fails because the inventory changed, update
    `_EXPECTED_CALL_SITES` deliberately — the general sweep below is the
    guard that must keep passing regardless."""
    found = _sibling_call_sites()
    assert found == _EXPECTED_CALL_SITES, (
        f"_sibling(...) call-site inventory drifted from ground truth.\n"
        f"found: {found}\nexpected: {_EXPECTED_CALL_SITES}"
    )


def test_every_published_callers_sibling_refs_are_allowlisted() -> None:
    """The actual guard: for every bin file that IS allowlisted by the
    claude-klabauter-coordinator-bin row and DOES shell out via
    `_sibling(...)`, every referenced callee name must ALSO be allowlisted
    by that same row — otherwise the callee is stranded out of the mirror
    while the caller that depends on it ships."""
    row = _find_row(_ROW_NAME)
    allowlist = _row_allowlist(row)
    call_sites = _sibling_call_sites()

    violations = []
    for caller, callees in call_sites.items():
        if caller not in allowlist:
            # An unpublished caller cannot strand anything in the mirror.
            continue
        for callee in sorted(callees):
            if callee not in allowlist:
                violations.append(f"{caller} -> {callee}")

    assert not violations, (
        f"caller(s) shell out via _sibling(...) to callee(s) missing from "
        f"the {_ROW_NAME!r} row's allowlist in {_TARGETS_FILE}: "
        f"{violations}"
    )
