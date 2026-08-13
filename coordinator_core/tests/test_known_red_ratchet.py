"""The ratchet gate for `state/bash-guards/known-red.json` (C8, AC8).

Spec backlink: pln-make-the-bash-guards-suite-a-g-2f81d6 § C8

WHY THIS EXISTS
    Three prior passes at `coordinator_core/bash_guards/tests/` each cleared
    real red cells and none built a mechanism stopping the set regrowing --
    it came back bigger every time. AC8's unit is the unmarked-red nodeid
    SET, never a count: a count is gameable three ways (mark-to-satisfy,
    fix-one-break-one swap, host drift). This module carries two
    SEPARATELY-FAILING gates plus a review-window check, none sharing a
    single assertion, so each failure mode names itself rather than
    reporting an ambiguous delta:

      1. `test_unmarked_red_is_a_subset_of_the_registry` -- catches
         regrowth AND the fix-one-break-one swap, by nodeid.
      2. `test_every_marker_in_the_tree_has_a_registry_entry` -- catches
         mark-to-satisfy: adding a marker without a registry entry (or with
         an empty owner/reason) fails here even though gate 1 would pass.
      3. `test_no_registry_entry_is_past_its_review_by` -- catches decay
         into permanence: a stale entry fails the gate on the calendar,
         not on good intentions.

    Directional-proof tests at the bottom exercise the pure check functions
    against synthetic registries/observed-sets (no live pytest subprocess),
    proving both a planted new unmarked-red nodeid fails gate 1 and a
    registered one passes it.

NEGATIVE SPEC
    - Carries no `pending_fix`/`designed_red`/`cadence` marker anywhere in
      this module -- AC8(f). The ratchet exempting itself is the first
      thing a frustrated developer would reach for, and it would silently
      defeat gate 1 and 2 both.
    - Does not re-derive the red set independently of
      `coordinator/bin/red-set-report.py` -- imported via the same
      `importlib.util.spec_from_file_location` pattern
      `check_test_suite_invocation.py` already uses for `coordinator/bin/`
      scripts, never re-implemented.
    - Does not hardcode a red-cell count anywhere -- every assertion below
      reads the registry and the live derivation, never a literal number.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "state" / "bash-guards" / "known-red.json"
REGENERATE_SCRIPT = REPO_ROOT / "coordinator" / "bin" / "regenerate-known-red-registry.py"
RED_SET_REPORT_SCRIPT = REPO_ROOT / "coordinator" / "bin" / "red-set-report.py"
# The MEASURED tree -- deliberately NOT this file's own location. This file
# now lives at coordinator_core/tests/ specifically so a self-referential
# gate never has to reason about its own inclusion (see module docstring's
# WHY THIS EXISTS and the P0 fix note below).
RED_SET_REPORT_TARGET = "coordinator_core/bash_guards/tests/"

# Capped, never bare `-n auto` (has killed a session on this box before; see
# coordinator/bin/red-set-report.py's capped_worker_count, which clamps this
# request to min(physical_cores // 2, usable_ram_mb // 150) regardless of the
# value here). 8 is comfortably above that cap on any box this runs on, so
# in practice the request is a "use the safe cap" signal, not a literal
# worker count.
_LIVE_DERIVATION_WORKERS = 8


def _load_regenerate_module():
    spec = importlib.util.spec_from_file_location(
        "_known_red_registry_regenerate", REGENERATE_SCRIPT
    )
    assert spec is not None and spec.loader is not None, (
        "could not load %s" % REGENERATE_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_red_set_report_module():
    spec = importlib.util.spec_from_file_location(
        "_known_red_registry_red_set_report", RED_SET_REPORT_SCRIPT
    )
    assert spec is not None and spec.loader is not None, (
        "could not load %s" % RED_SET_REPORT_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pure check functions -- the logic under test, kept free of subprocess/disk
# I/O so the directional-proof tests below can exercise them against
# synthetic fixtures without spawning pytest.
# ---------------------------------------------------------------------------


def offenders_not_in_registry(observed_unmarked_red: set, registry_entries: dict) -> list:
    """AC8(b): the observed unmarked-red nodeid set must be a SUBSET of the
    registry. Returns the offending nodeids (empty means the gate passes)."""
    return sorted(observed_unmarked_red - set(registry_entries.keys()))


def markers_missing_registry_coverage(tree_marked_nodeids: set, registry_entries: dict) -> list:
    """AC8(c): every marker present in the tree must have a registry entry
    carrying a non-empty owner and reason. Returns the offending nodeids."""
    offenders = []
    for nodeid in sorted(tree_marked_nodeids):
        entry = registry_entries.get(nodeid)
        if entry is None:
            offenders.append(nodeid)
            continue
        if not entry.get("owner") or entry.get("owner") == "TODO":
            offenders.append(nodeid)
            continue
        if not entry.get("reason") or entry.get("reason") == "TODO":
            offenders.append(nodeid)
    return offenders


def entries_past_review_by(registry_entries: dict, today: datetime.date) -> list:
    """AC8(d): each entry carries `review_by`, past which the entry itself
    fails the gate. Returns the offending nodeids."""
    offenders = []
    for nodeid, entry in sorted(registry_entries.items()):
        review_by = entry.get("review_by")
        if not review_by:
            offenders.append(nodeid)
            continue
        if datetime.date.fromisoformat(review_by) < today:
            offenders.append(nodeid)
    return offenders


# ---------------------------------------------------------------------------
# Live gates -- run against the real tree and registry.
# ---------------------------------------------------------------------------


@pytest.mark.cadence
def test_unmarked_red_is_a_subset_of_the_registry():
    """AC8(b). FAILS by naming the offending nodeid(s) rather than reporting
    a bare count delta -- catches both regrowth and a fix-one-break-one
    swap, since a swap keeps the count flat but changes set membership.

    `cadence`, and the reason is NOT suppression -- it is cost. This gate
    must EXECUTE the whole guard-suite to learn the observed red set, which
    takes minutes; the two gates below are collection-only and instant. Left
    in the fast tier this one alone would add minutes to every commit, and a
    gate that costs that much is one developers learn to route around --
    which is the exact failure this plan exists to end, reintroduced by the
    fix for it. It runs at the cadence gate instead, where a full-suite cost
    is already paid.

    This does NOT reopen the mark-to-satisfy hole, and that is why the split
    is safe: AC8(c) -- `test_every_marker_in_the_tree_has_a_registry_entry`
    -- is the leg that stops a developer marking a cell to make the gate
    pass, and it is collection-only, unmarked, and still fires on every
    commit. Regrowth is caught at cadence; gaming is caught immediately.

    Negative-spec: do NOT mark this `pending_fix` or `designed_red`. AC8(f)
    forbids it, and those markers mean "known-broken" -- a claim about this
    test that would be false. `cadence` is a tier placement, not a verdict.
    """
    module = _load_regenerate_module()
    observed = module.derive_unmarked_red(
        RED_SET_REPORT_TARGET, workers=_LIVE_DERIVATION_WORKERS
    )
    registry = _load_registry()
    offenders = offenders_not_in_registry(observed, registry.get("entries", {}))
    assert not offenders, (
        "unmarked-red nodeid(s) with no known-red.json registry entry -- "
        "regrowth or a fix-one-break-one swap: %s" % offenders
    )


def test_every_marker_in_the_tree_has_a_registry_entry():
    """AC8(c). SEPARATE from gate 1 above on purpose: this is what stops
    marking a cell from making the whole gate pass by itself. Marking a
    cell without a registry entry (or with a TODO/empty owner or reason)
    fails HERE even when gate 1 is green."""
    marked = _collect_pending_fix_or_designed_red_nodeids()
    registry = _load_registry()
    offenders = markers_missing_registry_coverage(marked, registry.get("entries", {}))
    assert not offenders, (
        "pending_fix/designed_red marker(s) with no registry entry (or an "
        "empty/TODO owner or reason) -- marking a cell must be a "
        "deliberate, reviewable act, not a mute button: %s" % offenders
    )


def test_no_registry_entry_is_past_its_review_by():
    """AC8(d). Closes 'decays into permanence' mechanically: a stale entry
    fails the gate on the calendar rather than relying on good intentions."""
    registry = _load_registry()
    offenders = entries_past_review_by(registry.get("entries", {}), datetime.date.today())
    assert not offenders, (
        "known-red.json entry(ies) past their review_by date -- re-adjudicate "
        "or extend deliberately, do not let a marker decay into permanence: %s"
        % offenders
    )


def _collect_pending_fix_or_designed_red_nodeids() -> set:
    """Collection-only pytest run (no execution) against the marker
    expression, to get the exact nodeid set carrying pending_fix or
    designed_red -- independent of pass/fail outcome, unlike
    red-set-report.py's failure-diff technique (which conflates "excluded
    by marker" with "happened not to fail in this particular run" for an
    order-dependent test; see this chunk's own close-out notes).

    Reuses red-set-report.py's `run_pytest_collect`, which reads
    `item.nodeid` directly from an in-process collection plugin, rather
    than scraping `--collect-only -q` STDOUT. A stdout-scraping approach
    silently drops any nodeid containing a space -- and parametrised ids
    routinely do (e.g. `test_foo[value with space]`) -- which would let a
    marked cell with such an id evade AC8(c)'s coverage check entirely.
    """
    module = _load_red_set_report_module()
    return module.run_pytest_collect(
        RED_SET_REPORT_TARGET, marker_expr="pending_fix or designed_red"
    )


def test_marked_nodeid_with_a_space_is_still_collected(tmp_path):
    """Regression for the P1 stdout-scraping bug: a parametrised nodeid
    containing a space (e.g. `test_foo[value with space]`) must still be
    seen as marked. A stdout-scraping collector that drops any line
    containing a space would silently exclude it, letting a developer mark
    such a cell with no registry entry and get a fully green ratchet --
    exactly the mark-to-satisfy attack AC8(c) exists to close."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "markers = [\n"
        '    "pending_fix: known-broken path assumption",\n'
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "test_spacey.py").write_text(
        "import pytest\n"
        "\n"
        '@pytest.mark.pending_fix\n'
        '@pytest.mark.parametrize("value", ["value with space"])\n'
        "def test_marked_with_a_space_in_its_id(value):\n"
        "    assert False, 'expected: marked pending_fix'\n",
        encoding="utf-8",
    )
    module = _load_red_set_report_module()
    marked = module.run_pytest_collect(str(tmp_path), marker_expr="pending_fix")
    assert any(
        "test_marked_with_a_space_in_its_id[value with space]" in nodeid
        for nodeid in marked
    ), "nodeid with a space in its parametrize id was dropped: %s" % marked


