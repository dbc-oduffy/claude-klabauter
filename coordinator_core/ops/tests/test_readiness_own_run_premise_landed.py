"""plan.readiness_verdict regrades a CONTRADICTED row citing a path the refired run landed or kept."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import _own_run_premise
from coordinator_core.ops.tests.test_readiness_own_run_withheld_premise import (
    REL, RUN, SIZING, _repo, _reply, _row,
)


def _run(root: Path, status: str, kept: dict | None = None, run: str = RUN) -> None:
    record = root / "state" / "warp" / run
    record.mkdir(parents=True, exist_ok=True)
    (record / "halted.json").write_text(
        json.dumps({"status": status, "sizing_rel": SIZING}), encoding="utf-8"
    )
    if kept is not None:
        (record / "kept.json").write_text(json.dumps(kept), encoding="utf-8")


def test_landed_run_kept_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _run(tmp_path, "landed", {"chunks": {"C1": {"paths": {"src/x.py": "a" * 40}}}})
    reply = _reply(common, [_row("src/x.py")])
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0


def test_resumed_run_post_checkpoint_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _run(tmp_path, "resumed", {"post_checkpoint": {"src/x.py": "a" * 40}})
    reply = _reply(common, [_row("src/x.py")])
    assert reply["verdict"] == "ready"


def test_path_the_run_did_not_write_still_pulls(tmp_path):
    common = _repo(tmp_path)
    _run(tmp_path, "landed", {"chunks": {"C1": {"paths": {"src/x.py": "a" * 40}}}})
    reply = _reply(common, [_row("src/y.py")])
    assert reply["verdict"] == "judgment" and reply["blocking_findings"] == 1


def test_own_run_paths_stamps_run_id_and_ignores_other_sizing(tmp_path):
    _run(tmp_path, "landed", {"chunks": {"C1": {"paths": {"./src/x.py": "a" * 40}}}})
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {"src/x.py": RUN}
    assert _own_run_premise.own_run_paths(tmp_path, "state/sizings/2026-10-28-other-bbbbbb.yaml") == {}
