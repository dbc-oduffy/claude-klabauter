"""closing_bases follows a ledger's resumed_from chain so a resume's close names its predecessors' bases."""

import json

from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases
from coordinator_core.ops.dispatch_emit.requirements_ledger import CARRIED_HOP_CAP

OWN = "a" * 40
PRED = "b" * 40
SUBM = "c" * 40
GRAND = "d" * 40


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _run(root, rid, head=None, resumed_from=None):
    if head:
        _write(root / "state/warp" / rid / "manifest.json", {"head_sha": head})
    run = {"sizing_rel": None, "all_of_pm_ask": False}
    if resumed_from:
        run["resumed_from"] = resumed_from
    _write(root / "state/warp" / rid / "run.json", run)


def test_resume_closes_predecessor_bases(tmp_path):
    _run(tmp_path, "pred", head=PRED)
    _write(tmp_path / "scratch/warp/pred/submission.json", {"run_base_sha": SUBM})
    _run(tmp_path, "res", head=OWN, resumed_from="pred")
    assert closing_bases(tmp_path, "res.workflow", {}) == (OWN, PRED, SUBM)


def test_no_resumed_from_is_unchanged(tmp_path):
    _run(tmp_path, "pred", head=PRED)
    _run(tmp_path, "res", head=OWN)
    assert closing_bases(tmp_path, "res", {}) == (OWN,)


def test_chain_followed_and_cycle_safe(tmp_path):
    _run(tmp_path, "g", head=GRAND, resumed_from="res")
    _run(tmp_path, "pred", head=PRED, resumed_from="g")
    _run(tmp_path, "res", head=OWN, resumed_from="pred")
    assert closing_bases(tmp_path, "res", {}) == (OWN, PRED, GRAND)


def test_chain_bounded_by_hop_cap(tmp_path):
    n = CARRIED_HOP_CAP + 3
    for i in range(n):
        _run(tmp_path, f"r{i}", head=f"{i:040x}", resumed_from=f"r{i + 1}" if i + 1 < n else None)
    got = closing_bases(tmp_path, "r0", {})
    assert len(got) == CARRIED_HOP_CAP + 1


def test_missing_predecessor_record_is_skipped(tmp_path):
    _run(tmp_path, "res", head=OWN, resumed_from="gone")
    assert closing_bases(tmp_path, "res", {"review_run_base": SUBM}) == (OWN, SUBM)
