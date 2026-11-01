"""`review.reverify` never classes as foreign a failing node the base measured green.

Run ask-20261031T225655-406468 cleared a change whose own reverify failed a test labelled `foreign`.
The route that fired for it: the node's test file was in `_created`'s foreign set (a session write the
record could not attribute to this run's agent), so `foreign_node` took it without asking the base;
`_sibling_caused` (a live sibling run declares the test file or an import) is the second route that
never asked. With the base measured, a node absent from it is the run's own regression and flows to
`unattributed`; an unmeasured base keeps the foreign classification."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.tests.test_review_reverify_foreign_live_run import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, SIBLING_TEST, world,
)

IMPORTS_SIBLING = "tests/test_imports_sibling.py::test_b"
UNATTRIBUTED_FILE = "tests/test_nobody.py"
FOREIGN_FILE_NODE = f"{UNATTRIBUTED_FILE}::test_d"


def _call(root, nodes, **kw):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=nodes, **kw
    )


@pytest.fixture
def foreign_file(world, monkeypatch):
    real = rr._created
    monkeypatch.setattr(
        rr, "_created", lambda *a, **k: (real(*a, **k)[0], [UNATTRIBUTED_FILE])
    )
    return world


@pytest.mark.parametrize("node", [f"{SIBLING_TEST}::test_a", IMPORTS_SIBLING])
def test_sibling_route_base_green_node_is_unattributed(world, node):
    out = _call(world, [node], failing_at_base=[])
    assert out["foreign"] == [] and out["unattributed"] == [node]
    assert out["passed_at_base"] == [node]


@pytest.mark.parametrize("node", [f"{SIBLING_TEST}::test_a", IMPORTS_SIBLING])
def test_sibling_route_unmeasured_base_stays_foreign(world, node):
    out = _call(world, [node])
    assert out["foreign"] == [node] and out["passed_at_base"] == []


def test_sibling_route_red_at_base_is_pre_existing_not_foreign(world):
    node = f"{SIBLING_TEST}::test_a"
    out = _call(world, [node], failing_at_base=[node])
    assert out["foreign"] == [] and out["pre_existing"] == [node]
    assert out["passed_at_base"] == []


def test_foreign_file_route_base_green_node_is_unattributed(foreign_file):
    out = _call(foreign_file, [FOREIGN_FILE_NODE], failing_at_base=[])
    assert out["foreign"] == [] and out["unattributed"] == [FOREIGN_FILE_NODE]


def test_foreign_file_route_unmeasured_base_stays_foreign(foreign_file):
    out = _call(foreign_file, [FOREIGN_FILE_NODE])
    assert out["foreign"] == [FOREIGN_FILE_NODE]
