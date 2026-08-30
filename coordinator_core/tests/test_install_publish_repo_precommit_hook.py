"""Characterization tests for coordinator_core.ops.install_publish_repo_precommit_hook.

Built against a golden oracle captured from the bash original, run over
a positive and negative corpus (missing arg, non-repo, mismatched repo,
fresh install, already-installed no-op, upgrade-path append, foreign-hook
offer, empty arg).

Port of: install-publish-repo-precommit-hook.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 (residual)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.install_publish_repo_precommit_hook import main
from coordinator_core.win_portability import no_console_passthrough_kwargs

# PATH lookup, not a spawn: the previous shape ran `git --version` at MODULE
# level to compute this condition, so it spawned during collection on every
# run — including --collect-only and -k-filtered ones. That is exactly the
# import-time class test_no_new_spawning_tests.py's Rule 1 bans. shutil.which
# answers the same question (is git on PATH) without a process.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(
        shutil.which("git") is None,
        reason="git not available",
    ),
]


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, **no_console_passthrough_kwargs())


def _run_main(repo_dir: Path, argv: list[str], capsys) -> tuple[int, str]:
    old_cwd = os.getcwd()
    os.chdir(repo_dir)
    try:
        rc = main(argv)
    finally:
        os.chdir(old_cwd)
    captured = capsys.readouterr()
    return rc, captured.err


def test_missing_arg_skips(tmp_path, capsys):
    repo = tmp_path / "t1"
    _init_repo(repo)
    rc, err = _run_main(repo, [], capsys)
    assert rc == 0
    assert "missing EXPECTED_REPO_ROOT argument — skipping." in err


def test_empty_arg_skips(tmp_path, capsys):
    repo = tmp_path / "t7"
    _init_repo(repo)
    rc, err = _run_main(repo, [""], capsys)
    assert rc == 0
    assert "missing EXPECTED_REPO_ROOT argument — skipping." in err


def test_not_a_git_repo_skips(tmp_path, capsys):
    not_repo = tmp_path / "t2"
    not_repo.mkdir()
    rc, err = _run_main(not_repo, [str(not_repo)], capsys)
    assert rc == 0
    assert "not in a git repo — skipping." in err


def test_mismatched_expected_root_skips(tmp_path, capsys):
    repo = tmp_path / "t3"
    _init_repo(repo)
    rc, err = _run_main(repo, ["/nonexistent/path/xyz"], capsys)
    assert rc == 0
    assert "not the expected OSS repo" in err


def test_fresh_install_writes_hook(tmp_path, capsys):
    repo = tmp_path / "t4"
    _init_repo(repo)
    rc, err = _run_main(repo, [str(repo)], capsys)
    assert rc == 0
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    assert hook_path.is_file()
    assert f"installed {hook_path}." in err
    body = hook_path.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh\n")
    assert "coordinator-oss-exec-bit-gate" in body
    assert "check-no-illegal-paths" in body
    # POSIX-only: git execs this `#!/bin/sh` hook via its own bundled `sh`
    # on every platform, so os.access(X_OK)'s Windows degrade-to-F_OK is a
    # genuine no-op there, not a defect.
    if os.name != "nt":
        assert os.access(hook_path, os.X_OK)


def test_already_installed_is_noop(tmp_path, capsys):
    repo = tmp_path / "t4b"
    _init_repo(repo)
    _run_main(repo, [str(repo)], capsys)
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    before = hook_path.read_text(encoding="utf-8")
    rc, err = _run_main(repo, [str(repo)], capsys)
    assert rc == 0
    assert "already installed" in err
    assert "no-op" in err
    assert hook_path.read_text(encoding="utf-8") == before


def test_upgrade_path_appends_paths_gate(tmp_path, capsys):
    repo = tmp_path / "t5"
    _init_repo(repo)
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(
        "#!/bin/sh\n# coordinator-oss-exec-bit-gate — stub without paths marker\necho stub\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)
    rc, err = _run_main(repo, [str(repo)], capsys)
    assert rc == 0
    assert f"appended illegal-path gate to existing {hook_path}." in err
    body = hook_path.read_text(encoding="utf-8")
    assert "echo stub" in body
    assert "check-no-illegal-paths" in body


def test_foreign_hook_is_not_overwritten(tmp_path, capsys):
    repo = tmp_path / "t6"
    _init_repo(repo)
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_body = "#!/bin/sh\necho \"some other foreign hook\"\n"
    hook_path.write_text(foreign_body, encoding="utf-8")
    hook_path.chmod(0o755)
    rc, err = _run_main(repo, [str(repo)], capsys)
    assert rc == 0
    assert "an existing pre-commit hook is in place" in err
    assert "NOT installed" in err
    assert hook_path.read_text(encoding="utf-8") == foreign_body
