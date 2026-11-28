"""The default halt path parks a changed run-owned ``.py`` in the record and leaves it in the tree."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_evict, halted_record_commit
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_record_eviction import EVICTED_PARKED
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _record,
    _write,
    repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

OWN = "pkg/own.py"
PEER = "pkg/peer_importer.py"


@pytest.fixture
def halted_py(repo, monkeypatch):
    """OWN changes its binding surface against HEAD; only measured-rev.json names it as the run's."""
    _write(repo, OWN, "V = 0\n")
    _write(repo, PEER, "X = 0\n")
    _git(repo, "add", "pkg")
    _git(repo, "commit", "-q", "-m", "seed pkg")
    head_blob = _git(repo, "rev-parse", f"HEAD:{OWN}").strip()
    _write(repo, OWN, "V = 1\nNEW = 2\n")
    _record(repo)
    _write(
        repo, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json",
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": {OWN: head_blob}}),
    )
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/measured-rev.json", json.dumps({"paths": [OWN], "removed": []}))
    monkeypatch.setattr(halted_record_commit, "_run_written", lambda *_a, **_k: [])
    return repo


def _blob_names(root) -> list:
    blobs = root / RECORD_DIR_ROOT / RUN / halt_tree_evict.BLOBS_DIR
    return sorted(p.name for p in blobs.iterdir()) if blobs.is_dir() else []


def test_a_changed_py_is_parked_and_stays_in_the_tree(halted_py):
    out = _call(halted_py, LEDGER, **PASSING)

    assert out["withheld"][OWN] == EVICTED_PARKED
    assert out["evicted"] == [OWN]
    assert (halted_py / OWN).read_text(encoding="utf-8") == "V = 1\nNEW = 2\n"
    assert len(_blob_names(halted_py)) == 1


def test_a_dirty_importer_outside_the_parked_set_does_not_keep_the_py_out_of_the_record(halted_py):
    _write(halted_py, PEER, "from pkg.own import NEW\n")

    out = _call(halted_py, LEDGER, **PASSING)

    assert out["withheld"][OWN] == EVICTED_PARKED
    assert (halted_py / OWN).read_text(encoding="utf-8") == "V = 1\nNEW = 2\n"
    assert len(_blob_names(halted_py)) == 1


def test_product_landing_carries_an_importer_with_a_name_in_a_landing_chunk(product_repo):
    prod._author(product_repo, "a.py", "from pkg.b import f\n")
    prod._author(product_repo, "pkg/__init__.py", "")
    prod._author(product_repo, "pkg/b.py", "def f():\n    return 1\n")
    prod._declare(product_repo, "a.py", "pkg/b.py", "pkg/__init__.py")
    prod._record_dir(product_repo)

    out = prod._land(product_repo, inline_review=reviewed())

    assert out["committed"] is True and out.get("refused") is None, out
    assert "a.py" not in out.get("withheld", {}), out
    tree = prod._tree(product_repo)
    assert "a.py" in tree and "pkg/b.py" in tree
