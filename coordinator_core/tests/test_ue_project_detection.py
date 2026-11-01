"""Tests for coordinator_core.ue_project_detection."""

import pytest

from coordinator_core.ue_project_detection import declared_ue, is_skipped_path


@pytest.mark.parametrize(
    "p",
    [
        "tests/fixtures/x/Foo.uproject",
        "fixtures/Foo.uproject",
        "a/tests/Foo.uproject",
        "tests\\fixtures\\Foo.uproject",
    ],
)
def test_skipped(p):
    assert is_skipped_path(p) is True


@pytest.mark.parametrize(
    "p", ["Game/Game.uproject", "Foo.uproject", "contests/Foo.uproject", "Game/tests"]
)
def test_not_skipped(p):
    assert is_skipped_path(p) is False


def _decl(tmp_path, body):
    (tmp_path / "coordinator.local.md").write_text(body, encoding="utf-8")
    return declared_ue(tmp_path)


def test_missing_file_is_none(tmp_path):
    assert declared_ue(tmp_path) is None


@pytest.mark.parametrize(
    "body",
    [
        "no frontmatter here\nproject_type: unreal\n",
        "---\nproject_subtypes: []\n---\n",
        "---\nother: 1\n---\n",
        "---\nproject_type:\n---\n",
    ],
)
def test_none_cases(tmp_path, body):
    assert _decl(tmp_path, body) is None


def test_unreal_type(tmp_path):
    assert _decl(tmp_path, "---\nproject_type: unreal\n---\n") is True


def test_quoted_unreal_type(tmp_path):
    assert _decl(tmp_path, '---\nproject_type: "unreal"  # c\n---\n') is True


def test_block_subtypes(tmp_path):
    body = "---\nproject_type: game-dev\nproject_subtypes:\n  - unreal\n---\n"
    assert _decl(tmp_path, body) is True


def test_flow_subtypes(tmp_path):
    assert _decl(tmp_path, "---\nproject_subtypes: [unreal]\n---\n") is True
    assert _decl(tmp_path, "---\nproject_subtypes: [a, 'unreal']\n---\n") is True


def test_unreal_engine_type_and_subtype(tmp_path):
    assert _decl(tmp_path, "---\nproject_type: unreal-engine\n---\n") is True
    assert _decl(tmp_path, "---\nproject_subtypes: [unreal-engine]\n---\n") is True
    assert _decl(tmp_path, "---\nproject_type: unreal-engine-tools\n---\n") is False


def test_is_ue_repo_declared_or_marker(tmp_path):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    ue_route_source.clear_caches()
    declared = tmp_path / "declared"
    declared.mkdir()
    (declared / "coordinator.local.md").write_text(
        "---\nproject_type: unreal-engine\n---\n", encoding="utf-8"
    )
    marker = tmp_path / "marker"
    (marker / "Game").mkdir(parents=True)
    (marker / "Game" / "G.uproject").write_text("{}", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "coordinator.local.md").write_text(
        "---\nproject_type: general\n---\n", encoding="utf-8"
    )
    try:
        assert ue_route_source.is_ue_repo(declared) is True
        assert ue_route_source.is_ue_repo(marker) is True
        assert ue_route_source.is_ue_repo(plain) is False
    finally:
        ue_route_source.clear_caches()


def test_general_is_false(tmp_path):
    body = "---\nproject_type: general\nproject_subtypes: []\n---\n"
    assert _decl(tmp_path, body) is False


def test_block_subtypes_without_unreal_is_false(tmp_path):
    body = "---\nproject_subtypes:\n  - web\n---\n"
    assert _decl(tmp_path, body) is False
