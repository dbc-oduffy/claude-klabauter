"""
Tests for coordinator_core.ops.normalize_claimed_frontmatter.

Port source: coordinator/bin/normalize-consumed-frontmatter.js (coordinator-claude)

Review: code-reviewer -- A6 finding: this file-mutating op shipped with zero
test coverage. Covers the 8 cases the reviewer enumerated: status/deployment
flip, terminal-state preservation, shipped_in insert, shipped_in refusal,
gate_dependency strip, untracked-file skip, block-scalar per-file fail-loud
continues-scan, and --dry-run writes nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops import normalize_claimed_frontmatter as ncf


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _handoff_dir(root: Path) -> Path:
    return root / "state" / "handoffs"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class TestStatusDeploymentFlip:
    def test_flips_status_and_deployment_and_inserts_claimed_at(self, tmp_path, capsys):
        f = _handoff_dir(tmp_path) / "a.md"
        _write(
            f,
            "---\n"
            "title: A\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n",
        )

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 0

        out = f.read_text(encoding="utf-8")
        assert "status: claimed" in out
        assert "deployment_state: shipped" in out
        assert "claimed_at: 2026-07-01" in out

        stdout = capsys.readouterr().out
        assert "Updated" in stdout


class TestTerminalStatePreservation:
    def test_terminal_status_and_deployment_are_not_flipped(self, tmp_path, capsys):
        f = _handoff_dir(tmp_path) / "b.md"
        _write(
            f,
            "---\n"
            "title: B\n"
            "status: claimed\n"
            "deployment_state: closed\n"
            "closed_reason: stale\n"
            "claimed_at: 2026-06-01\n"
            "shipped_in: abc1234\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n",
        )
        original = f.read_text(encoding="utf-8")

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 0

        assert f.read_text(encoding="utf-8") == original
        stderr = capsys.readouterr().err
        assert "No drift" in stderr


class TestShippedInInsertion:
    def test_conforming_sha_notes_are_inserted(self, tmp_path):
        f = _handoff_dir(tmp_path) / "c.md"
        _write(
            f,
            "---\n"
            "title: C\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 abc1234 -->\n",
        )

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 0

        out = f.read_text(encoding="utf-8")
        assert "shipped_in: abc1234" in out
        assert "shipped_in_kind: ship-commit" in out

    def test_conforming_no_commit_token_is_inserted(self, tmp_path):
        f = _handoff_dir(tmp_path) / "c2.md"
        _write(
            f,
            "---\n"
            "title: C2\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 substantively-shipped-no-commit:2026-07-01 -->\n",
        )

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 0

        out = f.read_text(encoding="utf-8")
        # Value contains a `:` (structural char) so serialize_yaml_scalar quotes it.
        assert "shipped_in: 'substantively-shipped-no-commit:2026-07-01'" in out
        assert "shipped_in_kind: no-commit" in out


class TestShippedInRefusal:
    def test_non_conforming_notes_are_refused_not_written(self, tmp_path, capsys):
        f = _handoff_dir(tmp_path) / "d.md"
        _write(
            f,
            "---\n"
            "title: D\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 shipped via PR, see notes -->\n",
        )

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 0

        out = f.read_text(encoding="utf-8")
        assert "shipped_in:" not in out
        assert "shipped_in_kind:" not in out

        stdout = capsys.readouterr().out
        assert "shipped_in: refused" in stdout


class TestGateDependencyStrip:
    def test_gate_dependency_is_stripped_on_flip(self, tmp_path):
        f = _handoff_dir(tmp_path) / "e.md"
        _write(
            f,
            "---\n"
            "title: E\n"
            "status: open\n"
            "deployment_state: awaiting_gate\n"
            "gate_dependency: waiting on sibling repo X\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n",
        )

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 0

        out = f.read_text(encoding="utf-8")
        assert "gate_dependency:" not in out
        assert "deployment_state: shipped" in out


class TestUntrackedFileSkip:
    def test_untracked_file_is_silently_skipped(self, tmp_path, capsys):
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test")

        plans_dir = tmp_path / "docs" / "plans"
        tracked = plans_dir / "tracked.md"
        untracked = plans_dir / "untracked.md"
        body = (
            "---\n"
            "title: T\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n"
        )
        _write(tracked, body)
        _write(untracked, body)

        _git(tmp_path, "add", str(tracked))
        _git(tmp_path, "commit", "-q", "-m", "add tracked plan")

        rc = ncf.main(["--root", str(tmp_path), "--type", "plan"])
        assert rc == 0

        assert "status: claimed" in tracked.read_text(encoding="utf-8")
        assert "status: claimed" not in untracked.read_text(encoding="utf-8")


class TestBlockScalarFailsLoudContinuesScan:
    def test_block_scalar_field_errors_but_scan_continues(self, tmp_path, capsys):
        bad = _handoff_dir(tmp_path) / "bad.md"
        _write(
            bad,
            "---\n"
            "title: Bad\n"
            "status: >\n"
            "  folded scalar value\n"
            "  continuation line\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n",
        )
        good = _handoff_dir(tmp_path) / "good.md"
        _write(
            good,
            "---\n"
            "title: Good\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n",
        )

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff"])
        assert rc == 1

        stderr = capsys.readouterr().err
        assert "ERROR" in stderr
        assert "bad.md" in stderr

        assert "status: claimed" in good.read_text(encoding="utf-8")


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        f = _handoff_dir(tmp_path) / "f.md"
        original = (
            "---\n"
            "title: F\n"
            "status: open\n"
            "deployment_state: in_flight\n"
            "---\n"
            "Body.\n\n"
            "<!-- consumed: 2026-07-01 -->\n"
        )
        _write(f, original)

        rc = ncf.main(["--root", str(tmp_path), "--type", "handoff", "--dry-run"])
        assert rc == 0

        assert f.read_text(encoding="utf-8") == original

        stdout = capsys.readouterr().out
        assert "Would update" in stdout
