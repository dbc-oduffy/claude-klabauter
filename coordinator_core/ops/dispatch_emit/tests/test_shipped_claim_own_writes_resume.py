"""A run's own declared, executor-written paths back its shipped claims on landing; a foreign hold still refuses.

Fixture shape follows experiments run 758294 (XS, one row X1, refused `shipped-claim-unbacked` for every
path it wrote) and claude-klabauter run 8a265d (a refire whose own earlier halt recorded the paths), synthesised
under tmp_path. Measured source of the 758294 refusal: `land_run._peer_held` -> `live_run_write_claims`
reads a senior run that already landed (halted.json `landed`, a different sizing) but still holds a
fresh token and declares the same paths; the landing stamps them `peer-held` in `withhold_reason`
and `shipped_gate` judges `withheld`. The run's own halt record and halt-withheld index are not a
source: every own-halt case lands at baseline.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, land_run, run_token, shipped_claims, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    DELIVERY_NOTE_DIR,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.shipped_claims import SHIPPED_CLAIM_REFUSED
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests import test_ledger_halt_commits_delivered_rows as lh
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    repo as tc_repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = prod.RUN
HALT = "halt-record-only"
OWN = ("pkg/model.py", "pkg/receipt.py", "pkg/tests/test_model.py")
NOTE = f"{DELIVERY_NOTE_DIR}/{RUN}.md"
SIZING = "state/sizings/2026-10-24-own-writes.yaml"
OTHER_SIZING = "state/sizings/2026-10-24-other-sizing.yaml"
PRIOR = "ask-20261024T000000-bbbbbb"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000b2"
FIRST_PASS, OWN_HALT = "first-pass", "refire-after-own-halt"


@pytest.fixture(autouse=True)
def _ledger_open(monkeypatch):
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))


@pytest.fixture(autouse=True)
def _sessions_live(monkeypatch):
    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)


@pytest.fixture
def gate_map(monkeypatch):
    """The withheld map the landing hands `shipped_gate` on its last call."""
    seen: dict = {}
    real = shipped_claims.shipped_gate

    def spy(root, note, **kw):
        seen["map"] = dict(kw.get("withheld_paths") or {})
        return real(root, note, **kw)

    monkeypatch.setattr(shipped_claims, "shipped_gate", spy)
    return seen


def _stage_run(root, *, own_halt: bool) -> None:
    for p in OWN:
        prod._author(root, p, f"# {p}\n")
    prod._author(root, NOTE, "## What shipped\n\n" + "".join(f"- `{p}`\n" for p in OWN))
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="x1", paths=OWN), ChunkCommit(id="docs", title="docs", paths=(NOTE,))),
        plan_path=None,
    )
    prod._write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker)
    prod._write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    prod._write(root, prod.SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")
    if own_halt:
        prod._write(root, f"{RECORD_DIR_ROOT}/{RUN}/halted.json", json.dumps({
            "status": "halted", "halted": "commit", "sizing_rel": SIZING,
            "next_action": {"kind": "refire", "op": "engine-ask", "resume": {"run_id": RUN, "sizing": SIZING}},
            "run_artifacts": {"withheld": {p: HALT for p in OWN}},
        }))
        halted_withheld_index.add_run(root, RUN, OWN)


def _prior_run(root, *, session: str, status: str | None, sizing: str = OTHER_SIZING, paths=OWN) -> None:
    """A run other than the landing run that holds a fresh token and declares `paths`."""
    prod._write(root, f"{RECORD_DIR_ROOT}/{PRIOR}/manifest.json", json.dumps({"rows": [{"id": "P1", "writes": list(paths)}]}))
    prod._write(root, f"{RECORD_DIR_ROOT}/{PRIOR}/run.json", json.dumps({"sizing_rel": sizing}))
    if status:
        prod._write(root, f"{RECORD_DIR_ROOT}/{PRIOR}/halted.json", json.dumps({"status": status, "sizing_rel": sizing}))
    run_token.mint(root, PRIOR, "prior.mjs", session_id=session, sizing_rel=sizing)


def _own_reasons(seen: dict) -> dict:
    return {p: seen.get("map", {}).get(p) for p in OWN if p in seen.get("map", {})}


@pytest.mark.parametrize("shape", [FIRST_PASS, OWN_HALT])
def test_land_run_backs_own_paths_with_no_other_run(repo, gate_map, shape):  # noqa: F811
    _stage_run(repo, own_halt=shape == OWN_HALT)

    out = prod._land(repo)

    assert out["committed"] is True, (out, gate_map.get("map"))
    assert set(OWN) <= set(prod._tree(repo))
    assert out["shipped_claims"]["unbacked"] == []


@pytest.mark.parametrize("shape", [FIRST_PASS, OWN_HALT])
def test_land_run_backs_own_paths_a_landed_prior_run_still_declares(repo, gate_map, shape):  # noqa: F811
    """758294: the senior run 9cf334 landed (halted.json `landed`) yet holds a fresh token and declares the same paths.

    Refused `shipped-claim-unbacked` (every own path `peer-held`) until `warp_run_deliverable._live_peer_run`
    stopped holding landed runs (89b37ac00a)."""
    _stage_run(repo, own_halt=shape == OWN_HALT)
    _prior_run(repo, session=prod.SELF, status="landed")

    out = prod._land(repo)

    assert out.get("refused") != SHIPPED_CLAIM_REFUSED, (
        "withhold source: gate_withheld <- withhold_reason (land_run peer_held)", _own_reasons(gate_map), out,
    )
    assert out["committed"] is True, (out, _own_reasons(gate_map))
    assert set(OWN) <= set(prod._tree(repo))
    assert out["shipped_claims"]["unbacked"] == []


def test_land_run_does_not_stamp_peer_held_from_a_landed_prior_run(repo, gate_map):  # noqa: F811
    """The 758294 withhold source (gate_withheld <- withhold_reason peer_held) no longer reaches the gate map."""
    _stage_run(repo, own_halt=False)
    _prior_run(repo, session=prod.SELF, status="landed")

    out = prod._land(repo)

    assert "peer-held" not in set(_own_reasons(gate_map).values()), gate_map.get("map")
    assert out["shipped_claims"]["unbacked"] == []


@pytest.mark.parametrize("session", [prod.SELF, PEER_SESSION], ids=["same-session", "other-session"])
def test_land_run_still_refuses_paths_a_live_foreign_run_holds(repo, gate_map, session):  # noqa: F811
    _stage_run(repo, own_halt=False)
    _prior_run(repo, session=session, status=None)

    out = prod._land(repo)

    assert out.get("refused") == SHIPPED_CLAIM_REFUSED, (out, gate_map.get("map"))
    assert set(_own_reasons(gate_map).values()) == {"peer-held"}, gate_map["map"]


def test_land_run_still_refuses_a_path_the_em_withheld(repo, gate_map):  # noqa: F811
    _stage_run(repo, own_halt=True)

    out = prod._land(repo, withhold_paths=[{"path": OWN[0], "reason": "em ruled it out"}])

    assert out.get("refused") == SHIPPED_CLAIM_REFUSED, (out, gate_map.get("map"))
    assert OWN[0] in gate_map["map"] and gate_map["map"][OWN[0]] != HALT


def _terminal_params() -> dict:
    return {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": lh._REVIEWED,
        "session_id": lh._SID, "ledger_verdict": lh._verdict({"essential": False}), "run_id": lh.RUN,
    }


def _terminal_stage(root, *, own_halt: bool) -> None:
    lh._write(root, "a.py", "def a_tool():\n    return 1\n")
    lh._write(root, f"{DELIVERY_NOTE_DIR}/{lh.RUN}.md", "# Delivery\n\n## What shipped\n\n- `a.py`\n")
    if own_halt:
        lh._write(root, f"{RECORD_DIR_ROOT}/{lh.RUN}/halted.json", json.dumps({
            "status": "halted", "halted": "commit", "sizing_rel": lh.SIZING,
            "run_artifacts": {"withheld": {"a.py": HALT}},
        }))
        halted_withheld_index.add_run(root, lh.RUN, ["a.py"])


@pytest.mark.parametrize("shape", [FIRST_PASS, OWN_HALT])
def test_terminal_commit_backs_own_paths_with_no_other_run(tc_repo, gate_map, shape):  # noqa: F811
    _terminal_stage(tc_repo, own_halt=shape == OWN_HALT)

    out = terminal_commit._handler(_terminal_params(), repo_root=tc_repo / ".git")

    assert out["committed"] is True, (out, gate_map.get("map"))
    assert not out["shipped_claims"]["unbacked"]


def test_terminal_commit_backs_own_paths_a_landed_prior_run_still_declares(tc_repo, gate_map):  # noqa: F811
    _terminal_stage(tc_repo, own_halt=False)
    lh._write(tc_repo, f"{RECORD_DIR_ROOT}/{PRIOR}/manifest.json", json.dumps({"rows": [{"id": "P1", "writes": ["a.py"]}]}))
    lh._write(tc_repo, f"{RECORD_DIR_ROOT}/{PRIOR}/halted.json", json.dumps({"status": "landed", "sizing_rel": OTHER_SIZING}))
    run_token.mint(tc_repo, PRIOR, "prior.mjs", session_id=lh._SID, sizing_rel=OTHER_SIZING)

    out = terminal_commit._handler(_terminal_params(), repo_root=tc_repo / ".git")

    assert out.get("refused") != SHIPPED_CLAIM_REFUSED, (out, gate_map.get("map"))
    assert out["committed"] is True, out


def test_terminal_commit_still_refuses_a_path_the_em_withheld(tc_repo, gate_map):  # noqa: F811
    _terminal_stage(tc_repo, own_halt=True)
    lh._write(tc_repo, f"{RECORD_DIR_ROOT}/{lh.RUN}/withhold.jsonl", json.dumps(
        {"path": "a.py", "reason": "em ruled it out", "at": "t"}) + "\n")

    out = terminal_commit._handler(_terminal_params(), repo_root=tc_repo / ".git")

    assert out["committed"] is False, out
    assert out["refused"] == SHIPPED_CLAIM_REFUSED, (out, gate_map.get("map"))
