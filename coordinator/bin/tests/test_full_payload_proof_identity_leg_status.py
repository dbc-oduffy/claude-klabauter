"""test_full_payload_proof_identity_leg_status — regression coverage for
`percolate-full-payload-proof.py`'s `_parse_end_of_run_leg_status`, closing
the marker-collision that made a virgin-destination full-payload proof
misreport its `identity_check` leg as `skipped-advisory` when the real
`dispatch_end_of_run_identity_check` leg (`coordinator/bin/publish.py`) had
actually run clean.

Traced root cause (task: "the end-of-run identity check silently skips
itself on a first publish"): a full run into a wiped/virgin destination
(`setup/publish-targets.portable` declares the engine row, non-empty
`dest_subdir`, BEFORE the toplevel row that publishes `.github/`) makes
`dispatch_percolate_pre_ci`'s PER-ROW identity check advisory-skip for the
engine row (expected, ratified in that function's own docstring: "a row
with a non-empty `dest_subdir` can run before its sibling toplevel row has
ever published `.github/` to a virgin destination"). That per-row WARNING
text is "... identity checker not found at .../.github/scripts/check-
persona-names.py — pre_ci identity gate SKIPPED for this row (advisory:
...)". The proof harness's OLD `_IDENTITY_SKIP_MARKER`, `"identity checker
not found at"`, is a substring of THAT per-row message too, not just the
end-of-run leg's own `target_filtered=True` advisory print (`"end-of-run
identity checker not found at ..."`) it was meant to detect. By the time the
end-of-run leg itself runs (after every row, including the toplevel row,
has synced within the same pass), the checker IS present and the leg exits
clean and silent — but the parser, scanning the whole pass's stderr rather
than isolating the end-of-run leg's own output, still matched the leftover
per-row substring and misclassified the leg as `skipped-advisory`.

`dispatch_end_of_run_identity_check` itself was already fail-closed and
already covered end-to-end
(`test_full_unfiltered_run_into_virgin_destination_fails`,
`coordinator/bin/tests/test_percolate_identity_check_gate.py`) — this file
pins the property the OTHER measurement layer (the proof harness's own
classifier) needs to hold: it must not read a clean end-of-run leg as
skipped just because an EXPECTED, ratified per-row advisory fired earlier
in the same pass.

Run: python -m pytest coordinator/bin/tests/test_full_payload_proof_identity_leg_status.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_proof_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_full_payload_proof_under_test",
        _BIN_DIR / "percolate-full-payload-proof.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proof = _load_proof_module()

# The exact per-row advisory WARNING text `dispatch_percolate_pre_ci`
# (coordinator/bin/publish.py) prints when a row with a non-empty
# `dest_subdir` runs before its sibling toplevel row has published
# `.github/` to a virgin destination -- the expected, ratified shape row 1
# of a virgin klabauter-style publish produces. Deliberately reproduced
# verbatim here (not imported) so a future wording drift in either file
# shows up as a test failure pointing at THIS collision, not a silent pass.
_PER_ROW_ADVISORY_SKIP_TEXT = (
    "  WARNING: engine-row: identity checker not found at "
    "/scratch/dest-repo/.github/scripts/check-persona-names.py — "
    "pre_ci identity gate SKIPPED for this row (advisory: the "
    "toplevel row may not have published .github/ to this "
    "destination yet).\n"
)

# The end-of-run leg's own `target_filtered=True` advisory WARNING --
# genuinely a `skipped-advisory` end-of-run outcome, distinct from the
# per-row text above.
_END_OF_RUN_ADVISORY_SKIP_TEXT = (
    "  WARNING: end-of-run identity checker not found at "
    "/scratch/dest-repo/.github/scripts/check-persona-names.py — "
    "advisory under --target (this invocation may never have "
    "published .github/ to this destination).\n"
)

# The end-of-run leg's own unfiltered hard-failure text.
_END_OF_RUN_HARD_FAIL_TEXT = (
    "  Error: end-of-run identity check FAILED for /scratch/dest-repo: "
    "checker not found at .github/scripts/check-persona-names.py "
    "after a full, unfiltered run — .github/ was never published "
    "to this destination this run (or a prior one).\n"
)


class TestVirginDestinationRowLevelAdvisoryDoesNotMaskACleanEndOfRunLeg:
    """The exact pass-1 shape: a row-level advisory skip fires (engine row,
    expected), then the end-of-run leg runs afterward, finds the checker
    (published by the toplevel row later in the same pass), and exits
    clean and silent. Must classify as `ran-clean-silent`, never
    `skipped-advisory` -- the property this fix pins."""

    def test_per_row_advisory_alone_is_not_misread_as_end_of_run_skip(self):
        stderr_text = _PER_ROW_ADVISORY_SKIP_TEXT
        status = proof._parse_end_of_run_leg_status(stderr_text)
        assert status["identity_check"] == "ran-clean-silent"

    def test_per_row_advisory_plus_unrelated_other_leg_chatter_still_clean(self):
        # Realistic pass-1 shape: per-row identity advisory fires early,
        # totally unrelated other-leg stderr chatter follows, the identity
        # end-of-run leg itself never prints anything (it ran clean).
        stderr_text = (
            _PER_ROW_ADVISORY_SKIP_TEXT
            + "  NOTE: end-of-run unscanned-published check found a "
            "DELIBERATE exclusion...\n"
        )
        status = proof._parse_end_of_run_leg_status(stderr_text)
        assert status["identity_check"] == "ran-clean-silent"


class TestGenuineEndOfRunOutcomesStillClassifyCorrectly:
    """The fix must not blunt real end-of-run signal -- only the collision
    with the per-row message is removed."""

    def test_genuine_end_of_run_filtered_advisory_skip_still_detected(self):
        status = proof._parse_end_of_run_leg_status(_END_OF_RUN_ADVISORY_SKIP_TEXT)
        assert status["identity_check"] == "skipped-advisory"

    def test_genuine_end_of_run_unfiltered_hard_failure_still_detected(self):
        status = proof._parse_end_of_run_leg_status(_END_OF_RUN_HARD_FAIL_TEXT)
        assert status["identity_check"] == "ran-failed"

    def test_both_per_row_advisory_and_genuine_end_of_run_skip_still_flagged(self):
        # Belt-and-braces: even with the per-row noise present, a REAL
        # end-of-run skip (filtered advisory or unfiltered hard fail) must
        # still surface -- the fix narrows the marker, it does not disable
        # detection of the end-of-run leg's own genuine skip text.
        stderr_text = _PER_ROW_ADVISORY_SKIP_TEXT + _END_OF_RUN_ADVISORY_SKIP_TEXT
        status = proof._parse_end_of_run_leg_status(stderr_text)
        assert status["identity_check"] == "skipped-advisory"

        stderr_text = _PER_ROW_ADVISORY_SKIP_TEXT + _END_OF_RUN_HARD_FAIL_TEXT
        status = proof._parse_end_of_run_leg_status(stderr_text)
        assert status["identity_check"] == "ran-failed"

    def test_no_chatter_at_all_is_clean_silent(self):
        status = proof._parse_end_of_run_leg_status("")
        assert status["identity_check"] == "ran-clean-silent"
