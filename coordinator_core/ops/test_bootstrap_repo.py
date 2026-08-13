"""
Tests for coordinator_core.ops.bootstrap_repo.

Port of: bootstrap-repo.sh (example-doctrine-repo a1a568d2, 2026-07-22)
Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 2
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (BIG_PORT wave)

Fixture note: Stage 3 calls the natively-ported
`coordinator_core.install.scaffold_structure.scaffold_canonical_structure`
in-process (C4 retired the `scaffold-canonical-structure.sh` bridge; C11
rewired this call site onto it). Stage 4 still shells out to one genuine
Example-doctrine-repo-resident sibling this port does NOT own (`check-install-divergence.py`).
Tests stub a minimal `canonical-structure.yaml` manifest + template for stage 3
and a minimal fake for stage 4's `check-install-divergence.py`, under a
throwaway `COORDINATOR_ROOT`, so the suite is self-contained and does not
depend on the sibling example-doctrine-repo repo being checked out on the test machine.
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap

import pytest

from coordinator_core.ops.bootstrap_repo import (
    _validate_target_root_is_git_repo,
    _validate_target_root_op,
    main,
)

# Declared, not excused: this file spawns a real git process because
# `_validate_target_root_is_git_repo` under test validates a real target
# root against real git state (baseline commit, HEAD sha resolution) that no
# mock stands in for. Tests each init/commit their own throwaway repo, so
# `_init_git`/`_baseline_commit` are not hoisted to module scope -- per-test
# isolation. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_git(root: str) -> None:
    subprocess.run(["git", "-C", root, "init", "--quiet"], check=True, timeout=30)
    subprocess.run(["git", "-C", root, "config", "user.email", "test@test"], check=True, timeout=30)
    subprocess.run(["git", "-C", root, "config", "user.name", "Test"], check=True, timeout=30)
    subprocess.run(["git", "-C", root, "config", "commit.gpgsign", "false"], check=True, timeout=30)


def _baseline_commit(root: str) -> None:
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# baseline\n")
    subprocess.run(["git", "-C", root, "add", "--", "README.md"], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", root, "commit", "--quiet", "--no-verify", "-m", "chore: baseline"],
        check=True,
        timeout=30,
    )


def _head_sha(root: str) -> str:
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip()


def _commit_subject(root: str) -> str:
    proc = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        timeout=30,
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


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


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
    """Scaffold is native + advisory (C11): a missing canonical-structure.yaml
    manifest raises ScaffoldError internally, which stage 3 catches and warns
    on -- it must NOT block the rest of the bootstrap chain (unlike the retired
    bash oracle's separate scaffold-script-not-found prereq check, which was a
    stage-0 fatal precondition on the .sh file itself).

    Review: code-reviewer (Finding 1) -- must also neutralize the rung 2-4
    fallback's ambient machine state (real `~/.claude/.doe-root` + machine-local
    registry), or this test silently passes/fails depending on whether the
    executing machine happens to carry a real example-doctrine-repo checkout."""
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
    # Scaffold created nothing (manifest missing) -- nothing to stage, but the
    # chain still ran to completion rather than aborting at the failed stage.
    assert "nothing to commit" in out.out


# ---------------------------------------------------------------------------
# `_resolve_scaffold_manifest_root` — direct unit coverage of the rung ladder
#
# Review: code-reviewer (Finding 1/2) -- the two tests above only exercise this
# function indirectly and (before this diff) not hermetically. These pin the
# ladder itself with all ambient rungs neutralized by default: rung-1 hit
# (fast path, fallback never consulted), rung-1 miss -> rung-2 hit (the actual
# bug-fix path), and rung-1 miss -> all rungs miss (must stay a loud
# ScaffoldError, never a silent success -- the original bug was silence).
# ---------------------------------------------------------------------------


def test_resolve_scaffold_manifest_root_rung_one_hit_skips_fallback(tmp_path, monkeypatch):
    coordinator_root = _make_coordinator_root(tmp_path, name="rung-one-hit-coordinator")
    # Sabotage the fallback so a rung-1 hit provably never reaches it.
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
    """The case that matters most: if every rung misses, `main()` must still
    hit `ScaffoldError` (caught + reported as advisory by stage 3), never a
    silent no-manifest success. Pins against the fallback ever regressing to
    a silent default that masks a genuinely-missing manifest."""
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
    # All rungs miss -> unconditional rung-4 fallback path, which does not
    # exist on disk -- locate_manifest against it must still raise, and
    # main() must still surface that as an advisory (non-fatal) failure.
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


# ---------------------------------------------------------------------------
# Stage 1 — ensure-git
# ---------------------------------------------------------------------------


def test_non_git_non_interactive_exits_two_and_writes_nothing(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    rc = main(["--root", str(target), "--non-interactive"])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err
    assert list(target.iterdir()) == []


def test_eof_at_git_init_prompt_defaults_to_accept(tmp_path, monkeypatch):
    """Negative-spec regression: bash `${_reply:-Y}` on EOF accepts the git-init
    offer. Faithfully reproduced -- EOF must NOT silently decline here."""
    target = tmp_path / "target"
    target.mkdir()

    def _eof_input(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)
    rc = main(["--root", str(target)])  # interactive (no --non-interactive)
    assert rc == 0
    assert (target / ".git").is_dir()
    assert _commit_subject(str(target)) == "chore(coordinator): bootstrap"


# ---------------------------------------------------------------------------
# Stage 2 — assert-clean
# ---------------------------------------------------------------------------


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
    rc = main(["--root", str(target)])  # interactive
    assert rc == 3


# ---------------------------------------------------------------------------
# Full pipeline — dry-run and real commit
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Stage 4 — conflict-warn
# ---------------------------------------------------------------------------


def test_conflict_warn_gate_fires_exit_four(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))
    with open(target / "version.txt", "w", encoding="utf-8") as fh:
        fh.write("deadbeef\n")
    subprocess.run(["git", "-C", str(target), "add", "--", "version.txt"], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(target), "commit", "--quiet", "--no-verify", "-m", "chore: add version.txt"],
        check=True,
        timeout=30,
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


# ---------------------------------------------------------------------------
# Negative-spec regressions — dry-run scaffold-failure propagation, hook respect
# ---------------------------------------------------------------------------


def test_dry_run_scaffold_failure_is_advisory_not_propagated(tmp_path, monkeypatch, capsys):
    """Divergence from the retired bash oracle (documented, not a bug): the old
    oracle's `set -euo pipefail` meant a failing dry-run scaffold call aborted
    the WHOLE script with scaffold's raw subprocess exit code -- an artifact of
    shelling out through a `| sed` pipe. Stage 3 is now an in-process native
    call (C4's scaffold_canonical_structure, wired in by C11) with no
    subprocess and no raw exit code to propagate; a scaffold failure is always
    advisory (caught + logged), matching install.maximalist's Step 7 handling
    of the identical call. This test locks in the NEW contract.

    Review: code-reviewer (Finding 1) -- same ambient-state neutralization as
    test_missing_scaffold_manifest_is_advisory_not_fatal above; without it the
    rung 2-4 fallback can find a real manifest via this machine's actual
    `~/.claude/.doe-root`, making the manifest-genuinely-missing case
    untestable."""
    target = tmp_path / "target"
    target.mkdir()
    _init_git(str(target))
    _baseline_commit(str(target))

    failing_coord_root = _make_coordinator_root(
        tmp_path,
        name="failing-coordinator",
        write_manifest=False,  # no canonical-structure.yaml -> ScaffoldError, even in dry-run
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


# ---------------------------------------------------------------------------
# repo_setup.validate_target_root ("validate-target-root-is-git-repo")
# ---------------------------------------------------------------------------


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
    """AC7 — read-only check; a second call with identical input is idempotent."""
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
