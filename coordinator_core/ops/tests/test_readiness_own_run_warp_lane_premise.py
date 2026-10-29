"""A warp-lane run (``warp-<stem>-<stamp>``) is the own run of its sizing, so its withheld and kept paths are own work."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import _own_run_premise
from coordinator_core.ops.tests.test_readiness_own_run_withheld_premise import (
    SIZING, _repo, _reply, _row,
)

WARP_RUN = "warp-2026-10-28-fixture-aaaaaa-20261028T030000-cccccc"
ASK_RUN = "ask-20261028T010000-aaaaaa"


def _record(root: Path, run: str, status: str, withheld: list | None = None, kept: dict | None = None) -> None:
    record = root / "state" / "warp" / run
    record.mkdir(parents=True, exist_ok=True)
    marker: dict = {"status": status, "sizing_rel": SIZING}
    if withheld is not None:
        marker["run_artifacts"] = {"withheld": {p: "withheld" for p in withheld}}
    (record / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    if kept is not None:
        (record / "kept.json").write_text(json.dumps(kept), encoding="utf-8")


def test_halted_warp_run_withheld_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _record(tmp_path, WARP_RUN, "halted", withheld=["src/x.py"])
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {"src/x.py": WARP_RUN}
    reply = _reply(common, [_row("src/x.py")])
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0


def test_resumed_warp_run_kept_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _record(tmp_path, WARP_RUN, "resumed", kept={"chunks": {"C1": {"paths": {"src/x.py": "a" * 40}}}})
    reply = _reply(common, [_row("src/x.py")])
    assert reply["verdict"] == "ready"


def test_warp_run_path_it_did_not_write_still_pulls(tmp_path):
    common = _repo(tmp_path)
    _record(tmp_path, WARP_RUN, "halted", withheld=["src/x.py"])
    reply = _reply(common, [_row("src/y.py")])
    assert reply["verdict"] == "judgment" and reply["blocking_findings"] == 1


def test_newest_run_wins_across_ask_and_warp_lanes(tmp_path):
    _record(tmp_path, ASK_RUN, "landed", kept={"chunks": {"C1": {"paths": {"src/old.py": "a" * 40}}}})
    _record(tmp_path, WARP_RUN, "landed", kept={"chunks": {"C1": {"paths": {"src/new.py": "a" * 40}}}})
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {"src/new.py": WARP_RUN}


def test_warp_run_of_a_sibling_stem_is_not_own(tmp_path):
    sibling = "warp-2026-10-28-fixture-aaaaaa-slice-1-20261028T030000-cccccc"
    _record(tmp_path, sibling, "landed", kept={"chunks": {"C1": {"paths": {"src/x.py": "a" * 40}}}})
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {}
