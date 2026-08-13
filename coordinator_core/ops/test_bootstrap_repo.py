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
    _extract_failed_path_from_git_stderr,
    _git_add_batch_env,
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


# ---------------------------------------------------------------------------
# Stage 5 — commit staging: acquisition count + lock-retry (AC-7 / AC-8,
# docs/plans/2026-08-13-commit-seams-inherit-lock-reap-and-retry.md C6)
# ---------------------------------------------------------------------------


def _multi_entry_coordinator_root(tmp_path, n: int, name: str = "fake-coordinator-multi") -> str:
    """Same shape as `_make_coordinator_root`, but the manifest scaffolds
    `n` distinct files -- every entry maps to a distinct destination so
    Stage 5's staging set genuinely has `n` untracked files, not one."""
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
    """AC-7 — counted demonstration: N scaffolded files staged for the single
    bootstrap commit must cost ONE `git add` subprocess call, not N+1."""
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

    # Byte budget forces a split well before the old flat count of 500 would.
    assert len(batches) > 1
    assert sum(len(b) for b in batches) == 400
    # No round-trip data loss across the split.
    assert [p for batch in batches for p in batch] == long_paths

    base = _argv_bytes(["git", "-C", root, "add", "--"])
    for batch in batches:
        composed = base + sum(len(p.encode("utf-8")) + 1 for p in batch)
        assert composed <= _STAGE_BATCH_MAX_ARGV_BYTES


def test_batch_paths_by_byte_budget_never_drops_an_oversized_single_path():
    """A single path that alone exceeds the byte budget must still be
    attempted in its own one-path batch, never silently dropped."""
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
    """AC-8 — retry composed locally in `bootstrap_repo._git` (see its own
    docstring for the rejected `git_native` route): a transient
    `.git/index.lock` collision on the staging `add` is retried and the
    bootstrap commit still lands, rather than aborting on the first
    collision."""
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


# ---------------------------------------------------------------------------
# _extract_failed_path_from_git_stderr — Review: code-reviewer P3 (misattri-
# bution risk). Before this fix, `re.search(r"'([^']+)'", stderr) or
# re.search(r'"([^"]+)"', stderr)` matched ANY quoted substring anywhere in
# stderr and would confidently name the WRONG path when an unrelated quoted
# fragment (a hint/advice line, or a quoted token inside a different
# sentence) appeared before git's real failing-path message. These tests pin
# the fail-safe: an unrelated quoted string must never produce a named path,
# and a genuine known-template message must still extract correctly.
# ---------------------------------------------------------------------------


def test_extract_failed_path_ignores_unrelated_quoted_substring():
    """A hint/advice-shaped stderr line containing an unrelated quoted
    string, with NO known git add-failure template present, must fall back
    to None (the honest batch-scoped message) rather than misattributing
    that unrelated quote as the failing path. This is exactly the
    misattribution the prior "any quoted substring anywhere" regex was
    vulnerable to."""
    stderr = (
        "hint: Waiting for your editor to close the file... 'core.editor' is unset\n"
        "hint: see 'git help config' for more details\n"
    )
    assert _extract_failed_path_from_git_stderr(stderr) is None


def test_extract_failed_path_matches_known_pathspec_template():
    stderr = "fatal: pathspec 'state/foo/bar.md' did not match any files\n"
    assert _extract_failed_path_from_git_stderr(stderr) == "state/foo/bar.md"


def test_extract_failed_path_ignores_unrelated_quote_preceding_real_failure():
    """Even when a genuine known-template failure IS present, an unrelated
    quoted fragment earlier in stderr must not be picked up instead -- the
    anchored per-template patterns only match git's own message shape, never
    an arbitrary preceding quote."""
    stderr = (
        "hint: see 'git help config' for more details\n"
        "fatal: pathspec 'state/real-failure.md' did not match any files\n"
    )
    assert _extract_failed_path_from_git_stderr(stderr) == "state/real-failure.md"


def test_extract_failed_path_returns_none_for_no_stderr():
    assert _extract_failed_path_from_git_stderr(None) is None
    assert _extract_failed_path_from_git_stderr("") is None


def test_git_add_batch_env_pins_locale_without_dropping_ambient_env(monkeypatch):
    """`_git_add_batch_env` must force C-locale git messages (so the
    anchored English patterns above are sound) while still copying the rest
    of the ambient environment (PATH, etc.) rather than replacing it --
    dropping PATH would break the `git` subprocess spawn entirely."""
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
    env = _git_add_batch_env()
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["LANGUAGE"] == "C"
    assert env["SOME_UNRELATED_VAR"] == "keep-me"
    assert env.get("PATH") == os.environ.get("PATH")


def test_stage_five_add_failure_warning_uses_locale_pinned_extraction(tmp_path, monkeypatch, capfd):
    """End-to-end: a failed `git add` batch during Stage 5 must invoke git
    with the locale-pinned env (`_git_add_batch_env`) so that
    `_extract_failed_path_from_git_stderr`'s anchored templates are sound
    against whatever git actually emits -- pinned by asserting the captured
    `env` kwarg on the `add` subprocess call carries `LC_ALL=C`."""
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
