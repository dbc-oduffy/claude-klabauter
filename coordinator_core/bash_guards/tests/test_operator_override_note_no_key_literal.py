"""Regression pin for `operator_override_note`'s 2026-08-11 "doc pointer
ONLY" reshape (see that function's own docstring, "RESHAPED AGAIN
2026-08-11" and NEGATIVE SPEC 5, at `coordinator_core/bash_guards/
_helpers.py:356`): the render must carry NO override env-key literal, for
ANY key in the fleet's `COORDINATOR_*` override vocabulary, across every
input shape the function accepts -- not merely the two synthetic keys the
sibling pin (`test_operator_override_note_no_assignment_form.py`) already
exercises.

SEAM USED FOR THE KEY SET: this module re-derives the key set at test time
from `docs/reference/guard-override-keys.md`'s two markdown tables ("##
Override keys, by guard" and "## Override keys outside the bash-guard/
write-guard suite") -- both are `| \\`COORDINATOR_...\\` | ... |` rows, a
stable, greppable seam. This is a deliberate re-derivation, not a hand-
copied snapshot: a future key added to either table is picked up on the
next test run with no edit here. If that doc's table shape ever stops
being machine-parseable, fall back to the four documented prefix families
(`COORDINATOR_OVERRIDE_*`, `COORDINATOR_ALLOW_*`, `COORDINATOR_DISABLE_*`,
plus the two literal singletons `COORDINATOR_PROBE_NUDGE_OFF` and
`COORDINATOR_SCHEMA_STRICT`) instead -- not needed today, since the doc's
tables parsed cleanly as of this writing.

NEGATIVE SPEC -- this does not assert the render is byte-identical to any
particular doc-pointer string (that is `test_operator_override_note_
retains_affordances.py`'s job); it asserts only the ABSENCE of any
override-key literal, which is the invariant a re-interpolation regression
would break.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.bash_guards._helpers import operator_override_note

_DOC_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "reference"
    / "guard-override-keys.md"
)

#: Matches a `| \`COORDINATOR_WHATEVER\` |` (or `\`COORDINATOR_WHATEVER\` (leg)`)
#: table-row cell -- the first column of both override-key tables in the
#: doc. Deliberately permissive about what follows the backtick (a
#: parenthetical like "(Bash leg)" is common) since only the key itself is
#: extracted.
_TABLE_ROW_KEY_RE = re.compile(r"^\|\s*`(COORDINATOR_[A-Z0-9_]+)`")

#: Fallback prefix families (see module docstring) if the doc's table seam
#: is ever unavailable.
_FALLBACK_KEYS = (
    "COORDINATOR_OVERRIDE_FALLBACK_SENTINEL",
    "COORDINATOR_ALLOW_FALLBACK_SENTINEL",
    "COORDINATOR_DISABLE_FALLBACK_SENTINEL",
    "COORDINATOR_PROBE_NUDGE_OFF",
    "COORDINATOR_SCHEMA_STRICT",
)


def _derive_override_keys() -> list:
    if not _DOC_PATH.is_file():
        return list(_FALLBACK_KEYS)
    keys = set()
    for line in _DOC_PATH.read_text(encoding="utf-8").splitlines():
        match = _TABLE_ROW_KEY_RE.match(line)
        if match:
            keys.add(match.group(1))
    if not keys:
        return list(_FALLBACK_KEYS)
    return sorted(keys)


_OVERRIDE_KEYS = _derive_override_keys()


def test_key_set_derivation_is_non_empty():
    """Guard the guard: if this ever comes back empty, every test below
    would vacuously pass having exercised nothing -- the exact failure
    mode this pin exists to prevent. Fail loudly instead."""
    assert _OVERRIDE_KEYS, (
        "derived zero override keys from %s (or its fallback) -- the "
        "key-literal check below would be vacuous" % _DOC_PATH
    )


@pytest.mark.parametrize("env_var", _OVERRIDE_KEYS)
def test_flag_shaped_render_carries_no_key_literal(env_var):
    note = operator_override_note(env_var)
    assert env_var not in note, (
        "operator_override_note(%r) (flag-shaped) re-interpolated the "
        "override key into its render -- got: %r" % (env_var, note)
    )


@pytest.mark.parametrize("env_var", _OVERRIDE_KEYS)
def test_reason_shaped_render_carries_no_key_literal(env_var):
    note = operator_override_note(env_var, reason_placeholder="not now, doing X")
    assert env_var not in note, (
        "operator_override_note(%r, reason_placeholder=...) (reason-shaped) "
        "re-interpolated the override key into its render -- got: %r"
        % (env_var, note)
    )


def test_empty_string_call_carries_no_key_literal():
    """`operator_override_note("")` -- the load-bearing empty-arg call used
    by `_message_size._OVERRIDE_NOTE_TAIL` (module-import-time evaluation,
    out of scope to touch here). Must render the same doc-pointer-only
    string as any other input, with nothing key-shaped in it."""
    note = operator_override_note("")
    assert note, "operator_override_note('') rendered an empty string"
    for env_var in _OVERRIDE_KEYS:
        assert env_var not in note, (
            "operator_override_note('') unexpectedly contains an override "
            "key literal (%r) -- got: %r" % (env_var, note)
        )


def test_render_carries_no_bare_coordinator_prefix_at_all():
    """Belt-and-suspenders: no COORDINATOR_ prefixed token of any shape
    should appear in the render, not just the specific keys this repo
    happens to enumerate today."""
    note = operator_override_note("COORDINATOR_ALLOW_BELT_AND_SUSPENDERS_CHECK")
    assert "COORDINATOR_" not in note, (
        "operator_override_note() render contains a COORDINATOR_-prefixed "
        "token -- got: %r" % note
    )
