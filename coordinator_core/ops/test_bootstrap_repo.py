"""
Tests for coordinator_core.ops.bootstrap_repo.

Port of: bootstrap-repo.sh (DoE a1a568d2, 2026-07-22)
Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 2
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (BIG_PORT wave)

Fixture note: Stage 3 calls the natively-ported
`coordinator_core.install.scaffold_structure.scaffold_canonical_structure`
in-process (C4 retired the `scaffold-canonical-structure.sh` bridge; C11
rewired this call site onto it). Stage 4 still shells out to one genuine
DoE-resident sibling this port does NOT own (`check-install-divergence.py`).
Tests stub a minimal `canonical-structure.yaml` manifest + template for stage 3
and a minimal fake for stage 4's `check-install-divergence.py`, under a
throwaway `COORDINATOR_ROOT`, so the suite is self-contained and does not
depend on the sibling DoE-claude repo being checked out on the test machine.
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap

import pytest

from coordinator_core.ops.bootstrap_repo import (
    _extract_failed_path_from_git_stderr,
    _git_add_batch_env,
    _validate_target_root_is_git_repo,
    _validate_target_root_op,
    main,
)
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

# isolation. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_git(root: str) -> None:
    subprocess.run(["git", "-C", root, "init", "--quiet"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "-C", root, "config", "user.email", "test@test"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "-C", root, "config", "user.name", "Test"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "-C", root, "config", "commit.gpgsign", "false"], check=True, timeout=30, **no_console_passthrough_kwargs())


def _baseline_commit(root: str) -> None:
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# baseline\n")
    subprocess.run(["git", "-C", root, "add", "--", "README.md"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-C", root, "commit", "--quiet", "--no-verify", "-m", "chore: baseline"],
        check=True,
        timeout=30, **no_console_passthrough_kwargs(),
    )


def _head_sha(root: str) -> str:
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30, **no_console_creationflags(),
    )
    return proc.stdout.strip()


def _commit_subject(root: str) -> str:
    proc = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        timeout=30, **no_console_creationflags(),
    )
    return (proc.stdout or "").strip()


def _make_coordinator_root(
    tmp_path, manifest_body: str = "", divergence_body: str = "", name: str = "fake-coordinator",
    write_manifest: bool = True,
) -> str:
    coord_root = tmp_path / name
    bin_dir = coord_root / "bin"
    bin_dir.mkdir(parents=True)

    if write_manifest:
        (coord_root / "templates").mkdir(parents=True, exist_ok=True)
        (coord_root / "templates" / "orientation_cache.md").write_text("# scaffolded\n", encoding="utf-8")

        manifest = coord_root / "canonical-structure.yaml"
        manifest.write_text(
            manifest_body
            or textwrap.dedent(
                """\
                scaffold:
                  - path: state/orientation_cache.md
                    creation: eager
                    schema: null
                    gitkeep: false
                    readme: null
                    template: templates/orientation_cache.md
                """
            )
        )

    divergence = bin_dir / "check-install-divergence.py"
    divergence.write_text(
        divergence_body
        or textwrap.dedent(
            """\
            import sys
            print("divergence: clean")
            sys.exit(0)
            """
        )
    )
    return str(coord_root)


@pytest.fixture
def coordinator_root(tmp_path):
    return _make_coordinator_root(tmp_path)


@pytest.fixture(autouse=True)
def _set_coordinator_root(monkeypatch, coordinator_root):
    monkeypatch.setenv("COORDINATOR_ROOT", coordinator_root)


def test_help_exits_zero_and_prints_usage(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage:" in out
    assert "Exit codes:" in out


def test_root_flag_missing_value_exits_one(capsys):
    rc = main(["--root"])
    assert rc == 1
    assert "requires an argument" in capsys.readouterr().err


def test_unknown_flag_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    assert "unknown flag" in capsys.readouterr().err


def test_unexpected_positional_exits_one(capsys):
    rc = main(["stray-arg"])
    assert rc == 1
    assert "unexpected argument" in capsys.readouterr().err


def test_target_root_missing_exits_one(tmp_path, capsys):
    missing = str(tmp_path / "does-not-exist")
    rc = main(["--root", missing, "--non-interactive"])
    assert rc == 1
    assert "target root does not exist" in capsys.readouterr().err


def test_missing_scaffold_manifest_is_advisory_not_fatal(tmp_path, monkeypatch, capsys):
    empty_root = tmp_path / "empty-coordinator"
    bin_dir = empty_root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "check-install-divergence.py").write_text(
        "import sys\nprint('divergence: clean')\nsys.exit(0)\n"
    )
    monkeypatch.setenv("COORDINATOR_ROOT", str(empty_root))
    isolated_claude_home = tmp_path / "isolated-home" / ".claude"
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo._claude_home",
        lambda: str(isolated_claude_home),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo.read_doe_root_pointer_file",
        lambda home: None,
    )
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    rc = main(["--root", str(target), "--non-interactive"])
    out = capsys.readouterr()
    assert rc == 0
    assert "scaffold-canonical-structure failed" in out.err
    assert "nothing to commit" in out.out


def test_resolve_scaffold_manifest_root_rung_one_hit_skips_fallback(tmp_path, monkeypatch):
    coordinator_root = _make_coordinator_root(tmp_path, name="rung-one-hit-coordinator")
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo._content_root_rungs_2_to_4",
        lambda claude_home: (_ for _ in ()).throw(AssertionError("fallback consulted on rung-1 hit")),
    )
    from coordinator_core.ops.bootstrap_repo import _resolve_scaffold_manifest_root

    result = _resolve_scaffold_manifest_root("/unused/claude-home", coordinator_root)
    assert result == coordinator_root


def test_resolve_scaffold_manifest_root_rung_one_miss_rung_two_hit(tmp_path, monkeypatch):
    empty_root = tmp_path / "empty-coordinator"
    (empty_root / "bin").mkdir(parents=True)
    fallback_root = _make_coordinator_root(tmp_path, name="fallback-coordinator")
    doe_root = tmp_path / "doe-checkout"
    (doe_root / "coordinator").mkdir(parents=True)
    for name in os.listdir(fallback_root):
        os.replace(os.path.join(fallback_root, name), doe_root / "coordinator" / name)

    isolated_claude_home = tmp_path / "isolated-home" / ".claude"
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo.read_doe_root_pointer_file",
        lambda home: str(doe_root),
    )
    from coordinator_core.ops.bootstrap_repo import _resolve_scaffold_manifest_root

    result = _resolve_scaffold_manifest_root(str(isolated_claude_home), str(empty_root))
    assert result == str(doe_root / "coordinator")


def test_resolve_scaffold_manifest_root_all_rungs_miss_stays_loud(tmp_path, monkeypatch, capsys):
    empty_root = tmp_path / "empty-coordinator"
    (empty_root / "bin").mkdir(parents=True)
    (empty_root / "bin" / "check-install-divergence.py").write_text(
        "import sys\nprint('divergence: clean')\nsys.exit(0)\n"
    )
    isolated_claude_home = tmp_path / "isolated-home" / ".claude"
    monkeypatch.setenv("COORDINATOR_ROOT", str(empty_root))
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo._claude_home",
        lambda: str(isolated_claude_home),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo.read_doe_root_pointer_file",
        lambda home: None,
    )
    from coordinator_core.ops.bootstrap_repo import _resolve_scaffold_manifest_root

    result = _resolve_scaffold_manifest_root(str(isolated_claude_home), str(empty_root))
    assert result == os.path.join(str(isolated_claude_home), "plugins", "coordinator-claude", "coordinator")

    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))
    rc = main(["--root", str(target), "--non-interactive"])
    out = capsys.readouterr()
    assert rc == 0
    assert "scaffold-canonical-structure failed" in out.err
    assert "nothing to commit" in out.out


def test_non_git_non_interactive_exits_two_and_writes_nothing(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err
    assert list(target.iterdir()) == []


def _claude_home_env(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_claude_home_target_is_refused_before_any_stage(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True)
    _init_git(str(claude_home))
    before = sorted(p.name for p in claude_home.iterdir())
    _claude_home_env(monkeypatch, home)
    rc = main(["--root", str(claude_home), "--non-interactive"])
    assert rc == 1
    assert "resolves to Claude Home" in capsys.readouterr().err
    assert sorted(p.name for p in claude_home.iterdir()) == before


def test_claude_config_dir_is_claude_home_for_the_refusal(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "custom-claude-config"
    config_dir.mkdir()
    _init_git(str(config_dir))
    _claude_home_env(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    rc = main(["--root", str(config_dir), "--non-interactive"])
    assert rc == 1
    assert "resolves to Claude Home" in capsys.readouterr().err


def test_eof_at_git_init_prompt_defaults_to_accept(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()

    def _eof_input(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)
    rc = main(["--root", str(target)])
    assert rc == 0
    assert (target / ".git").is_dir()
    assert _commit_subject(str(target)) == "chore(coordinator): bootstrap"


def test_dirty_tree_non_interactive_exits_three(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))
    with open(target / "dirty.txt", "w", encoding="utf-8") as fh:
        fh.write("uncommitted\n")

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 3
    assert "uncommitted changes" in capsys.readouterr().out


def test_unverifiable_status_aborts_rather_than_proceeding(tmp_path, monkeypatch, capsys):
    """BEHAVIOUR CHANGE regression (2026-07-22): a git-status check that fails
    (timeout/OSError/nonzero rc) is UNKNOWN, not clean — previously
    `_git_status_porcelain` returned "" for both cases, so this gate treated
    an unverifiable tree as clean and proceeded to mutate the repo, voiding
    the 'git revert' guarantee stage 2 exists to provide."""
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo._git_status_porcelain",
        lambda root: None,
    )
    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 3
    assert "unable to verify" in capsys.readouterr().out.lower()


def test_unverifiable_status_dry_run_reports_would_be_refused(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo._git_status_porcelain",
        lambda root: None,
    )
    rc = main(["--root", str(target), "--dry-run"])
    assert rc == 0
    assert "UNABLE TO VERIFY" in capsys.readouterr().out


def test_eof_at_dirty_tree_prompt_defaults_to_decline(tmp_path, monkeypatch):
    """Negative-spec regression: bash `${_reply:-N}` on EOF declines the dirty-tree
    force-proceed prompt -- the OPPOSITE polarity of the git-init prompt's EOF
    default. Both must be reproduced, not collapsed to one behavior."""
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))
    with open(target / "dirty.txt", "w", encoding="utf-8") as fh:
        fh.write("uncommitted\n")

    def _eof_input(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)
    rc = main(["--root", str(target)])
    assert rc == 3


def test_dry_run_prints_plan_and_makes_no_commit(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))
    before_sha = _head_sha(str(target))

    rc = main(["--root", str(target), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[bootstrap-repo dry-run]" in out
    assert _head_sha(str(target)) == before_sha
    assert not (target / "state").exists()


def test_first_bootstrap_creates_commit(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 0
    assert _commit_subject(str(target)) == "chore(coordinator): bootstrap"
    assert (target / "state" / "orientation_cache.md").is_file()


def test_forced_dirty_tree_commit_does_not_absorb_unrelated_staged_file(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    with open(target / "unrelated.txt", "w", encoding="utf-8") as fh:
        fh.write("someone else's staged work\n")
    subprocess.run(
        ["git", "-C", str(target), "add", "--", "unrelated.txt"],
        check=True, timeout=30, **no_console_passthrough_kwargs(),
    )

    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    rc = main(["--root", str(target)])
    assert rc == 0
    assert _commit_subject(str(target)) == "chore(coordinator): bootstrap"

    committed_paths = subprocess.run(
        ["git", "-C", str(target), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, timeout=30, **no_console_creationflags(),
    ).stdout.splitlines()
    assert "unrelated.txt" not in committed_paths
    assert "state/orientation_cache.md" in committed_paths

    still_staged = subprocess.run(
        ["git", "-C", str(target), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, timeout=30, **no_console_creationflags(),
    ).stdout.splitlines()
    assert "unrelated.txt" in still_staged


def test_second_bootstrap_is_noop_when_nothing_to_stage(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    assert main(["--root", str(target), "--non-interactive"]) == 0
    sha_after_first = _head_sha(str(target))

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 0
    assert "nothing to commit" in capsys.readouterr().out
    assert _head_sha(str(target)) == sha_after_first


def test_conflict_warn_gate_fires_exit_four(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))
    with open(target / "version.txt", "w", encoding="utf-8") as fh:
        fh.write("deadbeef\n")
    subprocess.run(["git", "-C", str(target), "add", "--", "version.txt"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-C", str(target), "commit", "--quiet", "--no-verify", "-m", "chore: add version.txt"],
        check=True,
        timeout=30, **no_console_passthrough_kwargs(),
    )

    conflict_coord_root = _make_coordinator_root(
        tmp_path,
        name="conflict-coordinator",
        divergence_body=textwrap.dedent(
            """\
            import sys
            print("CONFLICT: state/orientation_cache.md consumer-modified")
            sys.exit(3)
            """
        ),
    )
    monkeypatch.setenv("COORDINATOR_ROOT", conflict_coord_root)

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 4


def test_dry_run_scaffold_failure_is_advisory_not_propagated(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    failing_coord_root = _make_coordinator_root(
        tmp_path,
        name="failing-coordinator",
        write_manifest=False,
    )
    monkeypatch.setenv("COORDINATOR_ROOT", failing_coord_root)
    isolated_claude_home = tmp_path / "isolated-home" / ".claude"
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo._claude_home",
        lambda: str(isolated_claude_home),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.bootstrap_repo.read_doe_root_pointer_file",
        lambda home: None,
    )

    rc = main(["--root", str(target), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "scaffold dry-run failed" in out


def test_pre_commit_hook_rejection_propagates_and_skips_completion_trailer(tmp_path, capfd):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    hooks_dir = target / ".git" / "hooks"
    hook = hooks_dir / "pre-commit"
    hook.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "pre-commit: BLOCKING commit — hook test" >&2
            exit 1
            """
        )
    )
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    before_sha = _head_sha(str(target))
    rc = main(["--root", str(target), "--non-interactive"])
    captured = capfd.readouterr()

    assert rc != 0
    assert _head_sha(str(target)) == before_sha
    assert "BLOCKING commit" in captured.err
    assert "bootstrap complete" not in captured.out


