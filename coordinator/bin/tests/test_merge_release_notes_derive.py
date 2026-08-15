"""test_merge_release_notes_derive — pytest tests for
merge-release-notes-derive.py (ported /merging-to-main Step 5.5 logic:
pending-release reconcile sweep + tag-history release attribution walk).

Spec backlink: DoE-claude coordinator/skills/merging-to-main/SKILL.md
  Step 5.5 items 2-3 (pre-port original, ported verbatim into this CLI).

Coverage:
  TestFlipTags:
    test_flip_entry_resolves_to_earliest_containing_tag -- a commit already
      reachable from an EARLIER tag gets attributed to that earlier tag, not
      the newest release_tag_cut (the exact blanket-latest misattribution bug
      this logic exists to close).
    test_flip_entry_falls_through_to_release_tag_cut -- an entry whose
      commit isn't yet reachable from any existing tag (this release's own
      work) falls through to release_tag_cut/merge_sha/merge_date.
    test_flip_entry_skips_non_pending_status -- an entry whose status is not
      `pending-release` is left untouched (idempotency: re-running over an
      already-`released` entry is a no-op).
    test_cli_end_to_end_multiple_entries -- runs the `flip-tags` subcommand
      as a real subprocess over a scratch git repo with two entries, one
      resolving to an old tag and one falling through to the new cut tag.

  TestReconcileSweep:
    test_warns_on_nonzero_delta -- an entry with authored_by set and a
      sibling reconcile-completion-commits.py stub reporting delta=2 emits
      the "unaccounted session commit(s)" warning line.
    test_silent_on_null_authored_by -- an entry with authored_by: null is
      skipped (no sibling CLI invocation, no output).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_release_notes_derive",
        _BIN_DIR / "merge-release-notes-derive.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# git repo fixture
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")


def _commit(path: Path, filename: str, content: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-q", "-m", f"commit {filename}")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _tag(path: Path, name: str) -> None:
    _git(path, "tag", "-a", name, "-m", name)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _init_repo(r)
    return r


# ---------------------------------------------------------------------------
# TestFlipTags — pure-function level
# ---------------------------------------------------------------------------


def _entry_text(status: str, commits: list[str]) -> str:
    commit_lines = "\n".join(f"  - {c}" for c in commits)
    return (
        "---\n"
        f"status: {status}\n"
        "commits:\n"
        f"{commit_lines}\n"
        "---\n"
        "body text\n"
    )


class TestFlipTags:
    def test_flip_entry_resolves_to_earliest_containing_tag(self, repo: Path):
        old_sha = _commit(repo, "a.txt", "a")
        _tag(repo, "v1.0.0")
        new_sha = _commit(repo, "b.txt", "b")
        _tag(repo, "v2.0.0")

        entry = repo / "entry.md"
        entry.write_text(_entry_text("pending-release", [old_sha]), encoding="utf-8")

        import os

        cwd = os.getcwd()
        os.chdir(repo)
        try:
            tags = ["v1.0.0", "v2.0.0"]
            result = _mod._flip_entry(
                str(entry), tags, "v2.0.0", new_sha, "2026-07-23"
            )
        finally:
            os.chdir(cwd)

        assert result == f"{entry}: released_in=v1.0.0"
        text = entry.read_text(encoding="utf-8")
        assert "status: released" in text
        assert "released_in: v1.0.0" in text

    def test_flip_entry_falls_through_to_release_tag_cut(self, repo: Path):
        old_sha = _commit(repo, "a.txt", "a")
        _tag(repo, "v1.0.0")
        new_sha = _commit(repo, "b.txt", "b")
        # v2.0.0 not yet cut at time of flip -- new_sha isn't reachable from
        # any known tag yet.

        entry = repo / "entry.md"
        entry.write_text(_entry_text("pending-release", [new_sha]), encoding="utf-8")

        import os

        cwd = os.getcwd()
        os.chdir(repo)
        try:
            tags = ["v1.0.0", "v2.0.0"]
            result = _mod._flip_entry(
                str(entry), tags, "v2.0.0", new_sha, "2026-07-23"
            )
        finally:
            os.chdir(cwd)

        assert result == f"{entry}: released_in=v2.0.0"
        text = entry.read_text(encoding="utf-8")
        assert "released_sha: " + new_sha in text
        assert "released_at: 2026-07-23" in text

    def test_flip_entry_skips_non_pending_status(self, repo: Path):
        sha = _commit(repo, "a.txt", "a")
        entry = repo / "entry.md"
        original = _entry_text("released", [sha])
        entry.write_text(original, encoding="utf-8")

        import os

        cwd = os.getcwd()
        os.chdir(repo)
        try:
            result = _mod._flip_entry(
                str(entry), ["v1.0.0"], "v1.0.0", sha, "2026-07-23"
            )
        finally:
            os.chdir(cwd)

        assert result is None
        assert entry.read_text(encoding="utf-8") == original

    def test_cli_end_to_end_multiple_entries(self, repo: Path):
        old_sha = _commit(repo, "a.txt", "a")
        _tag(repo, "v1.0.0")
        new_sha = _commit(repo, "b.txt", "b")
        _tag(repo, "v2.0.0")

        entry_old = repo / "entry-old.md"
        entry_old.write_text(_entry_text("pending-release", [old_sha]), encoding="utf-8")
        entry_new = repo / "entry-new.md"
        entry_new.write_text(_entry_text("pending-release", [new_sha]), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(_BIN_DIR / "merge-release-notes-derive.py"),
                "flip-tags",
                "v2.0.0",
                new_sha,
                "2026-07-23",
                str(entry_old),
                str(entry_new),
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        assert result.returncode == 0, result.stderr
        assert f"{entry_old}: released_in=v1.0.0" in result.stdout
        assert f"{entry_new}: released_in=v2.0.0" in result.stdout


# ---------------------------------------------------------------------------
# TestReconcileSweep — module-level with _run_sibling_cli monkeypatched
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout


class TestReconcileSweep:
    def test_warns_on_nonzero_delta(self, tmp_path: Path, monkeypatch, capsys):
        entry = tmp_path / "entry.md"
        entry.write_text(
            "---\nstatus: pending-release\nauthored_by: session-abc\n---\nbody\n",
            encoding="utf-8",
        )

        def _fake_run_sibling_cli(name, args):
            if name == "query-completions.py":
                return _FakeCompleted(str(entry) + "\n")
            if name == "reconcile-completion-commits.py":
                return _FakeCompleted("delta=2\n")
            raise AssertionError(f"unexpected sibling CLI: {name}")

        monkeypatch.setattr(_mod, "_run_sibling_cli", _fake_run_sibling_cli)

        rc = _mod.cmd_reconcile_sweep(argparse_namespace())
        captured = capsys.readouterr()
        assert rc == 0
        assert "entry.md" in captured.out
        assert "2 session commit(s) unaccounted" in captured.out

    def test_silent_on_null_authored_by(self, tmp_path: Path, monkeypatch, capsys):
        entry = tmp_path / "entry.md"
        entry.write_text(
            "---\nstatus: pending-release\nauthored_by: null\n---\nbody\n",
            encoding="utf-8",
        )

        calls = []

        def _fake_run_sibling_cli(name, args):
            calls.append(name)
            if name == "query-completions.py":
                return _FakeCompleted(str(entry) + "\n")
            raise AssertionError("reconcile-completion-commits.py must not be invoked")

        monkeypatch.setattr(_mod, "_run_sibling_cli", _fake_run_sibling_cli)

        rc = _mod.cmd_reconcile_sweep(argparse_namespace())
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""
        assert calls == ["query-completions.py"]


def argparse_namespace():
    import argparse

    return argparse.Namespace()
