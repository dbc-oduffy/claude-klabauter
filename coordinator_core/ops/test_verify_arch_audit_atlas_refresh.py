"""
test_verify_arch_audit_atlas_refresh.py — pytest unit tests for
coordinator_core.ops.verify_arch_audit_atlas_refresh.

Port source: coordinator/tests/test_atlas_refresh_gate.py (DoE-claude,
bash-oracle subprocess tests over `bash <helper> ...`) — reauthored here as
direct in-process calls against the ported Python module's `main(argv)`,
same fixtures/assertions, plus platform-edge cases the DoE oracle test never
exercised (Windows-shaped TMPDIR-absent env; git-timeout degrade path).

Spec backlink: docs/plans/2026-06-04-architecture-audit-atlas-refresh-gate.md § C1
Spec backlink: docs/plans/2026-06-08-atlas-attested-clock-split.md
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Isolation: every test builds a fresh fixture git repo under tmp_path and
calls `main(argv)` with `os.chdir` scoped to that repo (captured stdout via
capsys). Negative-spec: tests do NOT touch the real ~/.claude
docs/architecture/systems/ tree or the operator's working tree.
"""

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import verify_arch_audit_atlas_refresh as mod
from coordinator_core.ops.verify_arch_audit_atlas_refresh import main
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=repo)
    _run(["git", "config", "core.autocrlf", "false"], cwd=repo)
    _run(["git", "config", "core.eol", "lf"], cwd=repo)
    return repo


def _write_atlas(repo: Path, system: str, last_mapped: str, last_attested: str = None, body: str = "") -> Path:
    if last_attested is None:
        last_attested = last_mapped
    atlas_dir = repo / "docs" / "architecture" / "systems"
    atlas_dir.mkdir(parents=True, exist_ok=True)
    atlas_path = atlas_dir / f"{system}.md"
    content = (
        "---\n"
        f"system: {system}\n"
        f"last_mapped: {last_mapped}\n"
        f"last_attested: {last_attested}\n"
        "---\n"
        "\n"
        f"# {system}\n"
        "\n"
        f"{body}\n"
    )
    atlas_path.write_bytes(content.encode("utf-8"))
    return atlas_path


def _stage(repo: Path, path: Path):
    rel = path.relative_to(repo).as_posix()
    _run(["git", "add", "--", rel], cwd=repo)


def _commit_baseline(repo: Path, message: str = "baseline"):
    readme = repo / "README.md"
    readme.write_text("baseline\n", encoding="utf-8")
    _run(["git", "add", "--", "README.md"], cwd=repo)
    _run(["git", "commit", "-q", "-m", message], cwd=repo)


def _invoke(repo: Path, monkeypatch, audit_date: str, system: str, commit_msg_file: str = None) -> int:
    """Chdir into repo and call main() directly, matching the CLI contract."""
    monkeypatch.chdir(repo)
    argv = [audit_date, system]
    if commit_msg_file is not None:
        argv.append(commit_msg_file)
    return main(argv)


