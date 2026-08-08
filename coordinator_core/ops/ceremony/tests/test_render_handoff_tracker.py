"""
coordinator_core.ops.ceremony.tests.test_render_handoff_tracker — unit tests for
the C9 disk-write/CLI layer that wires C8b's ``render_repo_section`` to disk
(``coordinator_core.ops.ceremony.render_handoff_tracker``).

Coverage:
  (a) per-repo write — ``render_repo`` resolves the sibling-repo state root and
      the header/body shape matches the wsc_commit precedent.
  (d) op handler (``ceremony.render_handoff_tracker``) — per-repo param
      branch, missing-repo_root error shape.
  (e) CLI ``main(argv)`` — ``--root``/``--stdout`` flag handling, exit codes,
      unknown-flag rejection.

This module does NOT re-test render_repo_section's own table/grouping logic —
that is test_renderers_handoff_tracker.py's job (golden-fixture parity anchor).

Negative-spec: the fleet-aggregate ``--all-repos`` mode (and its
``repos.*``-registry-enumeration coverage) was REMOVED 2026-07-23
(PM-ratified) along with the mode itself — see the module docstring negative-
spec in ``render_handoff_tracker.py`` for the removal rationale. Do not
re-add ``--all-repos`` coverage here without re-adding the mode.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C8b/C9
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops.ceremony import render_handoff_tracker as rht
from coordinator_core.state_root import StateRootError


# ---------------------------------------------------------------------------
# (a) per-repo render + write-path resolution
# ---------------------------------------------------------------------------


class TestRenderRepo:
    def test_write_path_and_header_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        # Non-meta-repo git_root: is_meta_repo() path-compares only, no real .git needed.
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        output, out_path = rht.render_repo(repo)

        assert out_path == repo / "state" / "handoff-tracker.md"
        assert output.startswith(f"# Handoff Tracker\n\n_Generated: ")
        # Basename only -- never the full absolute path (a standing
        # concrete-path-citation finding a marker can't fix durably, since a
        # render silently wipes it every time).
        assert f"| root: {repo.name}_" in output.splitlines()[2]
        assert str(repo) not in output
        assert "## Handoffs" in output

    def test_write_creates_file_on_disk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        output, out_path = rht.render_repo(repo)
        rht._write(output, out_path)

        assert out_path.is_file()
        assert out_path.read_text(encoding="utf-8") == output + "\n"


# ---------------------------------------------------------------------------
# (d) op handler
# ---------------------------------------------------------------------------


class TestOpHandler:
    def test_per_repo_via_root_param(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        result = rht._handler({"root": str(repo)}, repo_root=None)

        assert result["exit_code"] == 0
        assert result["mode"] == "repo"
        out_path = Path(result["out_path"])
        assert out_path == repo / "state" / "handoff-tracker.md"
        assert out_path.is_file()

    def test_missing_repo_root_and_no_override_errors(self):
        result = rht._handler({}, repo_root=None)
        assert result["exit_code"] == 1
        assert "repo_root arg is None" in result["error"]


# ---------------------------------------------------------------------------
# (e) CLI main(argv)
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_root_and_stdout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        rc = rht.main(["--root", str(repo), "--stdout"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "# Handoff Tracker" in captured.out
        assert not (repo / "state" / "handoff-tracker.md").exists()

    def test_root_writes_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        rc = rht.main(["--root", str(repo)])

        assert rc == 0
        assert (repo / "state" / "handoff-tracker.md").is_file()
        assert "Wrote tracker" in capsys.readouterr().out

    def test_unknown_flag_returns_1(self, capsys):
        rc = rht.main(["--bogus"])
        assert rc == 1
        assert "Unknown argument" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# self_commit (C5 residue, 2026-07-23 wsc-tail-slim-down): opt-in only,
# exercised by the coordinator/bin/render-handoff-tracker.py CLI trampoline.
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed"],
        cwd=str(root), check=True,
    )


def _log_subjects(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(root), capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


class TestSelfCommit:
    def test_sibling_repo_branch_commits_with_explicit_pathspec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        _init_git_repo(repo)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        rc = rht.main(["--root", str(repo)], self_commit=True)

        assert rc == 0
        subjects = _log_subjects(repo)
        assert subjects[0] == "tracker: refresh handoff-tracker.md (detached render)"
        show = subprocess.run(
            ["git", "show", "--name-only", "--format="], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        assert show.stdout.strip() == "state/handoff-tracker.md"

    def test_meta_repo_branch_never_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        _init_git_repo(repo)
        central_state_root = tmp_path / "elsewhere" / "state"

        with mock.patch.object(
            rht, "coordinator_state_root", return_value=str(central_state_root),
        ):
            rc = rht.main(["--root", str(repo)], self_commit=True)

        assert rc == 0
        assert (central_state_root / "handoff-tracker.md").is_file()
        # Nothing landed in the sibling repo's own history -- the artifact is
        # in a DIFFERENT git repo entirely.
        assert _log_subjects(repo) == ["seed"]

    def test_no_change_produces_no_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        _init_git_repo(repo)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        # Freeze the generated timestamp so two consecutive runs produce
        # byte-identical output -- the second run is a genuinely no-change
        # render (must be a clean no-op: no commit, no failure record).
        with mock.patch.object(
            rht, "_generated_timestamp", return_value="2026-07-23 00:00 UTC",
        ):
            rc1 = rht.main(["--root", str(repo)], self_commit=True)
            after_first = _log_subjects(repo)
            rc2 = rht.main(["--root", str(repo)], self_commit=True)
            after_second = _log_subjects(repo)

        assert rc1 == 0 and rc2 == 0
        assert len(after_first) == 2  # seed + this render's own commit
        assert after_second == after_first  # no new commit on the identical re-render
        log_path = repo / "state" / "housekeeping-failures.log"
        assert not log_path.exists()

    def test_self_commit_false_default_never_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        repo = tmp_path / "sibling-repo"
        repo.mkdir()
        _init_git_repo(repo)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "not-the-meta-repo"))

        rc = rht.main(["--root", str(repo)])  # self_commit defaults False

        assert rc == 0
        assert (repo / "state" / "handoff-tracker.md").is_file()
        assert _log_subjects(repo) == ["seed"]  # unchanged -- no follow-up commit
