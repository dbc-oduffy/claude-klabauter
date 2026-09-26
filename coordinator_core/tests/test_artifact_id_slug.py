
from __future__ import annotations

import pytest

from coordinator_core.artifact_id_slug import id_slug


def test_a_cut_landing_on_a_separator_does_not_leave_one():
    assert id_slug("corpus-knowledge-delivery-raw-and-more", 30) == "corpus-knowledge-delivery-raw"


def test_an_interior_run_collapses():
    assert id_slug("a--b---c") == "a-b-c"


def test_a_clean_slug_is_unchanged():
    assert id_slug("corpus-knowledge-delivery", 40) == "corpus-knowledge-delivery"


def test_it_is_idempotent():
    once = id_slug("a--b-", 40)
    assert id_slug(once, 40) == once


@pytest.mark.parametrize("raw", ["", "-", "---"])
def test_a_slug_that_is_only_separators_empties_rather_than_carrying_them(raw):
    assert id_slug(raw) == ""


def test_no_limit_means_no_truncation():
    raw = "a" * 200
    assert id_slug(raw) == raw
