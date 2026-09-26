from __future__ import annotations

from pathlib import Path

from coordinator_core.wire_paths import plans_dir


def test_plans_dir_resolves_docs_plans_under_root(tmp_path: Path) -> None:
    assert plans_dir(tmp_path) == tmp_path / "docs" / "plans"


def test_workstream_complete_leg_a_calls_the_shared_emitter() -> None:
    from coordinator_core.workstream_complete import _plans_dir
    from coordinator_core.wire_paths import plans_dir as canonical

    assert _plans_dir is canonical
