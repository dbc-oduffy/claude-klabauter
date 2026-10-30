"""dispatch.land_run: a halted co-declarer owning no hunks in the path (an unstarted dependent, a run that left the file as staged) does not hold it; one owning hunks still does."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/ask_compose.py"
PREDECESSOR = prod.RUN
DEPENDENT = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 60))}-c595ec"
OTHER_BLOB = "1" * 40


def _commit_base(root) -> str:
    prod._write(root, PATH, "x = 0\n")
    prod._git(["add", PATH], root)
    prod._git(["commit", "-q", "-m", "base"], root)
    return prod._git(["rev-parse", f"HEAD:{PATH}"], root).strip()


def _manifest(root, run_id: str, baseline: str) -> None:
    prod._write(
        root, f"{prod.RECORD_DIR_ROOT}/{run_id}/manifest.json",
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: baseline}}),
    )


def _halted_dependent(root, baseline: str, halted: str = "dependents_unstarted") -> None:
    _manifest(root, DEPENDENT, baseline)
    rdir = f"{prod.RECORD_DIR_ROOT}/{DEPENDENT}"
    prod._write(root, f"{rdir}/run.json", json.dumps({"run_id": DEPENDENT}))
    prod._write(root, f"{rdir}/halted.json", json.dumps({"status": "halted", "halted": halted}))
    prod._write(
        root, f"{rdir}/run-stage.json",
        json.dumps({"stage": "halted", "started_at": "2026-10-21T08:00:00Z", "last_activity_at": "2026-10-21T09:00:00Z"}),
    )


def _land_predecessor(root, own_baseline: str) -> dict:
    prod._author(root, PATH, "x = 1\n")
    prod._declare(root, PATH)
    prod._record_dir(root)
    _manifest(root, PREDECESSOR, own_baseline)
    return prod._land(root)


def test_unstarted_dependent_declaring_the_predecessors_path_does_not_hold_it(repo):
    base = _commit_base(repo)
    _halted_dependent(repo, base)

    out = _land_predecessor(repo, base)

    assert out.get("refused") is None, out
    assert out["committed"] is True, out
    assert PATH in prod._tree(repo)
    assert DEPENDENT not in str(out), out


def test_c595ec_shape_dependent_stays_unstarted_after_predecessor_lands(repo):
    """Mutual hold: the dependent waits on the predecessor, so the predecessor must land without it."""
    base = _commit_base(repo)
    _halted_dependent(repo, base)

    first = _land_predecessor(repo, base)

    assert first["committed"] is True, first
    assert json.loads(
        (repo / prod.RECORD_DIR_ROOT / DEPENDENT / "halted.json").read_text()
    )["halted"] == "dependents_unstarted"


def test_halted_dependent_owning_hunks_in_the_path_lands_naming_it_in_provenance(repo):
    base = _commit_base(repo)
    _halted_dependent(repo, base)

    out = _land_predecessor(repo, OTHER_BLOB)

    assert out.get("refused") is None, out
    assert out["committed"] is True, out
    provenance = [e for e in out["provenance"] if e["reason"] == peer_hunk_gate.PEER_HUNKS_CO_DECLARED]
    assert DEPENDENT in str(provenance), out


def test_halted_declarer_that_left_the_path_as_staged_does_not_hold_it(repo):
    """The 432570 shape: the tree's blob equals the declarer's baseline."""
    prod._write(repo, PATH, "x = 1\n")
    prod._git(["add", PATH], repo)
    prod._git(["commit", "-q", "-m", "base"], repo)
    base = prod._git(["rev-parse", f"HEAD:{PATH}"], repo).strip()
    _halted_dependent(repo, base, halted="ledger-check")
    prod._author(repo, PATH, "x = 1\n")
    prod._declare(repo, PATH)
    prod._record_dir(repo)
    _manifest(repo, PREDECESSOR, base)

    out = prod._land(repo)

    assert out.get("refused") is None, out