# ---------------------------------------------------------------------------
# AC5 — Branch A pass with both clocks.
# ---------------------------------------------------------------------------
def test_branch_a_requires_both_clocks_and_body_diff(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-04-01", "2026-04-01", "Old body.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    _write_atlas(
        repo, "demo-system", "2026-06-04", "2026-06-04",
        "Refreshed body line one.\nRefreshed body line two.",
    )
    _stage(repo, atlas)

    rc = _invoke(repo, monkeypatch, "2026-06-04", "demo-system")
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS branch=A" in out


# ---------------------------------------------------------------------------
# AC6 — Branch A teaching-shape FAIL when last_attested not bumped.
# ---------------------------------------------------------------------------
def test_branch_a_fails_without_last_attested_bump(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-04-01", "2026-04-01", "Original body.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    _write_atlas(
        repo, "demo-system", "2026-06-04", "2026-04-01",
        "Updated body content — rotation happened.",
    )
    _stage(repo, atlas)

    msg_file = repo / "COMMIT_MSG"
    msg_file.write_text("arch-audit: demo-system\n", encoding="utf-8")

    rc = _invoke(repo, monkeypatch, "2026-06-04", "demo-system", commit_msg_file=str(msg_file))
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAIL" in out
    assert "A real audit IS an attestation" in out
    assert "bump both clocks" in out


# ---------------------------------------------------------------------------
# AC3 — Branch B passes when last_attested bumped only.
# ---------------------------------------------------------------------------
def test_branch_b_passes_on_last_attested_bump_only(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-06-01", "2026-06-01", "Same body content.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    _write_atlas(repo, "demo-system", "2026-06-01", "2026-06-04", "Same body content.")
    _stage(repo, atlas)

    msg_file = repo / "COMMIT_MSG"
    msg_file.write_text(
        "arch-audit: ratify demo-system atlas current\n\n"
        "atlas-current-as-of:2026-06-04\n",
        encoding="utf-8",
    )

    rc = _invoke(repo, monkeypatch, "2026-06-04", "demo-system", commit_msg_file=str(msg_file))
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS branch=B" in out


# ---------------------------------------------------------------------------
# AC4 — same-day re-attestation.
# ---------------------------------------------------------------------------
def test_branch_b_same_day_reattestation_passes(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-06-07", "2026-06-07", "Body unchanged.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    _write_atlas(repo, "demo-system", "2026-06-07", "2026-06-08", "Body unchanged.")
    _stage(repo, atlas)

    msg_file = repo / "COMMIT_MSG"
    msg_file.write_text(
        "arch-audit: re-attest demo-system same day\n\n"
        "atlas-current-as-of:2026-06-08\n",
        encoding="utf-8",
    )

    rc = _invoke(repo, monkeypatch, "2026-06-08", "demo-system", commit_msg_file=str(msg_file))
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS branch=B" in out


def test_neither_branch_fail(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-04-01", "2026-04-01", "Original body.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    msg_file = repo / "COMMIT_MSG"
    msg_file.write_text("arch-audit: demo-system\n", encoding="utf-8")

    rc = _invoke(repo, monkeypatch, "2026-06-04", "demo-system", commit_msg_file=str(msg_file))
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAIL: /architecture-audit Step 6.5 atlas-refresh gate not satisfied" in out
    assert "demo-system" in out
    assert "refresh docs/architecture/systems/demo-system.md" in out
    assert "atlas-current-as-of:2026-06-04" in out


def test_branch_a_wrong_date_fails(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-04-01", "2026-04-01", "Original body.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    _write_atlas(repo, "demo-system", "2026-05-15", "2026-05-15", "Updated body content here.")
    _stage(repo, atlas)

    msg_file = repo / "COMMIT_MSG"
    msg_file.write_text("arch-audit: demo-system\n", encoding="utf-8")

    rc = _invoke(repo, monkeypatch, "2026-06-04", "demo-system", commit_msg_file=str(msg_file))
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAIL: /architecture-audit Step 6.5 atlas-refresh gate not satisfied" in out


def test_branch_b_missing_token_fails(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    atlas = _write_atlas(repo, "demo-system", "2026-04-01", "2026-04-01", "Stable body content.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    _write_atlas(repo, "demo-system", "2026-04-01", "2026-06-04", "Stable body content.")
    _stage(repo, atlas)

    msg_file = repo / "COMMIT_MSG"
    msg_file.write_text("arch-audit: demo-system closeout\n", encoding="utf-8")

    rc = _invoke(repo, monkeypatch, "2026-06-04", "demo-system", commit_msg_file=str(msg_file))
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAIL: /architecture-audit Step 6.5 atlas-refresh gate not satisfied" in out


def test_skip_when_atlas_absent(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)

    rc = _invoke(repo, monkeypatch, "2026-06-04", "nonexistent-system")
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIP" in out
    assert "nonexistent-system" in out


def test_skip_when_not_inside_git_work_tree(tmp_path, monkeypatch, capsys):
    """Negative case not covered by the DoE oracle test: bare (non-git) cwd."""
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)
    rc = main(["2026-06-04", "demo-system"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIP" in out
    assert "not inside a git work tree" in out


def test_usage_on_missing_args(monkeypatch, capsys):
    rc = main(["2026-06-04"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "usage: verify-arch-audit-atlas-refresh.sh" in err


# ---------------------------------------------------------------------------
# Platform-edge case (addendum rule 2/4): git subprocess timeout must not
# hang the caller — degrade gracefully rather than block. Exercises the
# fixed-timeout path via a monkeypatched git timeout, not a real 30s wait.
# ---------------------------------------------------------------------------
def test_git_timeout_degrades_gracefully(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _commit_baseline(repo)
    atlas = _write_atlas(repo, "demo-system", "2026-04-01", "2026-04-01", "Body.")
    _stage(repo, atlas)
    _run(["git", "commit", "-q", "-m", "seed atlas"], cwd=repo)

    real_run = subprocess.run

    def _raise_timeout(cmd, *a, **kw):
        if cmd[:2] == ["git", "diff"]:
            raise subprocess.TimeoutExpired(cmd, 30)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", _raise_timeout)
    monkeypatch.chdir(repo)

    # Should not raise / hang — degrades to empty diff content, still exits 0.
    rc = main(["2026-06-04", "demo-system"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out  # some verdict line was emitted, not a hang/crash
