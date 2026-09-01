"""Regression for `coordinator_core.wire_paths.plans_dir` — the single
fleet-wide plans-directory emitter, called by leg A instead of composing
`root / "docs" / "plans"` inline.

Spec backlink: cross-repo/archive/2026-08-08-doe-claude-em-plans-path-
emitter-one-home.md.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.wire_paths import plans_dir


def test_plans_dir_resolves_docs_plans_under_root(tmp_path: Path) -> None:
    assert plans_dir(tmp_path) == tmp_path / "docs" / "plans"


def test_workstream_complete_leg_a_calls_the_shared_emitter() -> None:
    from coordinator_core.workstream_complete import _plans_dir
    from coordinator_core.wire_paths import plans_dir as canonical

    assert _plans_dir is canonical