def test_validate_target_root_valid_git_repo(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))

    result = _validate_target_root_is_git_repo(str(target))

    assert result == {"valid": True, "reason": None}


def test_validate_target_root_missing_directory(tmp_path):
    missing = tmp_path / "does-not-exist"

    result = _validate_target_root_is_git_repo(str(missing))

    assert result["valid"] is False
    assert "does not exist" in result["reason"]


def test_validate_target_root_directory_not_a_git_repo(tmp_path):
    target = tmp_path / "plain-dir"
    target.mkdir()

    result = _validate_target_root_is_git_repo(str(target))

    assert result["valid"] is False
    assert "not a git repository" in result["reason"]


def test_validate_target_root_double_invocation_is_a_safe_no_op(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))

    first = _validate_target_root_is_git_repo(str(target))
    second = _validate_target_root_is_git_repo(str(target))

    assert first == second == {"valid": True, "reason": None}


def test_validate_target_root_op_handler_returns_contract_shape(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))

    result = _validate_target_root_op({"target_root": str(target)})

    assert result == {"valid": True, "reason": None}


def test_validate_target_root_op_handler_requires_target_root_param():
    with pytest.raises(ValueError):
        _validate_target_root_op({})


def _multi_entry_coordinator_root(tmp_path, n: int, name: str = "fake-coordinator-multi") -> str:
    entries = "\n".join(
        textwrap.dedent(
            f"""\
            - path: state/scaffolded-{i}.md
              creation: eager
              schema: null
              gitkeep: false
              readme: null
              template: templates/orientation_cache.md
            """
        )
        for i in range(n)
    )
    manifest_body = "scaffold:\n" + textwrap.indent(entries, "  ")
    return _make_coordinator_root(tmp_path, manifest_body=manifest_body, name=name)


