"""test_merge_recovery_and_tag_cut — pytest tests for merge-recovery-and-tag-cut.py.

Covers the two genuinely imperative cores ported from example-doctrine-repo's
merging-to-main SKILL.md: the idempotent annotated-tag cut (`cut_tag`) and the
`tag_prefix:` frontmatter parse (`resolve_tag_prefix`), including its
fail-loud quoted-value branch.

Spec backlink: coordinator/bin/merge-recovery-and-tag-cut.py module docstring.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_recovery_and_tag_cut",
        _BIN_DIR / "merge-recovery-and-tag-cut.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
cut_tag = _mod.cut_tag
resolve_tag_prefix = _mod.resolve_tag_prefix


# ---------------------------------------------------------------------------
# Helpers — minimal git repo + bare "origin" remote factory
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _init_repo_with_origin(tmp_path: Path) -> Path:
    """Bare 'origin' remote + a work clone with one commit on main, pushed."""
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)

    work = tmp_path / "work"
    work.mkdir()
    _git(["init"], cwd=work)
    _git(["config", "user.email", "test@example.com"], cwd=work)
    _git(["config", "user.name", "Test User"], cwd=work)
    _git(["checkout", "-b", "main"], cwd=work)
    (work / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "f.txt"], cwd=work)
    _git(["commit", "-m", "initial"], cwd=work)
    _git(["remote", "add", "origin", str(origin)], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)
    return work


# ---------------------------------------------------------------------------
# cut_tag
# ---------------------------------------------------------------------------

def test_cut_tag_creates_and_pushes_annotated_tag(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    head_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

    cut, merge_sha = cut_tag(work, "v1.0.0")

    assert cut is True
    assert merge_sha == head_sha

    # Tag object is annotated (not lightweight) and peels to the commit.
    tag_type = _git(["cat-file", "-t", "v1.0.0"], cwd=work).stdout.strip()
    assert tag_type == "tag"
    peeled = _git(["rev-parse", "v1.0.0^{}"], cwd=work).stdout.strip()
    assert peeled == head_sha

    # Pushed to origin, not just local.
    origin_tags = _git(["ls-remote", "--tags", "origin"], cwd=work).stdout
    assert "refs/tags/v1.0.0" in origin_tags


def test_cut_tag_is_idempotent_on_retry(tmp_path: Path) -> None:
    """Second call against an unchanged origin/main skips — no re-tag error."""
    work = _init_repo_with_origin(tmp_path)

    first_cut, first_sha = cut_tag(work, "v1.0.0")
    assert first_cut is True

    second_cut, second_sha = cut_tag(work, "v1.0.0")
    assert second_cut is False
    assert second_sha == first_sha


# ---------------------------------------------------------------------------
# resolve_tag_prefix
# ---------------------------------------------------------------------------

def test_resolve_tag_prefix_extracts_value(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "project_type: game-dev\n"
        "tag_prefix: example-game-repo-\n"
        "---\n"
        "# Body\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == "example-game-repo-"


def test_resolve_tag_prefix_strips_inline_comment(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "tag_prefix: example-game-repo-  # namespace prefix\n"
        "---\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == "example-game-repo-"


def test_resolve_tag_prefix_absent_key_returns_empty(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "project_type: general\n"
        "---\n"
        "tag_prefix: should-not-be-seen\n",  # outside frontmatter — must be ignored
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == ""


def test_resolve_tag_prefix_quoted_value_fails_loud(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        'tag_prefix: "example-game-repo-"\n'
        "---\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc_info:
        resolve_tag_prefix(config)
    assert exc_info.value.code == 1
