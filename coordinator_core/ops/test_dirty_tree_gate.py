"""
Characterization tests for coordinator_core.ops.dirty_tree_gate — same 8
assertions as the bash oracle's own test suite.

Port of: dirty-tree-gate.sh (DoE 894d4bc6, 2026-07-22)
Oracle: test-dirty-tree-gate.sh (DoE 894d4bc6, 2026-07-22)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state
from coordinator_core.ops.dirty_tree_gate import _build_known_scope, _resolve_plugin_root, main
from coordinator_core.testing.doe_root import resolve_doe_root
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@dirty-tree-gate")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitkeep").touch()
    _git(repo, "add", ".gitkeep")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def isolated_plugin_root(tmp_path, monkeypatch):
    """Point CLAUDE_PLUGIN_ROOT at the real DoE-claude coordinator/ checkout.

    The gate's own resolution logic (CLAUDE_PLUGIN_ROOT / .doe-root) is
    exercised by _resolve_plugin_root's own unit coverage below; classifier
    behavior tests need a *working* coordinator-state-root.sh, so they point
    straight at the sibling DoE-claude repo's coordinator/ tree.
    """
    doe_coordinator = os.environ.get("DOE_COORDINATOR_ROOT")
    if not doe_coordinator:
        doe_root = resolve_doe_root()
        if doe_root:
            candidate = Path(doe_root) / "coordinator"
            if candidate.is_dir():
                doe_coordinator = str(candidate)
    if not doe_coordinator or not Path(doe_coordinator).is_dir():
        pytest.skip("sibling DoE-claude/coordinator checkout not found")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", doe_coordinator)
    return doe_coordinator


def test_resolve_plugin_root_doe_root_rung_uses_userprofile(tmp_path, monkeypatch):
    """Native-Windows condition for the ``.doe-root`` legacy rung
    (home-resolution-lint bare_home_or_chain fix, 2026-07-29): CLAUDE_PLUGIN_ROOT
    unset, CLAUDE_HOME/HOME both absent, only USERPROFILE set. The rung now
    delegates to ``read_doe_root_pointer_file()``'s own default (which falls
    through to ``os.path.expanduser("~")``, Windows-safe) instead of a
    hand-rolled two-rung ``CLAUDE_HOME or HOME`` chain that degraded to a
    cwd-relative pointer path in exactly this condition."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-unused"))

    userprofile_home = tmp_path / "winhome"
    userprofile_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(userprofile_home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(userprofile_home) if p == "~" else p)

    doe_repo = tmp_path / "doe-repo"
    coordinator_dir = doe_repo / "coordinator"
    coordinator_dir.mkdir(parents=True)
    doe_root_dir = userprofile_home / ".claude"
    doe_root_dir.mkdir(parents=True)
    (doe_root_dir / ".doe-root").write_text(str(doe_repo), encoding="utf-8")

    plugin_root, err = _resolve_plugin_root()
    assert err is None, err
    assert plugin_root == str(coordinator_dir)


def _run_gate(repo: Path, *args: str, capsys):
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        rc = main(list(args))
    finally:
        os.chdir(cwd)
    captured = capsys.readouterr()
    return rc, captured.out