# ---------------------------------------------------------------------------
# Directional-proof tests -- synthetic fixtures, no live subprocess. Prove
# the pure check functions above actually discriminate red from green.
# ---------------------------------------------------------------------------


def test_proof_planted_unmarked_red_cell_fails_the_subset_gate():
    """Prove direction 1: a nodeid observed as unmarked-red but ABSENT from
    the registry is a real, named offender."""
    observed = {"bash_guards/tests/test_fake_planted.py::test_new_regression"}
    registry_entries = {
        "bash_guards/tests/test_fake_planted.py::test_already_known": {
            "owner": "someone", "reason": "known", "review_by": "2099-01-01",
        }
    }
    offenders = offenders_not_in_registry(observed, registry_entries)
    assert offenders == ["bash_guards/tests/test_fake_planted.py::test_new_regression"]


def test_proof_registered_cell_passes_the_subset_gate():
    """Prove direction 2: the same nodeid, once given a registry entry, no
    longer offends."""
    observed = {"bash_guards/tests/test_fake_planted.py::test_new_regression"}
    registry_entries = {
        "bash_guards/tests/test_fake_planted.py::test_new_regression": {
            "owner": "someone", "reason": "known", "review_by": "2099-01-01",
        }
    }
    offenders = offenders_not_in_registry(observed, registry_entries)
    assert offenders == []


