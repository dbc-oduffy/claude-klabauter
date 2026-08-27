"""The ratchet gate for `state/generator-provenance/unresolved-writers.json`.

Spec backlink: state/sizings/2026-08-14-cut-the-unresolved-writer-population-and.yaml
spike_amendments § P2-ratchet.

WHY THIS EXISTS
    P1 (`coordinator_core.ops.generator_provenance`) cut the
    `Verdict.WRITE_TARGET_UNRESOLVED` population from 230 to 200 by six
    mechanical, regression-tested rules. Nothing yet stops it growing back --
    a new module joining the population the same way the original 230 did,
    one unresolved write at a time, with nothing naming the addition as a
    reviewable event. This module carries THREE SEPARATELY-FAILING gates, so
    each failure mode names itself rather than reporting an ambiguous delta:

      1. `test_unresolved_set_is_a_subset_of_the_baseline` -- catches
         regrowth AND the fix-one-break-one swap, by module path.
      2. `test_every_baseline_entry_has_a_nonempty_reason_and_owner` --
         catches silence-by-regeneration: a "TODO" reason/owner counts as
         NOT adjudicated. Expected RED on landing (all 200 seed entries are
         literal "TODO") -- that is the design, not a defect. Marked
         `designed_red` for that reason (see module docstring's own
         Marker placement note below) so the shared fast tier stays green;
         its failure output is the adjudication worklist.
      3. `test_no_baseline_entry_is_past_its_review_by` -- catches decay
         into permanence, on the calendar rather than good intentions.

    Marker placement -- the one deliberate exception to this module's own
    negative spec below: gate 2 alone carries `designed_red`. The spec's
    prohibition is scoped to gates 1 and 3, whose red state would mean a
    live regression; gate 2's red state on landing IS the adjudication
    worklist by design (spike_amendments § P2-ratchet), and per
    `docs/reference/test-tiers.md` a `designed_red` test's failure output is
    a worklist to read, never a fact to gate a commit on -- exactly what a
    freshly-seeded, all-TODO baseline needs so it does not break the shared
    fast tier for every session on this machine. `fast_test_cmd` deselects
    `designed_red` (`-m 'not cadence and not pending_fix and not
    designed_red'`), so gate 2 is invisible there and visible at cadence.

NEGATIVE SPEC
    - Gates 1 and 3 carry no `pending_fix`/`cadence`/`designed_red` marker
      anywhere in this module -- the ratchet exempting itself on those two
      gates is the first thing a frustrated developer would reach for, and
      it would silently defeat regrowth/decay detection.
    - Does not re-derive the unresolved-writer population independently of
      `coordinator_core.ops.generator_provenance.discover_generators` --
      imported directly, via
      `coordinator.bin.regenerate-unresolved-writers`'s own
      `derive_unresolved_writers`, never reimplemented.
    - Does not hardcode a module-path count anywhere -- every assertion
      below reads the baseline and the live derivation, never a literal
      number.
    - Lives outside `state/generator-provenance/` (the tree it measures),
      at `coordinator_core/ops/tests/`.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_PATH = REPO_ROOT / "state" / "generator-provenance" / "unresolved-writers.json"
REGENERATE_SCRIPT = REPO_ROOT / "coordinator" / "bin" / "regenerate-unresolved-writers.py"


def _load_regenerate_module():
    spec = importlib.util.spec_from_file_location(
        "_unresolved_writers_regenerate", REGENERATE_SCRIPT
    )
    assert spec is not None and spec.loader is not None, (
        "could not load %s" % REGENERATE_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pure check functions -- the logic under test, kept free of live derivation
# so the directional-proof tests below can exercise them against synthetic
# fixtures.
# ---------------------------------------------------------------------------


def offenders_not_in_baseline(observed_unresolved: set, baseline_entries: dict) -> list:
    """The subset gate: the observed unresolved-writer module-path set must
    be a SUBSET of the baseline. Returns the offending module paths (empty
    means the gate passes)."""
    return sorted(observed_unresolved - set(baseline_entries.keys()))


PLACEHOLDER_VALUES = frozenset({"todo", "tbd", "n/a", "na", "-", "?", ""})


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    return value.strip().casefold() in PLACEHOLDER_VALUES


def entries_missing_reason_or_owner(baseline_entries: dict) -> list:
    """The coverage gate: every baseline entry must carry a non-empty
    reason and owner, and neither may be a placeholder ("TODO", "TBD",
    "N/A", "-", "?", any case/whitespace variant thereof, or empty after
    stripping) -- not adjudicated. Returns the offending module paths."""
    offenders = []
    for module_path, entry in sorted(baseline_entries.items()):
        reason = entry.get("reason")
        owner = entry.get("owner")
        if _is_placeholder(reason) or _is_placeholder(owner):
            offenders.append(module_path)
    return offenders


def entries_past_review_by(baseline_entries: dict, today: datetime.date) -> list:
    """The review-window gate: each entry carries `review_by`, past which
    the entry itself fails the gate. Returns the offending module paths."""
    offenders = []
    for module_path, entry in sorted(baseline_entries.items()):
        review_by = entry.get("review_by")
        if not review_by:
            offenders.append(module_path)
            continue
        if datetime.date.fromisoformat(review_by) < today:
            offenders.append(module_path)
    return offenders


# ---------------------------------------------------------------------------
# Live gates -- run against the real tree and baseline.
# ---------------------------------------------------------------------------


@pytest.mark.real_home  # live-tree oracle: derive_unresolved_writers resolves
# the real claude-klabauter root via the machine-local registry
# (coordinator/lib/resolve-claude-klabauter) -- under the suite's default home-
# quarantine fixture that registry key is unresolvable and every call fails
# closed with ClaudeKlabauterResolutionError before this gate's own subset check ever
# runs. Read-only against the live tree, so the standard opt-out applies.
def test_unresolved_set_is_a_subset_of_the_baseline():
    """Catches regrowth and the fix-one-break-one swap: a swap keeps the
    population count flat but changes module-path membership, which this
    gate names directly rather than reporting a bare count delta."""
    module = _load_regenerate_module()
    observed = module.derive_unresolved_writers(REPO_ROOT)
    baseline = _load_baseline()
    offenders = offenders_not_in_baseline(observed, baseline.get("entries", {}))
    assert not offenders, (
        "module(s) newly write through an unresolved path expression with no "
        "GENERATES, and are not in the baseline "
        "(state/generator-provenance/unresolved-writers.json):\n\n  %s\n\n"
        "Declare what each emits at the site: GENERATES = [{\"artifact\": ..., "
        "\"stamp_key\": ..., \"sources\": [...]}], or GENERATES = [] if it "
        "emits no tracked artifact. To acknowledge without adjudicating: "
        "coordinator/bin/regenerate-unresolved-writers.py --add-missing "
        "(writes TODO owner/reason; the coverage gate stays red until filled "
        "in)." % "\n  ".join(offenders)
    )


def test_every_baseline_entry_has_a_nonempty_reason_and_owner():
    """The forcing function that makes `--add-missing` an acknowledgement
    rather than an escape hatch.

    Landed `designed_red` while the seeded baseline carried 200 literal "TODO"
    pairs, when its failure output WAS the adjudication worklist. That worklist
    is now empty -- every entry carries a real reason and owner -- so the marker
    is gone and this runs live in the fast tier. That promotion is the point:
    `--add-missing` writes TODO, TODO fails here, and a new undeclared writer
    therefore cannot be parked silently. Re-marking this `designed_red` to get
    a red tier green again would reopen exactly the hole it closes; adjudicate
    the entry instead."""
    baseline = _load_baseline()
    offenders = entries_missing_reason_or_owner(baseline.get("entries", {}))
    assert not offenders, (
        "unresolved-writers.json entry(ies) with an empty or TODO reason/owner "
        "-- adjudicate (fill in a real reason and owner) or declare GENERATES "
        "at the site: %s" % offenders
    )


def test_no_baseline_entry_is_past_its_review_by():
    """Closes 'decays into permanence' mechanically: a stale entry fails the
    gate on the calendar rather than relying on good intentions."""
    baseline = _load_baseline()
    offenders = entries_past_review_by(baseline.get("entries", {}), datetime.date.today())
    assert not offenders, (
        "unresolved-writers.json entry(ies) past their review_by date -- "
        "re-adjudicate or extend deliberately, do not let an entry decay into "
        "permanence: %s" % offenders
    )


# ---------------------------------------------------------------------------
# Directional-proof tests -- synthetic fixtures, no live derivation. Prove
# the pure check functions above actually discriminate offending state from
# clean state.
# ---------------------------------------------------------------------------


def test_proof_planted_unresolved_writer_fails_the_subset_gate():
    observed = {"coordinator_core/gen_new_regression.py"}
    baseline_entries = {
        "coordinator_core/gen_already_known.py": {
            "reason": "known", "owner": "someone", "review_by": "2099-01-01",
        }
    }
    offenders = offenders_not_in_baseline(observed, baseline_entries)
    assert offenders == ["coordinator_core/gen_new_regression.py"]


def test_proof_baselined_writer_passes_the_subset_gate():
    observed = {"coordinator_core/gen_new_regression.py"}
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {
            "reason": "known", "owner": "someone", "review_by": "2099-01-01",
        }
    }
    offenders = offenders_not_in_baseline(observed, baseline_entries)
    assert offenders == []


def test_proof_todo_reason_and_owner_fails_the_coverage_gate():
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {"reason": "TODO", "owner": "TODO"},
    }
    offenders = entries_missing_reason_or_owner(baseline_entries)
    assert offenders == ["coordinator_core/gen_new_regression.py"]


@pytest.mark.parametrize(
    "reason",
    ["todo", "Todo", "TODO ", " todo ", "tbd", "TBD", "n/a", "N/A", "-", "?", "   "],
)
def test_proof_placeholder_reason_spelling_fails_the_coverage_gate(reason):
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {"reason": reason, "owner": "someone"},
    }
    offenders = entries_missing_reason_or_owner(baseline_entries)
    assert offenders == ["coordinator_core/gen_new_regression.py"]


@pytest.mark.parametrize(
    "owner",
    ["todo", "Todo", "TODO ", " todo ", "tbd", "TBD", "n/a", "N/A", "-", "?", "   "],
)
def test_proof_placeholder_owner_spelling_fails_the_coverage_gate(owner):
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {"reason": "a real reason", "owner": owner},
    }
    offenders = entries_missing_reason_or_owner(baseline_entries)
    assert offenders == ["coordinator_core/gen_new_regression.py"]


def test_proof_real_reason_and_owner_passes_the_coverage_gate():
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {
            "reason": "a real reason", "owner": "some/plan.md",
        },
    }
    offenders = entries_missing_reason_or_owner(baseline_entries)
    assert offenders == []


def test_proof_expired_review_by_fails_the_review_gate():
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {"review_by": "2020-01-01"},
    }
    offenders = entries_past_review_by(baseline_entries, datetime.date(2026, 8, 14))
    assert offenders == ["coordinator_core/gen_new_regression.py"]


def test_proof_future_review_by_passes_the_review_gate():
    baseline_entries = {
        "coordinator_core/gen_new_regression.py": {"review_by": "2099-01-01"},
    }
    offenders = entries_past_review_by(baseline_entries, datetime.date(2026, 8, 14))
    assert offenders == []
