"""Tests for coordinator_core.symbol_knowledge.freshness against a synthetic example-retrieval-repo store."""

from __future__ import annotations


import pytest

from coordinator_core.symbol_knowledge import freshness
from coordinator_core.symbol_knowledge.tests.test_flag import PARENT_SRC, PREFIX

pytestmark = [pytest.mark.complete]

OTHER = PREFIX + "Box#other()."
NEW_SRC = PARENT_SRC.replace("return 1", "return 11").replace("return 3", "return 33").replace("return 4", "return 44")


def test_missing_store_is_not_applicable_naming_path_and_table(tmp_path):
    out = freshness.freshness(tmp_path, "HEAD")
    assert out["status"] == "not_applicable"
    assert out["store"] == freshness.STORE_REL and out["table"] == freshness.FILES_TABLE
