"""The operator-facing half of the `no_delta` split: two sentences, not one.

`_handler` rendered every `no_delta` path as "contributed nothing -- already
at HEAD". That is true of a path whose bytes match HEAD and FALSE of a path
declared deleted that HEAD never carried -- which is what an untracked new
file becomes once `_split_paths_for_commit_v2` misclassifies it from the
wrong cwd. The operator's brand-new file was skipped, and the warning told
them nothing was owed
(`state/audits/2026-08-31-committer-p0-root-cause-cwd-probe-becomes-deletion.md`,
signature B).

`coordinator_core/git/tests/test_declared_absent_from_head_is_split_out.py`
pins the field this reads. This module pins only what the operator sees.

Throwaway `tmp_path` repos throughout; the working repo is never touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "seed.md").write_bytes(b"seed\n")
    (repo / "held.md").write_bytes(b"held\n")
    _git(["add", "--", "seed.md", "held.md"], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _joined(result) -> str:
    return " ".join(result["warnings"])


def test_a_path_head_never_had_is_reported_as_skipped_not_as_already_at_head(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.md").write_bytes(b"moved\n")

    result = _call(
        repo,
        {
            "paths": ["seed.md"],
            "deleted_paths": ["ghost.md"],
            "message": "docs: edit",
        },
    )

    assert result["committed"] is True
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert "SKIPPED" in warning
    assert "ghost.md" in warning
    assert "already at HEAD" not in warning


def test_a_path_matching_head_still_gets_the_already_at_head_sentence(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.md").write_bytes(b"moved\n")

    result = _call(
        repo, {"paths": ["seed.md", "held.md"], "message": "docs: edit"}
    )

    assert result["committed"] is True
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert "already at HEAD" in warning
    assert "held.md" in warning
    assert "SKIPPED" not in warning


def test_both_kinds_together_are_two_warnings_each_naming_only_its_own_path(tmp_path):
    # The collapse this fix exists to undo: one sentence covering both halves
    # necessarily said "already at HEAD" about a path HEAD never had.
    repo = _repo(tmp_path)
    (repo / "extra.md").write_bytes(b"extra\n")
    _git(["add", "--", "extra.md"], repo)
    _git(["commit", "-qm", "extra"], repo)
    (repo / "extra.md").write_bytes(b"moved\n")

    result = _call(
        repo,
        {
            "paths": ["extra.md", "held.md"],
            "deleted_paths": ["ghost.md"],
            "message": "docs: edit",
        },
    )

    assert result["committed"] is True
    assert len(result["warnings"]) == 2
    matched = [w for w in result["warnings"] if "already at HEAD" in w]
    skipped = [w for w in result["warnings"] if "SKIPPED" in w]
    assert len(matched) == 1 and len(skipped) == 1
    assert "held.md" in matched[0] and "ghost.md" not in matched[0]
    assert "ghost.md" in skipped[0] and "held.md" not in skipped[0]


def test_an_ordinary_commit_warns_about_neither(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.md").write_bytes(b"moved\n")

    result = _call(repo, {"paths": ["seed.md"], "message": "docs: edit"})

    assert result["committed"] is True
    assert "already at HEAD" not in _joined(result)
    assert "SKIPPED" not in _joined(result)
