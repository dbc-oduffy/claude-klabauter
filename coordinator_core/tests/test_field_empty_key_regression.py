"""
coordinator_core.tests.test_field_empty_key_regression — Regression coverage
for the `\\s*`-pads-across-newlines defect in `_field`'s frontmatter-key regex.

Bug: both `coordinator_core.archival._field` and
`coordinator_core.tests._baton_dag_oracle._field` matched a key's value with
a pattern anchoring the key, a literal colon, then a `\\s`-class pad before
the captured value. `\\s` matches a newline, so on a present-but-empty key
(`key:` followed immediately by a newline), that pad consumed the newline
and the capture group then grabbed the FOLLOWING line's content instead of
the empty string — silently returning a neighbouring field's value. Flagged
by test_no_forked_frontmatter_key_regex.py; fixed by padding with a
space/tab-only character class instead, which cannot cross a line boundary.

Both `_field` copies are independently-maintained duplicates (see
archival.py's DR-242 section header and _baton_dag_oracle.py's own note at
its `claimed_or_shipped` re-export site) serving two unrelated consumers, not
a parity contract — each is covered here because each has the same defect in
its own right, not because one owes the other a matching fix.
"""

from __future__ import annotations

from coordinator_core.archival import _field as archival_field
from coordinator_core.tests._baton_dag_oracle import _field as oracle_field


def test_archival_field_present_but_empty_key_returns_empty_string() -> None:
    fm = "\nstatus: consumed\nclaimed_by:\nshipped_in: 1.2.3\n"
    assert archival_field(fm, "claimed_by") == ""
    assert archival_field(fm, "shipped_in") == "1.2.3"


def test_oracle_field_present_but_empty_key_returns_empty_string() -> None:
    fm = "\nstatus: consumed\nclaimed_by:\nshipped_in: 1.2.3\n"
    assert oracle_field(fm, "claimed_by") == ""
    assert oracle_field(fm, "shipped_in") == "1.2.3"
