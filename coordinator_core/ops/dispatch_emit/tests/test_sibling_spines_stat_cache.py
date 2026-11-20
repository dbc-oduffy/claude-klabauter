"""peer_hunk_gate._sibling_spines: unchanged siblings answer from run_dir_stat_cache across calls."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_dir_stat_cache

PATH = "pkg/execute_review.py"
PLAN = "docs/plans/b.md"


@pytest.fixture(autouse=True)
def _clean_cache():
    run_dir_stat_cache.clear()
    yield
    run_dir_stat_cache.clear()


def _plan_text(*writes: str) -> str:
    body = "".join(f"    - {w}\n" for w in writes)
    return "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n" + body + "```\n"


def _sibling(root: Path, run_id: str) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/b.yaml"}), encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings" / "b.yaml").write_text(f'plan: "{PLAN}"\n', encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / PLAN).write_text(_plan_text(PATH), encoding="utf-8")


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list:
    reads: list = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self.name) or real(self, *a, **k))
    return reads


def _bump_mtime(path: Path) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


def test_two_calls_over_unchanged_siblings_parse_each_sizing_and_plan_once(tmp_path, monkeypatch) -> None:
    run_ids = ["ask-20261118T000000-aaaaaa", "ask-20261118T000001-bbbbbb"]
    for run_id in run_ids:
        _sibling(tmp_path, run_id)
    reads = _count_reads(monkeypatch)
    first = peer_hunk_gate._sibling_spines(tmp_path, run_ids)
    second = peer_hunk_gate._sibling_spines(tmp_path, run_ids)
    assert first == second == {r: {PATH} for r in run_ids}
    assert reads.count("b.yaml") == 1 and reads.count("b.md") == 1


def test_editing_the_plan_reparses_it_and_the_new_writes_are_seen(tmp_path) -> None:
    run_id = "ask-20261118T000000-aaaaaa"
    _sibling(tmp_path, run_id)
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == {PATH}
    (tmp_path / PLAN).write_text(_plan_text(PATH, "src/new.py"), encoding="utf-8")
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == {PATH, "src/new.py"}


def test_editing_the_sizing_to_name_another_plan_is_followed(tmp_path) -> None:
    run_id = "ask-20261118T000000-aaaaaa"
    _sibling(tmp_path, run_id)
    (tmp_path / "docs" / "plans" / "c.md").write_text(_plan_text("src/c.py"), encoding="utf-8")
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == {PATH}
    sizing = tmp_path / "state" / "sizings" / "b.yaml"
    sizing.write_text('plan: "docs/plans/c.md"\n', encoding="utf-8")
    _bump_mtime(sizing)
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == {"src/c.py"}


def test_a_cached_parse_exception_is_handed_to_each_call_and_cleared_by_a_fix(tmp_path) -> None:
    run_id = "ask-20261118T000000-aaaaaa"
    _sibling(tmp_path, run_id)
    (tmp_path / PLAN).write_text(
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes: [unclosed\n```\n", encoding="utf-8"
    )
    first = peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id]
    second = peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id]
    assert isinstance(first, Exception) and second is first
    (tmp_path / PLAN).write_text(_plan_text(PATH), encoding="utf-8")
    _bump_mtime(tmp_path / PLAN)
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == {PATH}


def test_a_plan_that_vanishes_after_being_cached_answers_empty(tmp_path) -> None:
    run_id = "ask-20261118T000000-aaaaaa"
    _sibling(tmp_path, run_id)
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == {PATH}
    (tmp_path / PLAN).unlink()
    assert peer_hunk_gate._sibling_spines(tmp_path, [run_id])[run_id] == set()
