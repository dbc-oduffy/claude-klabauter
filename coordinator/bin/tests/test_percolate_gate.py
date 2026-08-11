"""test_percolate_gate — pytest tests for coordinator/bin/percolate-gate.py.

Covers the three ported gate-logic subcommands (branch0-gate, scan-secrets,
inverse-drift) against the contract documented in percolate-gate.py's module
docstring, itself a direct port of the fences in example-doctrine-repo
coordinator/skills/percolate/SKILL.md (Branch 0, Step 2c, Step 2d). The
former `run-pre-ci-hooks` subcommand (Step 5a) was removed 2026-07-24 once
the declarative engine-side pre-ci guard (`publish.py`'s
`dispatch_percolate_pre_ci`) reached parity — see docs/plans/2026-07-24-
extirpate-orphaned-claude-central-publish-shell.md.

Run: python -m pytest coordinator/bin/tests/test_percolate_gate.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_gate", _BIN_DIR / "percolate-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _run_cli(args: list[str]):
    """Invoke main() in-process, capturing stdout/exit code (mirrors the
    check-*.py test convention of exercising the module's own main())."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod.main(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# branch0-gate
# ---------------------------------------------------------------------------

def _make_percolate_root(tmp_path: Path, target: str, with_hooks: bool = True) -> tuple[Path, Path]:
    percolate_root = tmp_path / "percolate-root"
    setup_dir = percolate_root / "setup"
    setup_dir.mkdir(parents=True)

    source_dir = tmp_path / "source" / target
    source_dir.mkdir(parents=True)
    (source_dir / ".percolate-ignore").write_text("", encoding="utf-8")

    dest_dir = tmp_path / "dest" / target
    dest_dir.mkdir(parents=True)

    (setup_dir / "publish-targets.portable").write_text(
        f"{target}|mirror|{source_dir}|{dest_dir}\n", encoding="utf-8"
    )

    if with_hooks:
        for hook_point in ("pre-rsync", "post-rsync", "pre-ci"):
            (setup_dir / "percolate-hooks" / target / hook_point).mkdir(parents=True)

    return percolate_root, source_dir


def test_branch0_gate_configured(tmp_path):
    percolate_root, source_dir = _make_percolate_root(tmp_path, "alpha")
    rc, out = _run_cli(
        ["branch0-gate", "alpha", "--percolate-root", str(percolate_root)]
    )
    assert rc == 0
    assert out.strip() == f"CONFIGURED:{source_dir}"


def test_branch0_gate_missing_target_entry(tmp_path):
    percolate_root, _ = _make_percolate_root(tmp_path, "alpha")
    rc, out = _run_cli(
        ["branch0-gate", "not-registered", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert "MISSING_TARGET_ENTRY" in out


def test_branch0_gate_configured_with_hook_dirs_absent(tmp_path):
    """Regression (2026-07-24, extirpate-orphaned-claude-central-publish-shell
    chunk C1): the per-target pre-rsync/post-rsync/pre-ci hook subdirectories
    are vestigial now that the percolate engine consumes the declarative
    `percolate-store.yaml` instead. branch0-gate must return CONFIGURED for a
    valid target whose hook subdirs are absent -- only `publish-targets.portable`
    and `.percolate-ignore` are required."""
    percolate_root, source_dir = _make_percolate_root(tmp_path, "alpha", with_hooks=False)
    rc, out = _run_cli(
        ["branch0-gate", "alpha", "--percolate-root", str(percolate_root)]
    )
    assert rc == 0
    assert out.strip() == f"CONFIGURED:{source_dir}"
    assert "MISSING_HOOK_DIR" not in out


def test_branch0_gate_missing_ignore_file(tmp_path):
    percolate_root, source_dir = _make_percolate_root(tmp_path, "alpha")
    (source_dir / ".percolate-ignore").unlink()
    rc, out = _run_cli(
        ["branch0-gate", "alpha", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert "MISSING_IGNORE" in out


# ---------------------------------------------------------------------------
# scan-secrets
# ---------------------------------------------------------------------------

def test_scan_secrets_high_hit_blocks(tmp_path):
    target_file = tmp_path / "leaky.md"
    target_file.write_text(
        "here is a token: sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8"  # noqa: secrets
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 2
    assert "HIGH" in out
    assert "sk-a..." in out
    # The full secret must not appear verbatim in the redacted panel.
    assert "sk-abcdefghijklmnopqrstuvwx" not in out  # noqa: secrets


def test_scan_secrets_medium_hit_does_not_block(tmp_path):
    target_file = tmp_path / "wiki.md"
    target_file.write_text(
        "See ~/.claude/tasks/foo for details.\n", encoding="utf-8"
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert "MEDIUM" in out
    assert "~/.claude/tasks/foo" in out
    assert "HIGH" in out and "(none)" in out


def test_scan_secrets_clean(tmp_path):
    target_file = tmp_path / "clean.md"
    target_file.write_text("nothing sensitive here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert out.count("(none)") == 3


def test_scan_secrets_peer_repo_extension(tmp_path):
    target_file = tmp_path / "mentions.md"
    target_file.write_text("cross-reference example-retrieval-repo here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    registry = tmp_path / "repo-registry.md"
    registry.write_text(
        "- shortname: example-retrieval-repo\n  path: /x/example-retrieval-repo\n"
        "- shortname: claude-klabauter\n  path: /x/claude-klabauter\n",
        encoding="utf-8",
    )

    rc, out = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--peer-repos-file",
            str(registry),
            "--target",
            "coordinator-claude",
        ]
    )
    assert rc == 0
    assert "example-retrieval-repo" in out.split("MEDIUM")[1]


# ---------------------------------------------------------------------------
# inverse-drift
# ---------------------------------------------------------------------------

def _init_dest_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "dest"
    dest.mkdir()
    subprocess.run(["git", "init", str(dest)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    return dest


def test_inverse_drift_marker_mode_detects_commit(tmp_path):
    dest = _init_dest_repo(tmp_path)
    tracked = dest / "file.md"
    tracked.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "file.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "initial"], check=True, capture_output=True
    )
    marker_sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    tracked.write_text("v2 hand-fixed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "file.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "dest-side hand fix"],
        check=True,
        capture_output=True,
    )

    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)
    (percolate_root / "setup" / "percolate-state" / "alpha.lastsync").write_text(
        marker_sha, encoding="utf-8"
    )

    file_list = tmp_path / "files.txt"
    file_list.write_text(str(tracked) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        [
            "inverse-drift",
            "alpha",
            "--percolate-root",
            str(percolate_root),
            "--dest",
            str(dest),
            "--files",
            str(file_list),
        ]
    )
    assert rc == 0
    assert "anchor_mode: marker" in out
    assert "dest-side hand fix" in out


def test_inverse_drift_marker_stale_falls_back(tmp_path):
    dest = _init_dest_repo(tmp_path)
    tracked = dest / "file.md"
    tracked.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "file.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "initial"], check=True, capture_output=True
    )

    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)
    # A genuinely malformed/unresolvable marker (not a full 40-hex SHA) —
    # `git rev-parse --verify` treats a syntactically valid 40-hex string as
    # verified without checking object-database presence (a git quirk the
    # bash oracle's `git rev-parse --verify "$since_ref"` shares faithfully),
    # so the stale case needs a marker git genuinely can't resolve at all.
    (percolate_root / "setup" / "percolate-state" / "alpha.lastsync").write_text(
        "not-a-real-ref-anywhere", encoding="utf-8"
    )

    file_list = tmp_path / "files.txt"
    file_list.write_text(str(tracked) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        [
            "inverse-drift",
            "alpha",
            "--percolate-root",
            str(percolate_root),
            "--dest",
            str(dest),
            "--files",
            str(file_list),
        ]
    )
    assert rc == 0
    assert "anchor_mode: marker-stale" in out


def test_inverse_drift_no_marker_30day_fallback_no_hits(tmp_path):
    dest = _init_dest_repo(tmp_path)
    tracked = dest / "file.md"
    tracked.write_text("v1\n", encoding="utf-8")

    percolate_root = tmp_path / "percolate-root"
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(tracked) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        [
            "inverse-drift",
            "alpha",
            "--percolate-root",
            str(percolate_root),
            "--dest",
            str(dest),
            "--files",
            str(file_list),
        ]
    )
    assert rc == 0
    assert "anchor_mode: 30day-fallback" in out
    assert "Inverse drift" not in out


# ---------------------------------------------------------------------------
# list-targets
# ---------------------------------------------------------------------------

def _make_multi_target_percolate_root(tmp_path: Path) -> Path:
    percolate_root = tmp_path / "percolate-root"
    setup_dir = percolate_root / "setup"
    setup_dir.mkdir(parents=True)

    rows = []
    for name in ("alpha", "beta", "gamma"):
        source_dir = tmp_path / "source" / name
        source_dir.mkdir(parents=True)
        dest_dir = tmp_path / "dest" / name
        dest_dir.mkdir(parents=True)
        rows.append(f"{name}|mirror|{source_dir}|{dest_dir}")

    (setup_dir / "publish-targets.portable").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return percolate_root


def test_list_targets_no_filter_lists_all_names_in_order(tmp_path):
    percolate_root = _make_multi_target_percolate_root(tmp_path)

    rc, out = _run_cli(["list-targets", "--percolate-root", str(percolate_root)])

    assert rc == 0
    assert out.strip().splitlines() == ["alpha", "beta", "gamma"]


def test_list_targets_with_target_prints_only_dest_path(tmp_path):
    percolate_root = _make_multi_target_percolate_root(tmp_path)
    expected_dest = tmp_path / "dest" / "beta"

    rc, out = _run_cli(
        ["list-targets", "--percolate-root", str(percolate_root), "--target", "beta"]
    )

    assert rc == 0
    assert out.strip() == str(expected_dest)


def test_list_targets_unknown_target_exits_nonzero_no_stdout(tmp_path):
    percolate_root = _make_multi_target_percolate_root(tmp_path)

    rc, out = _run_cli(
        ["list-targets", "--percolate-root", str(percolate_root), "--target", "not-registered"]
    )

    assert rc == 1
    assert out == ""


def test_list_targets_no_targets_registered_errors_to_stderr(tmp_path):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = _mod.main(["list-targets", "--percolate-root", str(percolate_root)])

    assert rc == 1
    assert out_buf.getvalue() == ""
    assert err_buf.getvalue() != ""


# ---------------------------------------------------------------------------
# resolve-root
# ---------------------------------------------------------------------------


def test_resolve_root_bare_prints_path_only(monkeypatch, tmp_path):
    resolved = tmp_path / "some-root"
    resolved.mkdir()

    monkeypatch.setattr(
        "coordinator_core.percolate.runtime_root.coordinator_percolate_runtime_root_explained",
        lambda: (str(resolved), "repo-local-git"),
    )

    rc, out, err = _run_cli_capturing_stderr(["resolve-root"])
    assert rc == 0
    assert out.strip() == str(resolved)
    assert err == ""


def test_resolve_root_explain_prints_path_and_rung(monkeypatch, tmp_path):
    resolved = tmp_path / "some-root"
    resolved.mkdir()

    monkeypatch.setattr(
        "coordinator_core.percolate.runtime_root.coordinator_percolate_runtime_root_explained",
        lambda: (str(resolved), "doe-root-pointer"),
    )

    rc, out, err = _run_cli_capturing_stderr(["resolve-root", "--explain"])
    assert rc == 0
    assert out.strip() == f"{resolved}\tdoe-root-pointer"
    assert err == ""


def test_resolve_root_ladder_failure_writes_stderr_verbatim_no_stdout(monkeypatch):
    message = "coordinator_percolate_runtime_root: cannot resolve PERCOLATE_ROOT.\n  (details)"

    def _raise():
        raise RuntimeError(message)

    monkeypatch.setattr(
        "coordinator_core.percolate.runtime_root.coordinator_percolate_runtime_root_explained",
        _raise,
    )

    rc, out, err = _run_cli_capturing_stderr(["resolve-root"])
    assert rc == 1
    assert out == ""
    assert err.strip() == message


# ---------------------------------------------------------------------------
# Step 2d — pathspec batching and source-path mapping
#
# Both regressions here were live defects, found 2026-08-11 while running the
# klabauter republish: the check crashed on Windows for any target with a few
# hundred files, and — once it stopped crashing — matched nothing at all,
# because the file list it is handed is built from SOURCE paths.
# ---------------------------------------------------------------------------


def _run_cli_capturing_stderr(args: list[str]):
    """_run_cli captures stdout only; Step 2d's fail-loud path writes stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _mod.main(args)
    return rc, out.getvalue(), err.getvalue()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def drift_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "dest"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_git_log_batched_survives_a_pathspec_set_over_the_windows_cmdline_cap(
    drift_repo: Path,
) -> None:
    """>32767 chars of pathspec must not raise WinError 206."""
    names = [f"file_{i:04d}_{'p' * 60}.py" for i in range(600)]
    for name in names:
        (drift_repo / name).write_text("x\n", encoding="utf-8")
    _git(drift_repo, "add", "-A")
    _git(drift_repo, "commit", "-qm", "bulk add")

    assert sum(len(n) + 1 for n in names) > 32767, "fixture must exceed the cap"

    base = [
        "git", "-C", str(drift_repo), "log",
        "--no-merges", "--format=%h %ad %s", "--date=short",
    ]
    lines = _mod._git_log_batched(base, ["--since=30 days ago"], names)

    # One commit touched every path; the union must not report it 600 times.
    assert len(lines) == 1
    assert "bulk add" in lines[0]


def test_git_log_batched_raises_instead_of_swallowing_a_git_failure(
    drift_repo: Path,
) -> None:
    base = ["git", "-C", str(drift_repo), "log", "--format=%h %ad %s", "--date=short"]
    with pytest.raises(RuntimeError, match="git log failed"):
        _mod._git_log_batched(base, ["no-such-ref..HEAD"], ["seed.txt"])


def test_inverse_drift_maps_source_paths_onto_the_dest_tree(
    drift_repo: Path, tmp_path: Path
) -> None:
    """A source-built file list must still match dest history.

    The pre-fix code appended unresolvable paths verbatim, so git matched
    nothing and the gate reported "no drift" no matter what had landed.
    """
    (drift_repo / "seed.txt").write_text("changed in dest\n", encoding="utf-8")
    _git(drift_repo, "commit", "-qam", "dest-authored fix")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "seed.txt").write_text("seed\n", encoding="utf-8")

    files_list = tmp_path / "files.txt"
    files_list.write_text(str(source_dir / "seed.txt"), encoding="utf-8")

    percolate_root = tmp_path / "root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)

    code, out, err = _run_cli_capturing_stderr([
        "inverse-drift", "some-target",
        "--percolate-root", str(percolate_root),
        "--dest", str(drift_repo),
        "--source-dir", str(source_dir),
        "--files", str(files_list),
    ])

    assert code == 0, err
    assert "dest-authored fix" in out, out


def test_inverse_drift_fails_loud_when_paths_resolve_against_nothing(
    drift_repo: Path, tmp_path: Path
) -> None:
    files_list = tmp_path / "files.txt"
    files_list.write_text(str(tmp_path / "elsewhere" / "orphan.py"), encoding="utf-8")

    percolate_root = tmp_path / "root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)

    code, _out, err = _run_cli_capturing_stderr([
        "inverse-drift", "no-such-target",
        "--percolate-root", str(percolate_root),
        "--dest", str(drift_repo),
        "--files", str(files_list),
    ])

    assert code == 1
    assert "--source-dir" in err
