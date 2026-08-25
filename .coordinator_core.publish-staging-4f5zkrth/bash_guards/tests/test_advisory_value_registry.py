"""Registry-validation coverage for `GuardEntry.advisory_value` (H5,
docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md).

Three structural asserts, matching the posture `test_guard_band_membership.py`
already holds -- read the live registration via `dispatch._build_guard_chain`
with a harmless dummy command/payload and inspect `GuardEntry` attributes;
never invoke the `fn` closures. Plus the total-predicate exercise (finding 2)
against the two reachable no-`hookSpecificOutput` envelope shapes.

  (a) AC-1 -- every REGISTERED guard's `advisory_value` is not UNCLASSIFIED.
  (b) AC-4 -- a guard registered WITHOUT an explicit `advisory_value`
      defaults to UNCLASSIFIED and the suppression predicate returns
      `False` (not-suppressed, i.e. SHOWN) for it on a non-Windows host.
  (c) AC-2 -- exactly one write site for literal `AdvisoryValue.<MEMBER>`
      references: `_advisory_value.py` (the enum's own definition) and
      `dispatch.py` (the registration). Nowhere else -- not
      `_blanket_disarm.py`, not a skill body, not a wiki page. A bare
      TYPE-level reference to `AdvisoryValue` (e.g. an annotation on a
      future typed record) is explicitly PERMITTED elsewhere; only literal
      MEMBER references are confined.
  (d) Finding 2 -- the suppression predicate is TOTAL: it must not raise
      against `{}` or a `hookSpecificOutput`-less dict, and must return
      `False` (not-suppressed) for both.

Spec backlink: DoE-claude:pln-os-aware-guard-advisory-defaul-060dbe
(DoE-claude) row H5.
"""
from __future__ import annotations

import pathlib

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._advisory_value import AdvisoryValue, suppress_advisory
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

_BASH_GUARDS_DIR = pathlib.Path(__file__).resolve().parents[1]


def _dummy_chain():
    """Build the real registration with harmless dummy call-time arguments,
    exactly as `test_guard_band_membership.py::_dummy_chain` does -- only
    registration-time facts (`name`, `advisory_value`) are inspected here;
    no `fn` closure is ever invoked."""
    return dispatch._build_guard_chain(
        cmd="echo bash-guard-advisory-value-probe",
        session_id="advisory-value-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )


# ---------------------------------------------------------------------------
# (a) AC-1: every registered guard carries an explicit, non-UNCLASSIFIED
# advisory_value -- including every CONFINEMENT_DENY entry (H3: no band is
# exempt from this requirement).
# ---------------------------------------------------------------------------


def test_every_registered_guard_is_classified():
    chain = _dummy_chain()
    unclassified = [
        entry.name for entry in chain if entry.advisory_value is AdvisoryValue.UNCLASSIFIED
    ]
    assert not unclassified, (
        "The following registered guard(s) carry no explicit advisory_value "
        "(AC-1 violation): %r. Classify them per "
        "state/plan-sidecars/2026-07-30-guard-advisory-value-classification.md "
        "(DoE-claude) rather than leaving the dataclass default in place." % unclassified
    )


def test_chain_is_non_trivial():
    """Guard the guard: an empty/short chain would make the assertion
    above pass vacuously."""
    chain = _dummy_chain()
    assert len(chain) >= 30, chain


# ---------------------------------------------------------------------------
# (b) AC-4: a guard registered WITHOUT an explicit advisory_value defaults
# to UNCLASSIFIED, which renders SHOWN -- exercised through the REAL
# suppression predicate against a REAL planted registration, not asserted
# about the dataclass default in the abstract.
# ---------------------------------------------------------------------------


def test_unclassified_guard_defaults_to_shown_on_non_windows():
    planted = GuardEntry(
        "planted-unclassified-guard",
        lambda: None,
        False,
        GuardBand.ADVISORY_REWRITE,
        # advisory_value deliberately OMITTED -- this is the "author of
        # guard N+1 forgot to classify it" case AC-4 exists for.
    )
    assert planted.advisory_value is AdvisoryValue.UNCLASSIFIED

    pure_advisory_envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "planted advisory text",
        }
    }
    suppressed = suppress_advisory(
        pure_advisory_envelope,
        advisory_value=planted.advisory_value,
        band=planted.band,
        host_is_windows=False,
    )
    assert suppressed is False, (
        "An unclassified guard's envelope must NOT be suppressed on a "
        "non-Windows host -- AC-4 requires it render SHOWN by default."
    )