def test_staged_only_case_a_exits_zero(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t1")
    (repo / "session-file.txt").write_text("session work\n")
    _git(repo, "add", "session-file.txt")
    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 0


def test_claimed_handoff_scope_case_b_exits_zero(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t2")
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "some" / "dir").mkdir(parents=True)
    (repo / "state" / "handoffs" / "test-consumed.md").write_text(
        "---\n"
        "status: claimed\n"
        "claimed_by: abc-session-123\n"
        "scope:\n"
        "  - some/dir/owned-file.txt\n"
        "---\n"
    )
    (repo / "some" / "dir" / "owned-file.txt").write_text("original\n")
    _git(repo, "add", "state/handoffs/test-consumed.md", "some/dir/owned-file.txt")
    _git(repo, "commit", "-q", "-m", "add handoff and owned file")
    (repo / "some" / "dir" / "owned-file.txt").write_text("modified by sibling session\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 0


def test_legacy_consumed_by_field_case_b_exits_zero(tmp_path, isolated_plugin_root, capsys):
    # Deliberately keeps legacy `consumed_by:` field vocabulary — exercises
    # _build_known_scope's documented old-name tolerance (this is a
    # read-only classifier, never a writer, so it never migrates off
    # accepting the legacy field name).
    repo = _make_repo(tmp_path, "t2b")
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "some" / "dir").mkdir(parents=True)
    (repo / "state" / "handoffs" / "test-consumed.md").write_text(
        "---\n"
        "status: consumed\n"
        "consumed_by: abc-session-123\n"
        "scope:\n"
        "  - some/dir/owned-file.txt\n"
        "---\n"
    )
    (repo / "some" / "dir" / "owned-file.txt").write_text("original\n")
    _git(repo, "add", "state/handoffs/test-consumed.md", "some/dir/owned-file.txt")
    _git(repo, "commit", "-q", "-m", "add handoff and owned file")
    (repo / "some" / "dir" / "owned-file.txt").write_text("modified by sibling session\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 0


def test_unattributable_untracked_case_c_exits_three(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t3")
    (repo / "orphan.txt").write_text("orphaned content\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 3
    assert "orphan.txt" in out


def test_new_directory_reports_every_file_not_the_collapsed_dir(
    tmp_path, isolated_plugin_root, capsys
):
    """Negative-spec: git's default `--porcelain` collapses a wholly-new
    directory to one `?? dir/` entry. This gate must classify the FILES, not
    the directory — a collapsed entry both under-reports the operator's real
    disposition load and can never match the case-(b) `known_scope` set, which
    holds handoff file paths."""
    repo = _make_repo(tmp_path, "t3b")
    nested = repo / "brand-new-dir"
    nested.mkdir()
    (nested / "one.txt").write_text("a\n")
    (nested / "two.txt").write_text("b\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 3
    assert "brand-new-dir/one.txt" in out
    assert "brand-new-dir/two.txt" in out
    assert "brand-new-dir\n" not in out


def test_unattributable_unstaged_modification_case_c_exits_three(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t4")
    (repo / "tracked.txt").write_text("original\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "add tracked")
    (repo / "tracked.txt").write_text("modified by unknown session\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 3
    assert "tracked.txt" in out


def test_eol_phantom_not_flagged_exits_zero(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t5")
    to_empty = repo / "to-empty.sh"
    to_empty.write_text("#!/usr/bin/env bash\ncat /dev/null\n")
    to_empty.chmod(0o755)
    # git invokes textconv driver commands via its bundled `sh -c "<value>"`
    # (even on Windows/Git-for-Windows, whose sh is MSYS bash). A Windows
    # backslash path fed into that shell string is misparsed as escape
    # sequences (`C:\Users\...` -> `C:Users...`), producing "command not
    # found" and a nonzero `git diff --quiet` exit — which defeats the very
    # EOL-phantom filter this test exercises. Use a forward-slash path,
    # which both POSIX sh and Git-for-Windows' MSYS sh accept.
    _git(repo, "config", "diff.eol-phantom.textconv", str(to_empty).replace(os.sep, "/"))
    _git(repo, "config", "diff.eol-phantom.cachetextconv", "false")
    (repo / ".gitattributes").write_text("phantom.txt diff=eol-phantom\n")
    (repo / "phantom.txt").write_text("original phantom content\n")
    _git(repo, "add", ".gitattributes", "phantom.txt", "to-empty.sh")
    _git(repo, "commit", "-q", "-m", "add phantom setup")
    (repo / "phantom.txt").write_text("changed phantom content\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 0


def test_unattributable_non_ascii_path_case_c_exits_three(tmp_path, isolated_plugin_root, capsys):
    """Regression: `core.quotepath` at git's DEFAULT (true) C-quotes a
    non-ASCII path, and in a diff header the quotes wrap the WHOLE `"b/
    <path>"` token — a naive `+++ b/` prefix match never sees the path, so
    it never enters the batched phantom-filter's `changed` set, and a
    genuinely-dirty file is misread as `path not in diff_paths` -> phantom
    -> skipped. Fail-OPEN: a should-be-case-(c) file silently let through,
    this module's own named worst case. Guards `_diff_changed_paths` and
    `main()`'s `git status --porcelain` call both passing
    `-c core.quotepath=false` so path form agrees on both sides."""
    repo = _make_repo(tmp_path, "t7")
    (repo / "mä.txt").write_text("original\n")
    _git(repo, "add", "mä.txt")
    _git(repo, "commit", "-q", "-m", "add non-ascii tracked file")
    (repo / "mä.txt").write_text("modified by unknown session\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 3
    assert "mä.txt" in out


def test_missing_terminator_exits_two(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t6")
    rc, out = _run_gate(repo, capsys=capsys)
    assert rc == 2


def test_staged_plus_consumed_scope_no_c_exits_zero(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t7")
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "some" / "dir").mkdir(parents=True)
    (repo / "state" / "handoffs" / "joint-test.md").write_text(
        "---\n"
        "status: claimed\n"
        "claimed_by: peer-session-456\n"
        "scope:\n"
        "  - some/dir/peer-file.txt\n"
        "---\n"
    )
    (repo / "some" / "dir" / "peer-file.txt").write_text("original\n")
    _git(repo, "add", "state/handoffs/joint-test.md", "some/dir/peer-file.txt")
    _git(repo, "commit", "-q", "-m", "setup joint test")
    (repo / "some" / "dir" / "peer-file.txt").write_text("modified by peer\n")
    (repo / "my-session-file.txt").write_text("this session work\n")
    _git(repo, "add", "my-session-file.txt")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 0


def test_scope_b_alongside_orphan_c_only_c_listed(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t8")
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "some" / "dir").mkdir(parents=True)
    (repo / "state" / "handoffs" / "mixed-test.md").write_text(
        "---\n"
        "status: claimed\n"
        "claimed_by: concurrent-session-789\n"
        "scope:\n"
        "  - some/dir/owned-b.txt\n"
        "---\n"
    )
    (repo / "some" / "dir" / "owned-b.txt").write_text("original\n")
    _git(repo, "add", "state/handoffs/mixed-test.md", "some/dir/owned-b.txt")
    _git(repo, "commit", "-q", "-m", "setup mixed test")
    (repo / "some" / "dir" / "owned-b.txt").write_text("peer modified\n")
    (repo / "orphan-in-mixed.txt").write_text("orphan\n")

    rc, out = _run_gate(repo, "--terminator", "test", capsys=capsys)
    assert rc == 3
    assert "orphan-in-mixed.txt" in out
    assert "owned-b.txt" not in out


def test_not_a_git_repo_exits_two(tmp_path, isolated_plugin_root, capsys):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    rc, out = _run_gate(non_repo, "--terminator", "test", capsys=capsys)
    assert rc == 2


def test_unknown_argument_exits_two(tmp_path, isolated_plugin_root, capsys):
    repo = _make_repo(tmp_path, "t9")
    rc, out = _run_gate(repo, "--bogus", capsys=capsys)
    assert rc == 2


# ---------------------------------------------------------------------------
# _build_known_scope — claim-ledger desync (AC4, docs/plans/2026-08-07-claim-
# state-ledger-first-authoritative-read.md § C3)
#
# Review: overengineering-reviewer — migrated from
# ops/ceremony/tests/test_commit_gates_known_scope.py, whose lockstep-parity
# purpose (running this predicate side-by-side with a second, now-deleted
# `ceremony/commit_gates.py::_build_known_scope` copy) died when 629cd7724b
# deleted the second copy. Not already covered by the case-(b) tests above,
# which exercise a mirror `claimed_by`/`consumed_by` field rather than a
# claim-ledger-only desync.
# ---------------------------------------------------------------------------


def _write_desynced_handoff(repo: Path, name: str) -> Path:
    """A handoff whose tracked frontmatter mirror is reverted to `open` (no
    claimed_by/consumed_by) but that a peer session still holds via the claim
    ledger -- the exact branch-switch-revert desync AC4 exists to fix."""
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    handoff = repo / "state" / "handoffs" / name
    handoff.write_text(
        "---\n"
        "status: open\n"
        "scope:\n"
        "  - peers/owned-file.txt\n"
        "category: workstream\n"
        "---\n"
        "\n# desynced peer handoff\n",
        encoding="utf-8",
    )
    _git(repo, "add", "--", f"state/handoffs/{name}")
    _git(repo, "commit", "-q", "-m", "seed: desynced peer handoff")
    return handoff


def _write_ledger_claim(repo: Path, handoff_name: str, session_id: str = "peer-session-id") -> None:
    common_dir = repo / ".git"
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")


def test_known_scope_desynced_handoff_scope_paths_survive(tmp_path):
    repo = _make_repo(tmp_path, "t10")
    handoff_name = "2026-08-07_120000_peer.md"
    _write_desynced_handoff(repo, handoff_name)
    _write_ledger_claim(repo, handoff_name)

    handoffs_dir = str(repo / "state" / "handoffs")
    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        known_scope = _build_known_scope(handoffs_dir, repo_root=str(repo))

    assert "peers/owned-file.txt" in known_scope


def test_known_scope_dead_ledger_holder_no_mirror_drops_scope(tmp_path):
    repo = _make_repo(tmp_path, "t11")
    handoff_name = "2026-08-07_120001_dead.md"
    _write_desynced_handoff(repo, handoff_name)
    _write_ledger_claim(repo, handoff_name, session_id="dead-session-id")

    handoffs_dir = str(repo / "state" / "handoffs")
    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=False):
        known_scope = _build_known_scope(handoffs_dir, repo_root=str(repo))

    assert "peers/owned-file.txt" not in known_scope
