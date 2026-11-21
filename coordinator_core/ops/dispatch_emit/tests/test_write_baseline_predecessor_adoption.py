"""A recovery run adopting a resumed_from predecessor's verified (kept.json) blobs is credited; anything else is not."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit import write_baseline as wb

PRED = "ask-20261027T135906-d0f621"
RECOVERY = "warp-2026-10-27-recovery-d0f621-20261027T143416-633153"


def _runs(root: Path, kept_paths: dict, *, record_kept: bool = True) -> Path:
    pred = root / "state" / "warp" / PRED
    pred.mkdir(parents=True)
    if record_kept:
        kept_work.write(pred, {"C1": {"paths": kept_paths}}, None)
    run = root / "state" / "warp" / RECOVERY
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"resumed_from": PRED}), encoding="utf-8")
    return run


def _file(root: Path, rel: str, text: str) -> str:
    (root / rel).write_text(text, encoding="utf-8")
    return kept_work.fingerprint(root, rel)


def test_exact_predecessor_blob_is_credited_not_refused(tmp_path: Path) -> None:
    sha = _file(tmp_path, "a.py", "delivered\n")
    run = _runs(tmp_path, {"a.py": sha})
    baseline = wb.capture(tmp_path, ["a.py"])
    assert wb.unwritten(tmp_path, baseline, ["a.py"]) == ["a.py"]
    assert wb.predecessor_wrote(tmp_path, run, baseline) == frozenset({"a.py"})
    assert wb.unwritten(tmp_path, wb.without_carried(tmp_path, run, baseline), ["a.py"]) == []


def test_non_matching_blob_is_still_refused(tmp_path: Path) -> None:
    _file(tmp_path, "a.py", "delivered\n")
    run = _runs(tmp_path, {"a.py": "0" * 40})
    baseline = wb.capture(tmp_path, ["a.py"])
    assert wb.predecessor_wrote(tmp_path, run, baseline) == frozenset()
    assert wb.unwritten(tmp_path, wb.without_carried(tmp_path, run, baseline), ["a.py"]) == ["a.py"]


def test_blob_edited_after_delivery_is_not_credited(tmp_path: Path) -> None:
    sha = _file(tmp_path, "a.py", "delivered\n")
    run = _runs(tmp_path, {"a.py": sha})
    _file(tmp_path, "a.py", "foreign edit\n")
    baseline = wb.capture(tmp_path, ["a.py"])
    assert wb.predecessor_wrote(tmp_path, run, baseline) == frozenset()


def test_path_the_predecessor_never_recorded_is_not_credited(tmp_path: Path) -> None:
    sha = _file(tmp_path, "a.py", "delivered\n")
    _file(tmp_path, "b.py", "foreign dirt\n")
    run = _runs(tmp_path, {"a.py": sha})
    baseline = wb.capture(tmp_path, ["a.py", "b.py"])
    assert wb.predecessor_wrote(tmp_path, run, baseline) == frozenset({"a.py"})
    assert wb.unwritten(tmp_path, wb.without_carried(tmp_path, run, baseline), ["a.py", "b.py"]) == ["b.py"]


def test_predecessor_without_record_confers_no_credit(tmp_path: Path) -> None:
    _file(tmp_path, "a.py", "delivered\n")
    run = _runs(tmp_path, {}, record_kept=False)
    baseline = wb.capture(tmp_path, ["a.py"])
    assert wb.predecessor_wrote(tmp_path, run, baseline) == frozenset()


def test_blob_kept_by_an_unlinked_run_is_not_credited(tmp_path: Path) -> None:
    sha = _file(tmp_path, "a.py", "delivered\n")
    run = _runs(tmp_path, {})
    other = tmp_path / "state" / "warp" / "ask-20261027T000000-ffffff"
    other.mkdir(parents=True)
    kept_work.write(other, {"C1": {"paths": {"a.py": sha}}}, None)
    baseline = wb.capture(tmp_path, ["a.py"])
    assert wb.predecessor_wrote(tmp_path, run, baseline) == frozenset()
