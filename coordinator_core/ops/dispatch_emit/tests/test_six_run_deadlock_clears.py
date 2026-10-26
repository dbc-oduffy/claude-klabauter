"""Six halted, reviewed-green runs ring-withholding each other's paths all land with no waiver; a halted peer's broken residue fails nobody's invariants."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hhr
from coordinator_core.ops.dispatch_emit import halted_withheld_index, run_token
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.tests import test_halted_hold_release as fx
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

N = 6
OWN = [f"pkg/own_{i}.py" for i in range(N)]
SHARED = [f"pkg/shared_{i}.py" for i in range(N)]
PATHS = OWN + SHARED


def _run_id(i: int) -> str:
    return fx._rid("ask", 600 - i * 60, f"{i:02d}abcd")


def _halted_green_run(root, i: int, run_id: str) -> None:
    """Run i declares its own path, the path it shares with run i+1, and the one it shares with run i-1; halted, it
    withholds the first two."""
    own, shared, prev = OWN[i], SHARED[i], SHARED[(i - 1) % N]
    rdir = root / "state" / "warp" / run_id
    fx._write(rdir / "run.json", json.dumps({"run_id": run_id, "sizing_rel": f"state/sizings/s{i}.yaml"}))
    fx._write(rdir / "ask.md", "ask\n")
    fx._write(rdir / "manifest.json", json.dumps({"rows": [{"id": "C1", "writes": [own, shared, prev]}], "doc_writes": []}))
    fx._write(rdir / "halted.json", json.dumps({
        "status": "halted", "sizing_rel": f"state/sizings/s{i}.yaml", "run_artifacts": {"withheld": {own: "peer-held", shared: "peer-held"}},
    }))
    fx._write(root / f"state/sizings/s{i}.yaml", "status: open\n")
    fx._green(root, run_id)
    fx._index(root, run_id, own, shared)


def _status(root, run_id: str) -> str:
    return json.loads((root / "state" / "warp" / run_id / "halted.json").read_text(encoding="utf-8")).get("status")


def _waivers(root) -> list:
    base = root / "state" / "withheld-paths"
    return sorted(p.name for p in base.rglob("*")) if base.is_dir() else []


def test_six_ring_withheld_halted_runs_all_land_with_no_waiver(repo):  # noqa: F811
    ids = [_run_id(i) for i in range(N)]
    for i, rid in enumerate(ids):
        _halted_green_run(repo, i, rid)
    for n, path in enumerate(PATHS):
        prod._author(repo, path, f"x = {n}\n")

    landed_via_handler = []
    prod._write(repo, "scratch/reviews/r0.md", "verdict: APPROVED\n")
    reviewed = {**prod.REVIEWED, "wave_reviews": [{"verdict": "APPROVED", "sidecar_path": "scratch/reviews/r0.md"}]}
    for i, rid in enumerate(ids):
        prod._declare(repo, OWN[i], SHARED[i], SHARED[(i - 1) % N], plan=None)
        out = prod._land(repo, run_id=rid, inline_review=reviewed)
        assert out.get("refused") is None, (i, out)
        assert out["committed"] is True, (i, out)
        auto = out.get("halted_auto_landed")
        if auto is not None:
            assert auto["ok"] is True, (i, auto)
            landed_via_handler.append(rid)
            assert _status(repo, rid) == "landed", (i, _status(repo, rid), out)

    assert landed_via_handler == [ids[0]], "run 0 alone is withheld by a still-halted peer and lands through the handler"
    assert _waivers(repo) == []
    tree = prod._git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()
    assert all(p in tree for p in PATHS)


def test_a_live_holder_in_the_ring_still_refuses(repo):  # noqa: F811
    ids = [_run_id(i) for i in range(2)]
    _halted_green_run(repo, 0, ids[0])
    _halted_green_run(repo, 1, ids[1])
    (repo / "state" / "warp" / ids[1] / "halted.json").unlink()
    halted_withheld_index.remove_run(repo, ids[1])
    manifest = repo / "state" / "warp" / ids[1] / "manifest.json"
    body = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**body, "write_baseline": {SHARED[1]: None}}), encoding="utf-8")
    run_token.mint(repo, ids[1], "run-a.mjs", session_id=prod.SELF)
    for path in PATHS:
        prod._author(repo, path, "x = 0\n")
    prod._declare(repo, OWN[0], SHARED[0], SHARED[1], plan=None)
    out = prod._land(repo, run_id=ids[0])
    assert out.get("halted_auto_landed") is None, out
    assert SHARED[1] not in prod._tree(repo), out
    assert _status(repo, ids[0]) == "halted"


def test_a_halted_peers_broken_py_fails_nobody_elses_invariants(tmp_path):
    root = fx._root(tmp_path)
    me, peer = fx._auto_land_fixture(root)
    broken = "coordinator_core/halted_peer_broken.py"
    fx._index(root, peer, broken)
    node = "coordinator_core/tests/test_raw_writes_have_a_disposition.py"
    fx._write(root / node, "def test_the_live_gate_is_green():\n    pass\n")
    fx._write(root / broken, "def broken(:\n")

    assert hhr.residue_paths(root, me) >= {broken}
    assert li.invariant_findings(root, [broken], me, {}) == []
    own_broken = "coordinator_core/own_broken.py"
    assert [f.path for f in li.invariant_findings(root, [broken, own_broken], me, {})] == [node]
