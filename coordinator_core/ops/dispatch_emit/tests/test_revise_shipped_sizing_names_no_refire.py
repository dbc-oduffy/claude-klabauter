"""workflow.revise on a finished run whose sizing is shipped names no refire; a sized sizing still does."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_revise_on_shipped_sizing_accepted import (  # noqa: F401
    REL, RUN, WORDS, _ship, _met, repo,
)

ENTRY = {"chunk": "C5", "evidence": "ledger R1 met"}


def test_credit_on_a_shipped_sizing_names_no_refire(repo):
    _ship(repo, _met)
    out = rl._handler({"run_id": RUN, "credit_chunks": [ENTRY]}, repo_root=repo)
    assert out["ok"], out
    assert "next" not in out
    assert "closes" in out["note"]


def test_credit_on_a_sized_sizing_still_names_the_refire(repo):
    out = rl._handler({"run_id": RUN, "credit_chunks": [ENTRY]}, repo_root=repo)
    assert out["ok"], out
    assert out["next"]["kind"] == "refire"
    assert REL in out["next"]["call"]


def test_a_new_row_on_a_shipped_sizing_reopens_it_and_still_names_the_refire(repo):
    _ship(repo, _met)
    out = rl._handler({"run_id": RUN, "verbatim": WORDS}, repo_root=repo)
    assert out["ok"], out
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["status"] == "routed"
    assert out["next"]["kind"] == "refire"