# ---------------------------------------------------------------------------
# (c) AC-2: exactly one write site for literal AdvisoryValue.<MEMBER>
# references. A bare type-level reference (e.g. `AdvisoryValue` used as a
# type annotation with no member access) is permitted anywhere; only
# literal member accesses are confined to the two files below.
# ---------------------------------------------------------------------------

_ALLOWED_MEMBER_REFERENCE_FILES = {
    _BASH_GUARDS_DIR / "_advisory_value.py",
    _BASH_GUARDS_DIR / "dispatch.py",
}

_MEMBER_NAMES = tuple(member.name for member in AdvisoryValue)


def _literal_member_reference_lines(text: str) -> list:
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for member in _MEMBER_NAMES:
            if ("AdvisoryValue.%s" % member) in line:
                hits.append((lineno, member, line.strip()))
    return hits


def test_advisory_value_member_references_confined_to_two_files():
    offenders = {}
    for path in _BASH_GUARDS_DIR.rglob("*.py"):
        if path in _ALLOWED_MEMBER_REFERENCE_FILES:
            continue
        if "/tests/" in str(path).replace("\\", "/"):
            # AC-2 confines PRODUCTION registration/definition sites -- a
            # second place the running dispatcher could read a classification
            # from. A test is not such a place: it registers nothing, and the
            # dispatcher never imports it.
            #
            # Tests are excluded as a CLASS rather than by filename because a
            # test that restates the classification is doing the opposite of
            # what AC-2 forbids. `test_advisory_value_host_default.py` holds a
            # hand-written `_EXPECTED_VALUE` map deliberately: it is the
            # INDEPENDENT ORACLE for the registry, so a mis-classification
            # there fails loudly. Deriving that map from the registry instead
            # would make the assertion tautological -- it would pass on any
            # registry, including a wrong one -- so the duplication is the
            # point, and a filename-scoped exclusion would have made the next
            # such oracle look like a violation.
            continue
        text = path.read_text(encoding="utf-8")
        hits = _literal_member_reference_lines(text)
        if hits:
            offenders[str(path)] = hits
    assert not offenders, (
        "Literal AdvisoryValue.<MEMBER> references found outside the two "
        "permitted registration/definition sites (AC-2 -- 'no second "
        "copy'): %r. A bare type-level `AdvisoryValue` annotation is "
        "permitted anywhere; only literal member access is confined." % offenders
    )


# ---------------------------------------------------------------------------
# (d) Finding 2: the suppression predicate is TOTAL -- must not raise
# against `{}` or a hookSpecificOutput-less dict, and must return False
# (not-suppressed) for both.
# ---------------------------------------------------------------------------


def test_predicate_is_total_against_bare_empty_dict():
    result = suppress_advisory(
        {},
        advisory_value=AdvisoryValue.WINDOWS_COST_ONLY,
        band=GuardBand.ADVISORY_REWRITE,
        host_is_windows=False,
    )
    assert result is False


def test_predicate_is_total_against_hookspecificoutput_less_dict():
    result = suppress_advisory(
        {"marker": "advisory"},
        advisory_value=AdvisoryValue.WINDOWS_COST_ONLY,
        band=GuardBand.ADVISORY_REWRITE,
        host_is_windows=False,
    )
    assert result is False


def test_predicate_is_total_against_non_dict_envelope():
    """Belt-and-suspenders: a caller error that hands a non-dict `out`
    (should be structurally impossible given every guard's own return
    type, but the predicate's TOTAL guarantee should not depend on that)
    must not raise either."""
    result = suppress_advisory(
        None,
        advisory_value=AdvisoryValue.WINDOWS_COST_ONLY,
        band=GuardBand.ADVISORY_REWRITE,
        host_is_windows=False,
    )
    assert result is False
