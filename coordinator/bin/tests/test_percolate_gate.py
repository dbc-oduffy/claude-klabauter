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
