"""The stage manifest carries review_code_paths, the code subset of review_declared_paths."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import review_floor
from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import stage

PLAN_REL = "docs/plans/2026-10-01-fixture.md"


def _plan(writes: list[str]) -> str:
    rows = "".join(
        f"- id: C{i}\n  title: row {i}\n  change_kind: code-edit\n  surface: {w}\n  writes:\n    - {w}\n"
        for i, w in enumerate(writes, 1)
    )
    return f"---\ndeliverable_id: dlv-fixture-abc123\n---\n# Fixture\n\n## Problem\n\nx\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _stage(tmp_path, writes):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(writes), encoding="utf-8", newline="\n")
    return stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_code_run_carries_code_paths(tmp_path):
    m = _stage(tmp_path, ["pkg/a.py", "pkg/b.py"])
    assert {"pkg/a.py", "pkg/b.py"} <= set(m.review_code_paths)
    assert set(m.review_code_paths) == set(review_floor.code_paths(m.review_declared_paths))
    assert m.to_json()[review_floor.MANIFEST_KEY] == list(m.review_code_paths)


def test_docs_only_run_carries_no_code_paths(tmp_path):
    m = _stage(tmp_path, ["docs/x/notes.md"])
    assert m.review_code_paths == ()
    assert m.to_json()[review_floor.MANIFEST_KEY] == []


def test_round_trip_and_older_manifest_without_key(tmp_path):
    m = _stage(tmp_path, ["pkg/a.py"])
    assert StageManifest.from_json(m.to_json()).review_code_paths == m.review_code_paths
    older = {k: v for k, v in m.to_json().items() if k != review_floor.MANIFEST_KEY}
    assert StageManifest.from_json(older).review_code_paths == ()
