"""The land_blobs parity harness on a recorded fixture: a source repo whose ledger holds landing commits and whose
`scratch/warp/<run>/` holds each run's record is replayed through the registered op, and the harness reports a
match, a miss with its reason, or a named skip."""

from __future__ import annotations


import pytest

from coordinator_core.benchmarks import land_blobs_parity as parity


RUN_1 = "ask-20261114T100000-aaaaaa"
RUN_2 = "ask-20261114T100100-bbbbbb"
RUN_3 = "ask-20261114T100200-cccccc"
#: `main` calls `declare_benchmark_origin`, which writes this variable; the CLI tests pin it first so teardown restores it.
ORIGIN_ENV = "COORDINATOR_INVOCATION_ORIGIN"


@pytest.mark.parametrize("recorded,replayed,match", [
    ({"kind": "landed", "tree": "t1"}, {"kind": "landed", "tree": "t1"}, True),
    ({"kind": "landed", "tree": "t1"}, {"kind": "landed", "tree": "t2"}, False),
    ({"kind": "refused", "refused": "secret-in-diff"}, {"kind": "refused", "refused": "secret-in-diff"}, True),
    ({"kind": "refused", "refused": "secret-in-diff"}, {"kind": "refused", "refused": "foreign-hunk"}, False),
    ({"kind": "landed", "tree": "t1"}, {"kind": "refused", "refused": "foreign-hunk"}, False),
    ({"kind": "refused", "refused": "unreviewed"}, {"kind": "landed", "tree": "t1"}, False),
    ({"kind": "landed", "tree": "t1"}, {"kind": "nothing-to-commit"}, False),
    ({"kind": "landed", "tree": "t1"}, {"kind": "error", "error": "boom"}, False),
])
def test_compare_matches_only_the_same_tree_or_the_same_refusal_kind(recorded, replayed, match):
    assert parity.compare(recorded, replayed)[0] is match


# --- the worktree-sync case ------------------------------------------------------------------------------

BIG = "pkg/big.py"
