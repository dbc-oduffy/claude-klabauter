"""final_review: the integration phase spelling and its target mandate clause."""

import pytest

from coordinator_core.ops.review_mint.execute_review import REVERIFY_PHASE
from coordinator_core.ops.review_mint.final_review import final_review_mandate

STATEMENT = "The emitted workflow ends review with one verified Opus pass."


def test_statement_verbatim_once():
    assert final_review_mandate(STATEMENT).count(STATEMENT) == 1


@pytest.mark.parametrize("bad", ["", "   \n"])
def test_empty_statement_raises(bad):
    with pytest.raises(ValueError):
        final_review_mandate(bad)


def test_grants_edit_rights_and_residue_ledger():
    out = final_review_mandate(STATEMENT)
    assert "Edit any declared path" in out
    assert "residue ledger" in out


def test_names_review_reverify():
    assert f"`{REVERIFY_PHASE}`" in final_review_mandate(STATEMENT)
