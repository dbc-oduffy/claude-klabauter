"""`run_close_commit` normalizes stage_paths it could fix itself: a directory
entry expands to its files, gitignored and emitted-workflow files are dropped,
each named in diagnostics, and the remaining real files commit.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_normalizes_stage_paths.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_commit_tail

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text("*.secret\n*.workflow.mjs\n*.emitted.json\n", encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", ".gitignore", "seed.txt", cwd=root)
    if _git("commit", "-qm", "seed", cwd=root).returncode != 0:
        pytest.skip("git unavailable")
    return root


def _close(root: Path, stage_paths):
    return directives_commit_tail.run_close_commit(
        root, session_id="test-session-normalize", subject="close", stage_paths=stage_paths
    )


def _committed_files(root: Path, sha: str) -> set:
    out = _git("show", "--name-only", "--format=", sha, cwd=root).stdout
    return {line for line in out.splitlines() if line}


def test_directory_expands_and_ignored_and_emitted_are_dropped(repo):
    d = repo / "work"
    d.mkdir()
    (d / "a.md").write_text("a\n", encoding="utf-8")
    (d / "b.md").write_text("b\n", encoding="utf-8")
    (d / "x.secret").write_text("s\n", encoding="utf-8")
    (d / "resume-script.workflow.mjs").write_text("m\n", encoding="utf-8")
    (d / "resume-script.workflow.mjs.emitted.json").write_text("{}\n", encoding="utf-8")

    result = _close(repo, ["work", "work/x.secret", "work/resume-script.workflow.mjs"])

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert _committed_files(repo, result.committed_sha) == {"work/a.md", "work/b.md"}
    diags = "\n".join(result.diagnostics)
    assert "work/x.secret" in diags
    assert "work/resume-script.workflow.mjs" in diags


def test_explicit_emitted_file_is_dropped_and_named(repo):
    (repo / "seed.txt").write_text("edited\n", encoding="utf-8")
    (repo / "run.workflow.mjs").write_text("m\n", encoding="utf-8")

    result = _close(repo, ["seed.txt", "run.workflow.mjs"])

    assert result.commit_failed is False, result.diagnostics
    assert _committed_files(repo, result.committed_sha) == {"seed.txt"}
    assert any("run.workflow.mjs" in d and "dropped" in d for d in result.diagnostics)


def test_a_plain_real_file_is_still_committed_and_nothing_is_dropped(repo):
    (repo / "seed.txt").write_text("edited\n", encoding="utf-8")

    result = _close(repo, ["seed.txt"])

    assert result.commit_failed is False
    assert _committed_files(repo, result.committed_sha) == {"seed.txt"}
    assert not any("dropped" in d for d in result.diagnostics)
