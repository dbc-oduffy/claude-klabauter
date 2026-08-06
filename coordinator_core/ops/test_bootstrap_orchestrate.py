"""
Tests for coordinator_core.ops.bootstrap_orchestrate.

Port of: bootstrap-orchestrate.sh (example-doctrine-repo 0fc2b3ba, 2026-07-22)
Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 4
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (BIG_PORT wave)

These tests independently re-derive the acceptance criteria from the example-doctrine-repo-side
bash oracle test (`coordinator/bin/tests/test-bootstrap-orchestrate.sh`, ACs
AC-ABSENT / AC-DRY / AC-IDEM / AC-MISSING-REPO / AC-HOOK-FAIL / AC-DASH-PREFIX /
AC-COMMENT-STRIP) against THIS module directly, rather than re-asserting the
port's own transcription. Fixture note: the pipeline shells out to two
Example-doctrine-repo-resident siblings via `coordinator_core.ops.bootstrap_repo`
(scaffold-canonical-structure.sh, check-install-divergence.py) that this port
does NOT own. Tests stub minimal fakes of both under a throwaway
`COORDINATOR_ROOT` so the suite is self-contained and does not depend on the
sibling example-doctrine-repo repo being checked out on the test machine. The currency
stamp (`lib/coordinator-currency.sh::coordinator_currency_write`) is no
longer a example-doctrine-repo-resident dependency as of C19 — `_coordinator_currency_write`
below is a native in-package reimplementation; see its own parity tests
further down this file.
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap

import pytest

from coordinator_core.ops.bootstrap_orchestrate import (
    _coordinator_currency_write,
    main,
)


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


def _commit_count(root: str) -> int:
    proc = subprocess.run(
        ["git", "-C", root, "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return int((proc.stdout or "0").strip() or "0")


def _head_sha(root: str) -> str:
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip()


def _make_coordinator_root(tmp_path, name: str = "fake-coordinator") -> str:
    """Fake example-doctrine-repo coordinator/ tree with the siblings this port depends on:
    bin/{scaffold-canonical-structure.sh,check-install-divergence.py} (needed
    by coordinator_core.ops.bootstrap_repo, transitively) and a
    coordinator-schema-version file (read natively by
    `_coordinator_currency_write` — no lib/coordinator-currency.sh dependency
    since C19).
    """
    coord_root = tmp_path / name
    bin_dir = coord_root / "bin"
    bin_dir.mkdir(parents=True)

    scaffold = bin_dir / "scaffold-canonical-structure.sh"
    scaffold.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            ROOT=""
            DRY_RUN=0
            while [[ $# -gt 0 ]]; do
              case "$1" in
                --root) ROOT="$2"; shift 2 ;;
                --dry-run) DRY_RUN=1; shift ;;
                *) shift ;;
              esac
            done
            if [[ "$DRY_RUN" -eq 1 ]]; then
              echo "scaffold: would write state/orientation_cache.md"
              exit 0
            fi
            mkdir -p "$ROOT/state"
            echo "# scaffolded" > "$ROOT/state/orientation_cache.md"
            echo "scaffold: ok"
            """
        )
    )
    scaffold.chmod(scaffold.stat().st_mode | stat.S_IEXEC)

    divergence = bin_dir / "check-install-divergence.py"
    divergence.write_text(
        textwrap.dedent(
            """\
            import sys
            print("divergence: clean")
            sys.exit(0)
            """
        )
    )

    (coord_root / "coordinator-schema-version").write_text("7\n")

    return str(coord_root)


@pytest.fixture
def coordinator_root(tmp_path):
    return _make_coordinator_root(tmp_path)


@pytest.fixture(autouse=True)
def _set_coordinator_root(monkeypatch, coordinator_root):
    monkeypatch.setenv("COORDINATOR_ROOT", coordinator_root)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _write_working_repos_yaml(home, *repo_paths):
    yaml_path = home / ".claude" / "working-repos.yaml"
    lines = [
        "# fixture working-repos.yaml",
        "version: 1",
        "discovered_at: 2026-01-01",
        "discovery_tier: A",
        "operator: test-operator",
        "repos:",
    ]
    for r in repo_paths:
        lines.append(f"  - path: {r}")
        lines.append("    source: test")
    yaml_path.write_text("\n".join(lines) + "\n")
    return str(yaml_path)


# ---------------------------------------------------------------------------
# AC-ABSENT — working-repos.yaml absent -> exit 2
# ---------------------------------------------------------------------------


