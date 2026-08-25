"""test_merge_release_notes_derive — pytest tests for
merge-release-notes-derive.py (ported /merging-to-main Step 5.5 logic:
tag-history release attribution walk).

Spec backlink: DoE-claude coordinator/skills/merging-to-main/SKILL.md
  Step 5.5 item 3 (pre-port original, ported verbatim into this CLI).

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

`TestReconcileSweep` (the `reconcile-sweep` subcommand's coverage) was
removed 2026-08-23 along with `completion.reconcile_commits`, the op it
dispatched — do not resurrect it before the op's replacement lands.
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


class TestContainsAllAbbreviatedShas:
    """`commits:` entries in a completion-log carry ABBREVIATED shas (8 chars);
    `git rev-list` emits full 40-char ones. Equality membership silently returns
    False for every real entry, collapsing tag resolution to the release-tag-cut
    fallback with no exception and no other failing test. Regression guard for
    the batched form of `_contains_all` (one `rev-list` per tag replacing a
    per-commit `merge-base --is-ancestor` spawn).
    """

    def _stub_rev_list(self, monkeypatch, shas):
        def _fake_git(*args):
            assert args[0] == "rev-list", args
            return subprocess.CompletedProcess(
                args=list(args), returncode=0, stdout="\n".join(shas) + "\n", stderr=""
            )

        monkeypatch.setattr(_mod, "_git", _fake_git)

    def test_abbreviated_commit_matches_full_ancestor_sha(self, monkeypatch):
        full = "1389e207ab34cd56ef78901234567890abcdef12"
        self._stub_rev_list(monkeypatch, [full, "f" * 40])
        assert _mod._contains_all("v1.0.0", [full[:8]]) is True

    def test_full_commit_still_matches(self, monkeypatch):
        full = "1389e207ab34cd56ef78901234567890abcdef12"
        self._stub_rev_list(monkeypatch, [full])
        assert _mod._contains_all("v1.0.0", [full]) is True

    def test_absent_commit_does_not_match(self, monkeypatch):
        self._stub_rev_list(monkeypatch, ["1389e207ab34cd56ef78901234567890abcdef12"])
        assert _mod._contains_all("v1.0.0", ["deadbeef"]) is False

    def test_all_must_be_present(self, monkeypatch):
        self._stub_rev_list(monkeypatch, ["1389e207ab34cd56ef78901234567890abcdef12"])
        assert _mod._contains_all("v1.0.0", ["1389e207", "deadbeef"]) is False

    def test_unresolvable_tag_yields_no_match(self, monkeypatch):
        def _fake_git(*args):
            return subprocess.CompletedProcess(
                args=list(args), returncode=128, stdout="", stderr="bad revision"
            )

        monkeypatch.setattr(_mod, "_git", _fake_git)
        assert _mod._contains_all("v9.9.9", ["1389e207"]) is False
