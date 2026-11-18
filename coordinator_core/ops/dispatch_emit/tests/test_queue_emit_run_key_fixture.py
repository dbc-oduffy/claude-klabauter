"""A queue emit whose --out sits directly in --repo derives a non-empty run_key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import grind_compose
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script

from .conftest import REVIEW_KW
from .test_queue_emit import _FIXTURE_PROFILE_DIR, _base_row, _write_row


def _fixture_repo(root: Path, *, git_init: bool) -> Path:
    if git_init:
        (root / ".git").mkdir()
    queue_dir = root / "state" / "lessons"
    queue_dir.mkdir(parents=True)
    for i in range(12):
        _write_row(queue_dir / f"row{i}.yaml", **_base_row(severity="P0"))
    return queue_dir


@pytest.mark.parametrize("git_init", [True, False])
def test_out_directly_in_repo_emits_with_a_non_empty_run_key(tmp_path: Path, git_init: bool) -> None:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    queue_dir = _fixture_repo(repo, git_init=git_init)
    out = repo / "grind.workflow.mjs"
    result = _dispatch_emit(
        {
            "queue": [str(queue_dir)],
            "profile": "fixture",
            "profile_dir": str(_FIXTURE_PROFILE_DIR),
            "output_path": str(out),
        },
        repo_root=repo,
    )
    assert result["ok"] is True
    script = out.read_text(encoding="utf-8")
    assert "run_key ''" not in script
    assert repo.name in script


@pytest.mark.parametrize("git_init", [True, False])
def test_run_dir_equal_to_repo_root_emits(tmp_path: Path, git_init: bool) -> None:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    queue_dir = _fixture_repo(repo, git_init=git_init)
    emission = emit_queue_script(
        "fixture", "standard", None,
        queue=[queue_dir], profile_dir=_FIXTURE_PROFILE_DIR, repo_root=repo, run_dir=repo,
        session_id="sess1", agent_type_host=None, **REVIEW_KW,
    )
    assert emission.script


@pytest.mark.parametrize("run_dir", ["", ".", ".."])
def test_unnameable_run_dir_without_repo_root_refuses_naming_the_input(run_dir: str) -> None:
    with pytest.raises(ValueError, match="run_dir"):
        grind_compose._run_key_of(run_dir, None)


def test_run_key_of_uses_repo_name_for_dot_run_dir_and_run_dir_name_otherwise(tmp_path: Path) -> None:
    repo = tmp_path / "some-repo"
    repo.mkdir()
    assert grind_compose._run_key_of(".", repo) == "some-repo"
    assert grind_compose._run_key_of("state/queue-grind/run-1", repo) == "run-1"
    assert json.dumps(grind_compose._run_key_of("a/b", None)) == '"b"'