def test_proof_marker_with_todo_owner_fails_the_coverage_gate():
    marked = {"bash_guards/tests/test_fake_planted.py::test_new_regression"}
    registry_entries = {
        "bash_guards/tests/test_fake_planted.py::test_new_regression": {
            "owner": "TODO", "reason": "TODO",
        }
    }
    offenders = markers_missing_registry_coverage(marked, registry_entries)
    assert offenders == ["bash_guards/tests/test_fake_planted.py::test_new_regression"]


def test_proof_marker_with_real_owner_and_reason_passes_the_coverage_gate():
    marked = {"bash_guards/tests/test_fake_planted.py::test_new_regression"}
    registry_entries = {
        "bash_guards/tests/test_fake_planted.py::test_new_regression": {
            "owner": "some/plan.md", "reason": "a real reason",
        }
    }
    offenders = markers_missing_registry_coverage(marked, registry_entries)
    assert offenders == []


def test_proof_expired_review_by_fails_the_review_gate():
    registry_entries = {
        "bash_guards/tests/test_fake_planted.py::test_new_regression": {
            "review_by": "2020-01-01",
        }
    }
    offenders = entries_past_review_by(registry_entries, datetime.date(2026, 8, 7))
    assert offenders == ["bash_guards/tests/test_fake_planted.py::test_new_regression"]


def test_proof_future_review_by_passes_the_review_gate():
    registry_entries = {
        "bash_guards/tests/test_fake_planted.py::test_new_regression": {
            "review_by": "2099-01-01",
        }
    }
    offenders = entries_past_review_by(registry_entries, datetime.date(2026, 8, 7))
    assert offenders == []