def test_stage_five_batches_add_into_one_call_not_per_file(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    monkeypatch.setenv("COORDINATOR_ROOT", _multi_entry_coordinator_root(tmp_path, 5))

    import coordinator_core.ops.bootstrap_repo as bootstrap_repo

    real_run = bootstrap_repo.subprocess.run
    add_calls: list[list[str]] = []

    def _counting_run(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "git" and "add" in cmd:
            add_calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(bootstrap_repo.subprocess, "run", _counting_run)

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 0
    assert len(add_calls) == 1, (
        f"expected ONE batched `git add`, got {len(add_calls)}: {add_calls}"
    )
    for i in range(5):
        assert f"scaffolded-{i}.md" in " ".join(add_calls[0])
        assert (target / "state" / f"scaffolded-{i}.md").is_file()


def test_batch_paths_by_byte_budget_splits_on_bytes_not_count():
    """Byte-budget regression (AC-7; Review: code-reviewer P2). Uses
    realistically long paths (80+ chars, matching this repo's own
    `state/bug-backlog/<date>-<slug>-<hash>.yaml`-shaped scaffold output) --
    a short-synthetic-name test would pass under both the old flat count of
    500 and the new byte budget and prove nothing about which bound is
    actually driving the split.

    400 such paths sum to well under the old `_STAGE_BATCH_SIZE = 500`
    count (so the old code would have emitted ONE oversized `git add`), but
    comfortably exceeds `_STAGE_BATCH_MAX_ARGV_BYTES` -- proving the new
    code splits on cumulative bytes before it would ever split on count.
    """
    from coordinator_core.ops.bootstrap_repo import (
        _STAGE_BATCH_MAX_ARGV_BYTES,
        _argv_bytes,
        _batch_paths_by_byte_budget,
    )

    root = "/home/example-user/some/target/repo"
    long_paths = [
        f"state/bug-backlog/2026-08-13-index-resync-failed-archive-and-commit-{i:04d}-e4ef4012149f.yaml"
        for i in range(400)
    ]

    batches = _batch_paths_by_byte_budget(long_paths, root)

    assert len(batches) > 1
    assert sum(len(b) for b in batches) == 400
    assert [p for batch in batches for p in batch] == long_paths

    base = _argv_bytes(["git", "-C", root, "add", "--"])
    for batch in batches:
        composed = base + sum(len(p.encode("utf-8")) + 1 for p in batch)
        assert composed <= _STAGE_BATCH_MAX_ARGV_BYTES


def test_batch_paths_by_byte_budget_never_drops_an_oversized_single_path():
    from coordinator_core.ops.bootstrap_repo import (
        _STAGE_BATCH_MAX_ARGV_BYTES,
        _batch_paths_by_byte_budget,
    )

    root = "/home/example-user/some/target/repo"
    huge_path = "state/" + ("x" * (_STAGE_BATCH_MAX_ARGV_BYTES + 500)) + ".md"
    paths = ["state/short.md", huge_path, "state/also-short.md"]

    batches = _batch_paths_by_byte_budget(paths, root)

    all_paths = [p for batch in batches for p in batch]
    assert all_paths == paths
    assert any(batch == [huge_path] for batch in batches)


def test_stage_five_add_retries_through_lock_contention(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    import coordinator_core.ops.bootstrap_repo as bootstrap_repo

    real_run = bootstrap_repo.subprocess.run
    attempts = {"add": 0}

    def _flaky_run(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "git" and "add" in cmd:
            attempts["add"] += 1
            if attempts["add"] < 3:
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=128,
                    stdout="",
                    stderr=(
                        f"fatal: Unable to create '{target}/.git/index.lock': "
                        "File exists."
                    ),
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(bootstrap_repo.subprocess, "run", _flaky_run)

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 0
    assert attempts["add"] == 3, "expected exactly 2 lock-contention retries before success"
    assert _commit_subject(str(target)) == "chore(coordinator): bootstrap"
    assert (target / "state" / "orientation_cache.md").is_file()


def test_extract_failed_path_ignores_unrelated_quoted_substring():
    stderr = (
        "hint: Waiting for your editor to close the file... 'core.editor' is unset\n"
        "hint: see 'git help config' for more details\n"
    )
    assert _extract_failed_path_from_git_stderr(stderr) is None


def test_extract_failed_path_matches_known_pathspec_template():
    stderr = "fatal: pathspec 'state/foo/bar.md' did not match any files\n"
    assert _extract_failed_path_from_git_stderr(stderr) == "state/foo/bar.md"


def test_extract_failed_path_ignores_unrelated_quote_preceding_real_failure():
    stderr = (
        "hint: see 'git help config' for more details\n"
        "fatal: pathspec 'state/real-failure.md' did not match any files\n"
    )
    assert _extract_failed_path_from_git_stderr(stderr) == "state/real-failure.md"


def test_extract_failed_path_returns_none_for_no_stderr():
    assert _extract_failed_path_from_git_stderr(None) is None
    assert _extract_failed_path_from_git_stderr("") is None


def test_git_add_batch_env_pins_locale_without_dropping_ambient_env(monkeypatch):
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
    env = _git_add_batch_env()
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["LANGUAGE"] == "C"
    assert env["SOME_UNRELATED_VAR"] == "keep-me"
    assert env.get("PATH") == os.environ.get("PATH")


def test_stage_five_add_failure_warning_uses_locale_pinned_extraction(tmp_path, monkeypatch, capfd):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    import coordinator_core.ops.bootstrap_repo as bootstrap_repo

    real_run = bootstrap_repo.subprocess.run
    seen_envs = []

    def _spy_run(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "git" and "add" in cmd:
            seen_envs.append(kwargs.get("env"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(bootstrap_repo.subprocess, "run", _spy_run)

    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 0
    assert seen_envs, "expected at least one git add call to be observed"
    for env in seen_envs:
        assert env is not None
        assert env.get("LC_ALL") == "C"


def _pointer_home(tmp_path, monkeypatch, doe_root):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".doe-root").write_text(str(doe_root), encoding="utf-8")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    return str(tmp_path / "claude-home")


def test_pointer_rung_resolves_the_private_authoring_tree(tmp_path, monkeypatch):
    from coordinator_core.ops.bootstrap_repo import _content_root_rungs_2_to_4

    doe_root = tmp_path / "DoE-claude"
    (doe_root / "coordinator").mkdir(parents=True)
    claude_home = _pointer_home(tmp_path, monkeypatch, doe_root)

    assert _content_root_rungs_2_to_4(claude_home) == str(doe_root / "coordinator")


def test_pointer_rung_resolves_the_published_flat_mirror(tmp_path, monkeypatch):
    from coordinator_core.ops.bootstrap_repo import _content_root_rungs_2_to_4

    doe_root = tmp_path / "coordinator-claude"
    (doe_root / ".claude-plugin").mkdir(parents=True)
    (doe_root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    claude_home = _pointer_home(tmp_path, monkeypatch, doe_root)

    assert _content_root_rungs_2_to_4(claude_home) == str(doe_root)


def test_pointer_rung_skips_a_bare_directory_and_falls_through(tmp_path, monkeypatch):
    from coordinator_core.ops.bootstrap_repo import _content_root_rungs_2_to_4

    doe_root = tmp_path / "bare"
    doe_root.mkdir()
    claude_home = _pointer_home(tmp_path, monkeypatch, doe_root)
    monkeypatch.setattr("coordinator_core.ops.bootstrap_repo._registry_get", lambda key: None)

    assert _content_root_rungs_2_to_4(claude_home) == os.path.join(
        claude_home, "plugins", "coordinator-claude", "coordinator"
    )
