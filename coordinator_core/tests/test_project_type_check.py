"""Verdict and seed behaviour of coordinator_core.project_type_check on tmp_path repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import project_type_check as ptc
from coordinator_core.ue_project_detection import declared_ue

pytestmark = pytest.mark.complete


def _local_md(root: Path, body: str, *, claude_dir: bool = False, eol: str = "\n") -> Path:
    path = root / (".claude/coordinator.local.md" if claude_dir else "coordinator.local.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.replace("\n", eol).encode("utf-8"))
    return path


def _uproject(root: Path, rel: str = "Game/Game.uproject") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")


def test_markers_with_no_declaration_is_seedable(tmp_path: Path) -> None:
    _uproject(tmp_path)
    res = ptc.check_root(tmp_path)
    assert res["verdict"] == "seedable"
    assert res["reason"] == "markers-undeclared"
    assert res["markers"] == ["Game/Game.uproject"]
    assert res["repair"]["writes"] == {"project_type": "game-dev", "project_subtypes": ["unreal"]}


def test_no_markers_no_declaration_is_missing(tmp_path: Path) -> None:
    res = ptc.check_root(tmp_path)
    assert (res["verdict"], res["reason"]) == ("missing", "no-markers-undeclared")
    assert res["repair"]["writes"] == {"project_type": "general"}


def test_markers_with_non_ue_declaration_is_conflict_without_repair(tmp_path: Path) -> None:
    _uproject(tmp_path)
    _local_md(tmp_path, "---\nproject_type: web-dev\n---\n")
    res = ptc.check_root(tmp_path)
    assert (res["verdict"], res["reason"]) == ("conflict", "markers-declared-non-ue")
    assert res["repair"] is None
    assert "project_subtypes" in res["hint"]


@pytest.mark.parametrize("legacy", ["meta", "web"])
def test_legacy_value_is_conflict_whatever_the_markers(tmp_path: Path, legacy: str) -> None:
    _local_md(tmp_path, f"---\nproject_type: {legacy}\n---\n")
    res = ptc.check_root(tmp_path)
    assert (res["verdict"], res["reason"]) == ("conflict", "legacy-value")
    assert res["hint"] == ptc.LEGACY_HINT
    assert res["repair"] is None
    _uproject(tmp_path)
    assert ptc.check_root(tmp_path)["reason"] == "legacy-value"


@pytest.mark.parametrize("decl", ["project_type: unreal", "project_type: unreal-engine", "project_type: game-dev\nproject_subtypes: [unreal]"])
def test_ue_declaration_is_ok_with_or_without_markers(tmp_path: Path, decl: str) -> None:
    _local_md(tmp_path, f"---\n{decl}\n---\n")
    assert ptc.check_root(tmp_path)["verdict"] == "ok"
    _uproject(tmp_path)
    assert ptc.check_root(tmp_path)["verdict"] == "ok"


def test_canonical_non_ue_declaration_without_markers_is_ok(tmp_path: Path) -> None:
    _local_md(tmp_path, "---\nproject_type: web-dev\n---\n")
    res = ptc.check_root(tmp_path)
    assert (res["verdict"], res["reason"], res["declared_ue"]) == ("ok", "consistent", False)


def test_markers_scan_is_root_plus_one_level_and_skips_tests_and_fixtures(tmp_path: Path) -> None:
    _uproject(tmp_path, "Root.uproject")
    _uproject(tmp_path, "Game/Game.uproject")
    _uproject(tmp_path, "a/b/Deep.uproject")
    _uproject(tmp_path, "tests/T.uproject")
    _uproject(tmp_path, "fixtures/F.uproject")
    assert ptc.ue_markers(tmp_path) == ["Root.uproject", "Game/Game.uproject"]


def test_only_skipped_or_deep_markers_leave_the_repo_missing(tmp_path: Path) -> None:
    _uproject(tmp_path, "tests/T.uproject")
    _uproject(tmp_path, "a/b/Deep.uproject")
    assert ptc.check_root(tmp_path)["verdict"] == "missing"


def test_apply_seeds_seedable_then_default_check_reports_ok(tmp_path: Path) -> None:
    _uproject(tmp_path)
    res = ptc.check_and_apply(tmp_path, apply=True)
    assert (res["applied"], res["after"]) == (True, "ok")
    assert declared_ue(tmp_path) is True
    again = ptc.check_root(tmp_path)
    assert again["verdict"] == "ok"
    assert (again["declared_type"], again["applied"]) == ("game-dev", False)
    assert (tmp_path / "coordinator.local.md").read_text(encoding="utf-8") == (
        "---\nproject_type: game-dev\nproject_subtypes: [unreal]\n---\n"
    )


def test_apply_seeds_missing_with_general(tmp_path: Path) -> None:
    res = ptc.check_and_apply(tmp_path, apply=True)
    assert (res["applied"], res["after"]) == (True, "ok")
    assert ptc.check_root(tmp_path)["declared_type"] == "general"


def test_without_apply_nothing_is_written(tmp_path: Path) -> None:
    _uproject(tmp_path)
    res = ptc.check_and_apply(tmp_path)
    assert res["applied"] is False
    assert not (tmp_path / "coordinator.local.md").exists()


def test_seed_keeps_other_keys_body_and_crlf_in_an_existing_file(tmp_path: Path) -> None:
    _uproject(tmp_path)
    path = _local_md(tmp_path, "---\ntest_command: pytest -q\nowner: me # keep\n---\nbody text\n", eol="\r\n")
    ptc.check_and_apply(tmp_path, apply=True)
    raw = path.read_bytes().decode("utf-8")
    assert raw == (
        "---\r\nproject_type: game-dev\r\nproject_subtypes: [unreal]\r\n"
        "test_command: pytest -q\r\nowner: me # keep\r\n---\r\nbody text\r\n"
    )
    assert ptc.check_root(tmp_path)["verdict"] == "ok"


def test_seed_prepends_frontmatter_when_the_file_has_none(tmp_path: Path) -> None:
    _uproject(tmp_path)
    path = _local_md(tmp_path, "# notes only\n")
    ptc.check_and_apply(tmp_path, apply=True)
    assert path.read_text(encoding="utf-8").endswith("---\n# notes only\n")
    assert ptc.check_root(tmp_path)["verdict"] == "ok"


def test_seed_targets_the_claude_dir_file_when_it_exists(tmp_path: Path) -> None:
    _uproject(tmp_path)
    claude = _local_md(tmp_path, "---\nowner: me\n---\n", claude_dir=True)
    ptc.check_and_apply(tmp_path, apply=True)
    assert "project_type: game-dev" in claude.read_text(encoding="utf-8")
    assert not (tmp_path / "coordinator.local.md").exists()


@pytest.mark.parametrize("body", ["---\nproject_type: web-dev\n---\n", "---\nproject_type: meta\n---\n"])
def test_apply_never_rewrites_a_conflict(tmp_path: Path, body: str) -> None:
    _uproject(tmp_path)
    path = _local_md(tmp_path, body)
    before = path.read_bytes()
    res = ptc.check_and_apply(tmp_path, apply=True)
    assert (res["verdict"], res["applied"], res["after"]) == ("conflict", False, None)
    assert path.read_bytes() == before


def test_apply_leaves_an_ok_repo_untouched(tmp_path: Path) -> None:
    path = _local_md(tmp_path, "---\nproject_type: unreal\n---\n")
    before = path.read_bytes()
    assert ptc.check_and_apply(tmp_path, apply=True)["applied"] is False
    assert path.read_bytes() == before
