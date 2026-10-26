"""Unit tests of coordinator_core.ops.fold_bar against the F11 (58f14b7d1d) verdict shape."""

from __future__ import annotations

import json

from coordinator_core.ops import fold_bar

SHA = "58f14b7d1d"
NODEIDS = [f"coordinator_core/tests/test_red_{i}.py::test_r{i}" for i in range(8)]


def _write(tmp_path, **over):
    rec = {
        "sha": SHA, "tier": "full", "failed": 0, "passed": 8545, "skipped": 19,
        "reds": [], "grant_ref": "grant-1",
    }
    rec.update(over)
    p = tmp_path / "state" / "warp" / "suite-verdicts" / f"{SHA}.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(rec), encoding="utf-8")


def test_f11_eight_reds_refused_naming_nodeids(tmp_path):
    _write(tmp_path, failed=8, passed=8537, reds=[{"nodeid": n, "first_red_commit": "abc123"} for n in NODEIDS])
    res = fold_bar.verdict(SHA, tmp_path)
    assert not res.ok
    assert set(res.nodeids) == set(NODEIDS)
    assert all(n in res.reason for n in NODEIDS)


def test_unbisected_red_refused(tmp_path):
    _write(tmp_path, failed=1, reds=[{"nodeid": NODEIDS[0]}])
    res = fold_bar.verdict(SHA, tmp_path)
    assert not res.ok and res.nodeids == (NODEIDS[0],)
    assert "first_red_commit" in res.reason


def test_absent_refused(tmp_path):
    res = fold_bar.verdict(SHA, tmp_path)
    assert not res.ok and "no recorded" in res.reason


def test_non_full_tier_refused(tmp_path):
    _write(tmp_path, tier="fast")
    assert not fold_bar.verdict(SHA, tmp_path).ok


def test_unreadable_refused(tmp_path):
    _write(tmp_path)
    (tmp_path / "state" / "warp" / "suite-verdicts" / f"{SHA}.json").write_text("{", encoding="utf-8")
    assert not fold_bar.verdict(SHA, tmp_path).ok


def test_verdict_dir_is_never_a_run_id(tmp_path):
    from pathlib import PurePosixPath

    from coordinator_core.ceremony_common.run.contract import RUN_ID_RE
    from coordinator_core.ops.dispatch_emit.run_truth_contract import SUITE_VERDICT_PATH_TEMPLATE

    segment = PurePosixPath(SUITE_VERDICT_PATH_TEMPLATE).parent.name
    assert segment == "suite-verdicts"
    assert not segment.startswith("warp-")
    assert RUN_ID_RE.fullmatch(segment) is None


def test_green_full_verdict_accepted(tmp_path):
    _write(tmp_path)
    res = fold_bar.verdict(SHA, tmp_path)
    assert res.ok and res.reason is None
