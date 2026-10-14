"""run_git picks DETACHED only for argv that provably cannot spawn a git descendant.

Pins `coordinator_core/git/run.py :: _is_leaf_argv` against the closed class list measured in
`docs/research/2026-10-14-run-git-console-flag-spike.md`, the flag run_git hands Popen for each
class, and that no console window appears for a leaf or a descendant-firing argv over a fixture
repo carrying a hook, a filter driver and a textconv driver. The Windows legs skip on POSIX:
console windows and job-object process counts exist only there.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.win_portability import leaf_spawn_creationflags, no_console_creationflags

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="console windows and job process counts exist only on Windows"
)

LEAF_ARGVS = [
    ["rev-parse", "HEAD"],
    ["-C", "somewhere", "rev-parse", "--show-toplevel"],
    ["--no-optional-locks", "rev-list", "HEAD"],
    ["merge-base", "HEAD", "HEAD"],
    ["symbolic-ref", "HEAD"],
    ["show-ref"],
    ["for-each-ref", "--format=%(refname)"],
    ["ls-tree", "HEAD"],
    ["config", "--get", "user.name"],
    ["config", "--list"],
    ["cat-file", "-p", "HEAD"],
    ["cat-file", "-t", "HEAD"],
    ["log", "-1", "--format=%H"],
    ["log", "-n", "5", "--oneline", "HEAD", "--", "a.txt"],
    ["log", "--max-count=3", "--pretty=format:%H", "--first-parent"],
]

NON_LEAF_ARGVS = [
    [],
    ["commit", "--allow-empty", "-m", "x"],
    ["ls-remote", "."],
    ["fetch"],
    ["status", "--porcelain"],
    ["ls-files"],
    ["diff", "--stat"],
    ["show", "HEAD"],
    ["diff-tree", "-p", "HEAD"],
    ["add", "."],
    ["-c", "core.fsmonitor=x", "rev-parse", "HEAD"],
    ["--git-dir=x", "rev-parse", "HEAD"],
    ["config", "user.name", "x"],
    ["symbolic-ref", "HEAD", "refs/heads/x"],
    ["symbolic-ref", "--delete", "HEAD"],
    ["config", "--edit"],
    ["config", "-e", "--get", "x"],
    ["cat-file", "--textconv", "HEAD:a"],
    ["cat-file", "--filters", "HEAD:a"],
    ["cat-file", "--batch"],
    ["cat-file", "-p", "-t", "HEAD"],
    ["log", "-p", "-1"],
    ["log", "--stat"],
    ["log", "-S", "x"],
    ["log", "--show-signature"],
    ["log", "--format", "%H"],
    ["log", "--ext-diff"],
    ["log", "-1", "--format=%G?"],
    ["log", "-1", "--pretty=format:%H %GS"],
]


@pytest.mark.parametrize("args", LEAF_ARGVS, ids=lambda a: " ".join(a))
def test_leaf_argv_is_classified_leaf(args):
    assert git_run._is_leaf_argv(args)


@pytest.mark.parametrize("args", NON_LEAF_ARGVS, ids=lambda a: " ".join(a) or "empty")
def test_non_leaf_argv_is_not_classified_leaf(args):
    assert not git_run._is_leaf_argv(args)


def test_console_flags_follow_the_classifier():
    assert git_run._console_flags(["rev-parse", "HEAD"]) == leaf_spawn_creationflags()
    assert git_run._console_flags(["commit", "-m", "x"]) == no_console_creationflags()


@_WINDOWS_ONLY
def test_run_git_hands_popen_the_classified_flag(monkeypatch, tmp_path):
    seen = []
    real_popen = subprocess.Popen

    def spy(*a, **kw):
        seen.append(kw.get("creationflags", 0))
        return real_popen(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    git_run.run_git(["--version"], cwd=str(tmp_path))
    git_run.run_git(["rev-parse", "--git-dir"], cwd=str(tmp_path))
    git_run.run_git(["status", "--porcelain"], cwd=str(tmp_path))
    leaf_flag = leaf_spawn_creationflags()["creationflags"]
    no_window = no_console_creationflags()["creationflags"]
    assert seen == [no_window, leaf_flag, no_window]


LEAF_JOB_PROCS_PER_CALL = 3.0
HOOK_TOKENS = {"pre-commit", "post-commit", "filter", "textconv", "fsmonitor"}


def _marker_tokens(marker):
    return set(marker.read_text().split()) if marker.exists() else set()


def _fixture_repo(root, marker):
    """Repo whose hooks, filter, textconv and fsmonitor each append their token to `marker`."""
    mark = marker.as_posix()

    def git(*a):
        subprocess.run(
            ["git", *a], cwd=root, check=True, capture_output=True, text=True,
            **no_console_creationflags(),
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    git("config", "gc.auto", "1")
    git("config", "filter.up.clean", f'echo filter >> "{mark}"; cat')
    git("config", "filter.up.smudge", "cat")
    git("config", "diff.tx.textconv", f'echo textconv >> "{mark}"; cat')
    (root / ".gitattributes").write_text("a.dat filter=up\nb.txt diff=tx\n")
    (root / "a.dat").write_text("one\n")
    (root / "b.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    for name in ("pre-commit", "post-commit"):
        hook = root / ".git" / "hooks" / name
        hook.write_text(f'#!/bin/sh\necho {name} >> "{mark}"\n')
    fsmonitor = root / ".git" / "fsmonitor-hook"
    fsmonitor.write_text(f'#!/bin/sh\necho fsmonitor >> "{mark}"\n')
    git("config", "core.fsmonitor", fsmonitor.as_posix())
    (root / "a.dat").write_text("two\n")
    (root / "b.txt").write_text("two\n")
    marker.write_text("")


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("leaf_repo")
    marker = root.parent / (root.name + ".marker")
    repo = root / "repo"
    repo.mkdir()
    _fixture_repo(repo, marker)
    return repo, marker


@_WINDOWS_ONLY
@pytest.mark.spawns_process
def test_no_console_window_for_leaf_argv_or_descendant_firing_argv(tmp_path):
    from coordinator_core.diagnostics.console_flash_watch import watch

    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "marker.txt"
    _fixture_repo(repo, marker)
    argvs = [
        ["rev-parse", "HEAD"],
        ["log", "-1", "--format=%H"],
        ["cat-file", "-p", "HEAD"],
        ["config", "--get", "user.name"],
        ["diff"],
        ["log", "-p", "-1"],
        ["status", "--porcelain"],
        ["commit", "-q", "--allow-empty", "-m", "fires hooks"],
    ]

    def during():
        for _ in range(3):
            for argv in argvs:
                result = git_run.run_git(argv, cwd=str(repo))
                assert result.returncode == 0, (argv, result.stderr)

    result = watch(duration_s=60.0, during=during)
    assert result.supported, "console_flash_watch unsupported: " + result.summary()
    missing = HOOK_TOKENS - _marker_tokens(marker)
    assert not missing, f"descendants never fired, zero windows would be vacuous: {sorted(missing)}"
    assert result.flash_count == 0, result.summary() + "\n" + "\n".join(map(str, result.appearances))


@_WINDOWS_ONLY
@pytest.mark.spawns_process
@pytest.mark.parametrize("args", LEAF_ARGVS, ids=lambda a: " ".join(a))
def test_leaf_argv_is_one_job_process_and_fires_no_descendant(args, fixture_repo, tmp_path):
    """The harness job holds two driver processes; 3.0 per call is the real git.exe alone, no conhost, no descendant.

    `cmd/git.exe` is a redirector that adds two processes (measured 5.0), which `run_git` avoids by
    spawning the real binary through `_git_exe`.
    """
    from coordinator_core.benchmarks.leaf_spawn_migration_verify import (
        _DRIVER_SCRIPT,
        _measure_process_time_regime,
    )

    repo, marker = fixture_repo
    argv = [git_run._git_exe(), *[str(repo) if a == "somewhere" else a for a in args]]
    flags = git_run._console_flags(args)
    assert flags == leaf_spawn_creationflags()
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER_SCRIPT, newline="\n")
    before = _marker_tokens(marker)
    _, procs = _measure_process_time_regime(
        str(driver), argv, flags["creationflags"], 4, 1, None, str(repo), 0
    )
    assert procs == [pytest.approx(LEAF_JOB_PROCS_PER_CALL, abs=0.01)], (argv, procs)
    assert _marker_tokens(marker) == before


@_WINDOWS_ONLY
@pytest.mark.spawns_process
def test_descendant_firing_argv_spawns_more_than_git_alone(fixture_repo, tmp_path):
    """Positive control: the fixture's descendants do start, so the one-process claim can fail."""
    from coordinator_core.benchmarks.leaf_spawn_migration_verify import (
        _DRIVER_SCRIPT,
        _measure_process_time_regime,
    )

    repo, marker = fixture_repo
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER_SCRIPT, newline="\n")
    for argv in (["diff"], ["status", "--porcelain"]):
        flags = git_run._console_flags(argv)
        assert flags == no_console_creationflags()
        _, procs = _measure_process_time_regime(
            str(driver), [git_run._git_exe(), *argv], flags["creationflags"], 4, 1, None, str(repo), 0
        )
        assert procs[0] > LEAF_JOB_PROCS_PER_CALL + 1, (argv, procs)
    assert {"textconv", "fsmonitor"} <= _marker_tokens(marker)
