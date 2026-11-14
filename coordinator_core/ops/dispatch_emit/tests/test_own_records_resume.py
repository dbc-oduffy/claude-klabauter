"""own_records_resume: the landing-resume verdict from a run's own manifest, returns and kept.json."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import row_returns
from coordinator_core.ops.dispatch_emit.own_records_resume import (
    landing_resume_from_own_records,
    own_written_paths,
)

RUN = "warp-2026-11-13-demo-aaaaaa-20261113T000000-bbbbbb"


def _rd(root: Path) -> Path:
    d = root / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    return d


def _put(rd: Path, name: str, doc) -> None:
    (rd / name).write_text(json.dumps(doc), encoding="utf-8")


def _fixture(root: Path, *, returned=("C1", "C2"), kept=("C2", "C3"), verdict="WARN", tests="pass") -> Path:
    """The df88cd shape: C1 returned only, C2 returned and kept, C3 kept only."""
    rd = _rd(root)
    _put(rd, "manifest.json", {"rows": [
        {"id": "C1", "writes": ["a/one.py", "a/test_one.py"]},
        {"id": "C2", "writes": ["a/two.py"]},
        {"id": "C3", "writes": ["a/three.py"]},
    ]})
    row_returns.write(rd, {r: f"DONE: {r}" for r in returned})
    chunks = {"C2": {"paths": {"a/two.py": "x"}}, "C3": {"paths": {"a\\three.py": "y"}}}
    _put(rd, "kept.json", {"chunks": {k: v for k, v in chunks.items() if k in kept},
                           "plan_gate": None, "post_checkpoint": {"a/late.py": "z"}})
    _put(rd, "run-outcome.json", {
        "incomplete_chunks": [], "plan_path": "docs/plans/p.md",
        "inline_review": {"wave_reviews": [{"verdict": verdict}], "tests": {"status": tests}},
    })
    _put(rd, "halted.json", {"status": "resumed", "run_artifacts": {"withheld": {"a/held.py": "halt-record-only"}}})
    return rd


def test_all_rows_done_and_reviewed_yields_landing_verdict(tmp_path):
    _fixture(tmp_path)
    got = landing_resume_from_own_records(tmp_path, RUN)
    assert got is not None
    assert (got.run_id, got.plan_rel, got.rows) == (RUN, "docs/plans/p.md", ("C1", "C2", "C3"))
    assert {"a/two.py", "a/three.py", "a/late.py", "a/one.py", "a/test_one.py"} <= got.paths
    assert got.paths == own_written_paths(tmp_path, RUN)


def test_withheld_paths_are_own_paths(tmp_path):
    _fixture(tmp_path)
    assert "a/held.py" in own_written_paths(tmp_path, RUN)


def test_unreturned_unkept_row_gives_none(tmp_path):
    _fixture(tmp_path, returned=("C1",), kept=("C2",))
    assert landing_resume_from_own_records(tmp_path, RUN) is None


def test_missing_manifest_gives_none(tmp_path):
    rd = _fixture(tmp_path)
    (rd / "manifest.json").unlink()
    assert landing_resume_from_own_records(tmp_path, RUN) is None


def test_review_not_passed_gives_none_and_paths_survive(tmp_path):
    _fixture(tmp_path, verdict="FAIL")
    assert landing_resume_from_own_records(tmp_path, RUN) is None
    assert "a/two.py" in own_written_paths(tmp_path, RUN)


def test_failed_tests_or_missing_outcome_gives_none(tmp_path):
    rd = _fixture(tmp_path, tests="fail")
    assert landing_resume_from_own_records(tmp_path, RUN) is None
    _fixture(tmp_path)
    (rd / "run-outcome.json").unlink()
    assert landing_resume_from_own_records(tmp_path, RUN) is None


def test_unreturned_row_paths_are_not_own_paths(tmp_path):
    _fixture(tmp_path, returned=("C2",), kept=("C2",))
    assert "a/one.py" not in own_written_paths(tmp_path, RUN)


def test_unsafe_run_id_and_absent_dir_are_empty(tmp_path):
    assert landing_resume_from_own_records(tmp_path, "../x") is None
    assert own_written_paths(tmp_path, "../x") == frozenset()
    assert landing_resume_from_own_records(tmp_path, RUN) is None