def test_absent_yaml_exits_two_with_remediation(fake_home, capsys):
    rc = main(["--non-interactive"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "/setup" in err or "setup" in err.lower()


# ---------------------------------------------------------------------------
# AC-DRY — --check-only produces action list, writes nothing
# ---------------------------------------------------------------------------


def test_check_only_produces_action_list_and_writes_nothing(tmp_path, fake_home, capsys):
    repo = tmp_path / "dry-repo"
    repo.mkdir()
    _init_git(str(repo))
    _baseline_commit(str(repo))
    before_sha = _head_sha(str(repo))

    _write_working_repos_yaml(fake_home, str(repo))

    rc = main(["--check-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "check-only" in out.lower() or "action list" in out.lower()

    assert _head_sha(str(repo)) == before_sha
    assert not (repo / "docs" / "coordinator-currency.yaml").exists()
    assert not (repo / "state").exists()


# ---------------------------------------------------------------------------
# AC-IDEM — Express re-run is a no-op
# ---------------------------------------------------------------------------


def test_express_rerun_is_idempotent(tmp_path, fake_home, capsys):
    repo = tmp_path / "idem-repo"
    repo.mkdir()
    _init_git(str(repo))
    _baseline_commit(str(repo))

    _write_working_repos_yaml(fake_home, str(repo))

    rc1 = main(["--non-interactive"])
    assert rc1 == 0
    assert (repo / "docs" / "coordinator-currency.yaml").is_file()
    count_after_first = _commit_count(str(repo))
    sha_after_first = _head_sha(str(repo))

    rc2 = main(["--non-interactive"])
    assert rc2 == 0
    assert _commit_count(str(repo)) == count_after_first
    assert _head_sha(str(repo)) == sha_after_first

    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    assert dirty == ""


# ---------------------------------------------------------------------------
# AC-MISSING-REPO — repos not on disk are skipped, not fatal
# ---------------------------------------------------------------------------


def test_missing_repo_skipped_real_repo_still_bootstrapped(tmp_path, fake_home, capsys):
    real_repo = tmp_path / "miss-real"
    real_repo.mkdir()
    _init_git(str(real_repo))
    _baseline_commit(str(real_repo))

    phantom = tmp_path / "this-repo-does-not-exist"

    _write_working_repos_yaml(fake_home, str(real_repo), str(phantom))

    rc = main(["--non-interactive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not found" in out.lower() or "missing" in out.lower() or "skip" in out.lower()
    assert _commit_count(str(real_repo)) >= 1


# ---------------------------------------------------------------------------
# AC-HOOK-FAIL — a failing pre-commit hook in one repo is surfaced as failed;
# other repos in the same Express run still succeed.
# ---------------------------------------------------------------------------


def test_failing_hook_surfaced_other_repo_still_succeeds(tmp_path, fake_home, capsys):
    good_repo = tmp_path / "hook-good"
    good_repo.mkdir()
    _init_git(str(good_repo))
    _baseline_commit(str(good_repo))

    bad_repo = tmp_path / "hook-bad"
    bad_repo.mkdir()
    _init_git(str(bad_repo))
    _baseline_commit(str(bad_repo))
    hooks_dir = bad_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("#!/bin/sh\necho 'pre-commit: BLOCKING commit' >&2\nexit 1\n")
    pre_commit.chmod(pre_commit.stat().st_mode | stat.S_IEXEC)

    _write_working_repos_yaml(fake_home, str(good_repo), str(bad_repo))

    rc = main(["--non-interactive"])
    assert rc != 0
    out = capsys.readouterr().out
    assert "failed" in out.lower() or "blocking" in out.lower()

    assert _commit_count(str(good_repo)) >= 1
    # Bad repo's bootstrap commit must not have landed past the baseline.
    assert _commit_count(str(bad_repo)) == 1


# ---------------------------------------------------------------------------
# AC-DASH-PREFIX — a leading-'-' path value is rejected (flag-injection guard);
# run reports partial success (exit 3) under --check-only.
# ---------------------------------------------------------------------------


def test_dash_prefixed_path_rejected_exit_three(tmp_path, fake_home, capsys):
    real_repo = tmp_path / "dash-real"
    real_repo.mkdir()
    _init_git(str(real_repo))
    _baseline_commit(str(real_repo))

    yaml_path = fake_home / ".claude" / "working-repos.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            f"""\
            # fixture: dash-prefix rejection
            version: 1
            discovered_at: 2026-01-01
            discovery_tier: A
            operator: test-operator
            repos:
              - path: {real_repo}
                source: test
              - path: --root /evil
                source: test
            """
        )
    )

    rc = main(["--check-only"])
    assert rc == 3
    out, err = capsys.readouterr()
    combined = out + err
    assert "rejecting repo path" in combined.lower()
    # The flag-shaped value must not leak downstream past the rejection line itself.
    non_rejection_lines = [
        line for line in combined.splitlines() if "rejecting repo path" not in line.lower()
    ]
    assert not any("--root /evil" in line for line in non_rejection_lines)


# ---------------------------------------------------------------------------
# AC-COMMENT-STRIP — whitespace-preceded inline comment stripped (space AND
# tab); embedded '#' with no leading whitespace is preserved.
# ---------------------------------------------------------------------------


def test_comment_stripping_space_and_tab(tmp_path, fake_home, capsys):
    space_repo = tmp_path / "cmt-space"
    tab_repo = tmp_path / "cmt-tab"
    space_repo.mkdir()
    tab_repo.mkdir()
    _init_git(str(space_repo))
    _init_git(str(tab_repo))

    yaml_path = fake_home / ".claude" / "working-repos.yaml"
    lines = [
        "# fixture: comment stripping",
        "version: 1",
        "discovered_at: 2026-01-01",
        "discovery_tier: A",
        "operator: test-operator",
        "repos:",
        f"  - path: {space_repo} # ZZSPACEZZ",
        "    source: test",
        f"  - path: {tab_repo}\t# ZZTABZZ",
        "    source: test",
    ]
    yaml_path.write_text("\n".join(lines) + "\n")

    rc = main(["--check-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ZZSPACEZZ" not in out
    assert "ZZTABZZ" not in out


# ---------------------------------------------------------------------------
# Embedded-'#'-with-no-leading-whitespace is a literal path character, not a
# comment (negative-spec: parity with the bash oracle's own regression fix).
# ---------------------------------------------------------------------------


def test_embedded_hash_without_leading_whitespace_preserved():
    from coordinator_core.ops.bootstrap_orchestrate import _strip_yaml_value

    assert _strip_yaml_value("/repos/a#b") == "/repos/a#b"
    assert _strip_yaml_value("/repos/a # comment") == "/repos/a"
    assert _strip_yaml_value("/repos/a\t# comment") == "/repos/a"


# ---------------------------------------------------------------------------
# Arg parsing / usage
# ---------------------------------------------------------------------------


def test_help_exits_zero_and_prints_usage(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage" in out
    assert "Exit codes:" in out


def test_unknown_flag_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    assert "unknown flag" in capsys.readouterr().err


def test_unexpected_positional_exits_one(capsys):
    rc = main(["stray-arg"])
    assert rc == 1
    assert "unexpected argument" in capsys.readouterr().err


def test_empty_repos_block_exits_one(fake_home, capsys):
    yaml_path = fake_home / ".claude" / "working-repos.yaml"
    yaml_path.write_text("version: 1\nrepos:\n")
    rc = main(["--non-interactive"])
    assert rc == 1
    assert "no repo paths found" in capsys.readouterr().err.lower()


def test_out_of_tree_paths_not_collected(tmp_path, fake_home, capsys):
    """out_of_tree: block paths are NOT bootstrapped -- negative-spec parity."""
    real_repo = tmp_path / "in-tree"
    real_repo.mkdir()
    _init_git(str(real_repo))
    _baseline_commit(str(real_repo))
    oot_repo = tmp_path / "out-of-tree-repo"

    yaml_path = fake_home / ".claude" / "working-repos.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            f"""\
            version: 1
            repos:
              - path: {real_repo}
                source: test
            out_of_tree:
              - path: {oot_repo}
                source: test
            """
        )
    )

    rc = main(["--check-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(oot_repo) not in out
    assert str(real_repo) in out


# ---------------------------------------------------------------------------
# C19 parity — _coordinator_currency_write (Port of:
# coordinator-currency.sh::coordinator_currency_write, example-doctrine-repo 9cc1d315,
# 2026-07-21). Locks the
# observable contract the retired `bash -c 'source ... && coordinator_
# currency_write ...'` bridge used to provide: idempotent write, atomic
# temp+rename, fail-loud on unreadable schema-version file.
# ---------------------------------------------------------------------------


def test_currency_write_writes_stamp(tmp_path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "coordinator-schema-version").write_text("7\n")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ok = _coordinator_currency_write(str(repo_root), str(plugin_root))
    assert ok is True

    stamp = repo_root / "docs" / "coordinator-currency.yaml"
    assert stamp.is_file()
    text = stamp.read_text(encoding="utf-8")
    assert "schema_version: 7" in text
    assert "stamped_at:" in text


def test_currency_write_idempotent_no_file_change(tmp_path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "coordinator-schema-version").write_text("7\n")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    assert _coordinator_currency_write(str(repo_root), str(plugin_root)) is True
    stamp = repo_root / "docs" / "coordinator-currency.yaml"
    first_mtime = stamp.stat().st_mtime_ns

    assert _coordinator_currency_write(str(repo_root), str(plugin_root)) is True
    assert stamp.stat().st_mtime_ns == first_mtime


def test_currency_write_refreshes_on_version_drift(tmp_path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "coordinator-schema-version").write_text("7\n")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    assert _coordinator_currency_write(str(repo_root), str(plugin_root)) is True

    (plugin_root / "coordinator-schema-version").write_text("8\n")
    assert _coordinator_currency_write(str(repo_root), str(plugin_root)) is True

    stamp = repo_root / "docs" / "coordinator-currency.yaml"
    assert "schema_version: 8" in stamp.read_text(encoding="utf-8")


def test_currency_write_missing_version_file_fails_loud(tmp_path, capsys):
    plugin_root = tmp_path / "plugin-no-version"
    plugin_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ok = _coordinator_currency_write(str(repo_root), str(plugin_root))
    assert ok is False
    assert not (repo_root / "docs" / "coordinator-currency.yaml").exists()
    err = capsys.readouterr().err
    assert "coordinator_currency_write" in err
