"""ue_signal_for_writes agrees with declared_ue on split and subtype declarations."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core.ue_project_detection import declared_ue

_SPEC = importlib.util.spec_from_file_location(
    "ue_review_signal_under_test", Path(__file__).resolve().parent.parent / "ue_review_signal.py"
)
urs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(urs)


def _decl(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{body}\n---\n", encoding="utf-8")


def test_split_declaration_dot_claude_unreal_root_meta(tmp_path):
    _decl(tmp_path / ".claude" / "coordinator.local.md", "project_type: unreal-engine")
    _decl(tmp_path / "coordinator.local.md", "project_type: meta")
    assert declared_ue(tmp_path) is True
    assert urs.declares_unreal_engine(tmp_path) is True
    assert urs.ue_signal_for_writes(["src/a.py"], tmp_path) is True


def test_subtypes_unreal_declares(tmp_path):
    _decl(tmp_path / "coordinator.local.md", "project_type: meta\nproject_subtypes:\n  - unreal")
    assert urs.ue_signal_for_writes(["src/a.py"], tmp_path) is True


@pytest.mark.parametrize("body", ["project_type: meta", "project_type: web"])
def test_non_ue_declaration_stays_false(tmp_path, body):
    _decl(tmp_path / "coordinator.local.md", body)
    assert declared_ue(tmp_path) is False
    assert urs.ue_signal_for_writes(["src/a.py"], tmp_path) is False


def test_no_declaration_stays_false(tmp_path):
    assert urs.ue_signal_for_writes(["src/a.py"], tmp_path) is False
