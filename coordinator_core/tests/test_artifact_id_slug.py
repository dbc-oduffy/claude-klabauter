"""A truncated slug never leaves a doubled dash in an artifact id.

An id is `<prefix>-<slug>-<6hex>`, and every composition site truncates the slug
to a fixed width first. A cut landing on a separator leaves a trailing dash, and
the id carries `--` at the hex boundary: `hnd-corpus-knowledge-delivery-raw--b8256d`.
Schema-valid under `[a-z0-9-]+` and readable, which is why it survived. The
sidecar writer collapses the run when it names the file, so an id-keyed lookup
reports a record absent from the directory it just read.

Measured 2026-09-11 fleet-wide: 45 landed ids already carry one, and a
missing-integration-record check reported a false positive on one. Those 45 are
join keys and cannot be renamed; this is how the set stops growing.
"""

from __future__ import annotations

import pytest

from coordinator_core.artifact_id_slug import id_slug


def test_a_cut_landing_on_a_separator_does_not_leave_one():
    """The ordering IS the fix. Every site replaced here stripped first and
    truncated after, which is precisely what produces the trailing dash."""
    assert id_slug("corpus-knowledge-delivery-raw-and-more", 30) == "corpus-knowledge-delivery-raw"


def test_an_interior_run_collapses():
    assert id_slug("a--b---c") == "a-b-c"


def test_a_clean_slug_is_unchanged():
    assert id_slug("corpus-knowledge-delivery", 40) == "corpus-knowledge-delivery"


def test_it_is_idempotent():
    """Callers that already collapse lose nothing by calling this too, which is
    what lets it be added at a site without auditing that site's own slugifier."""
    once = id_slug("a--b-", 40)
    assert id_slug(once, 40) == once


@pytest.mark.parametrize("raw", ["", "-", "---"])
def test_a_slug_that_is_only_separators_empties_rather_than_carrying_them(raw):
    """Callers fall back to a literal (`or "derived"`) on an empty slug. Returning
    a bare dash instead would satisfy that falsy check and mint `hnd---<hex>`."""
    assert id_slug(raw) == ""


def test_no_limit_means_no_truncation():
    raw = "a" * 200
    assert id_slug(raw) == raw
