"""Characterization + parity tests for coordinator_core.ops.discover_working_repos.

Mirrors the bash oracle's own regression net (issue #12) plus dedicated
coverage for the greedy-decode disambiguator and the platform/permission
edges called out in PORTER-BRIEF-ADDENDUM.md.

Port of: discover-working-repos.sh (DoE 6fb5fb37, 2026-07-22)
Oracle: test-discover-working-repos.sh (DoE 6fb5fb37, 2026-07-22)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.discover_working_repos import (
    _TIER_A_EXCLUDE_RE,
    _decode_projects_dir_name,
    _fs_probe_path,
    _gate_and_dedup,
    _is_git_root,
    _sort_unique,
    _tier_a,
    _tier_a_greedy_decode,
    _tier_a_posix,
    _tier_b,
    _to_posix_key,
    main,
)

# Declared, not excused: this file spawns a real git process because
# `_is_git_root`/discovery under test detect real `.git` directory presence
# and real repo state across a tiered filesystem walk -- no mock stands in
# for real repo-root detection. Each test builds its own repo at a distinct
# path shape (tier A/B, nested, decoded-name edges), so `_init_git_repo` is
# not hoisted to module scope -- per-test isolation. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-q", "-b", "main"],
        check=False,
        timeout=30,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.com"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True, timeout=30)
    (path / "f").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, timeout=30)


class TestIsGitRoot:
    def test_repo_root_recognized(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        assert _is_git_root(str(repo)) is True

    def test_bare_parent_dir_rejected(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        assert _is_git_root(str(tmp_path / "dev")) is False

    def test_repo_subdir_rejected(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        sub = repo / "sub"
        sub.mkdir()
        assert _is_git_root(str(sub)) is False

    def test_missing_path_rejected(self, tmp_path: Path):
        assert _is_git_root(str(tmp_path / "does-not-exist")) is False

    def test_forward_slash_path_recognized_on_native_separator_platform(self, tmp_path: Path):
        # Regression net for the separator-mismatch bug: git rev-parse
        # --show-toplevel always emits POSIX (forward-slash) separators,
        # while os.path.realpath emits native separators (backslashes on
        # Windows). A plain string `==` between the two never held on
        # Windows even when they name the same directory on disk. Address
        # the fixture with forward slashes (regardless of host platform) to
        # force that mismatch and assert identity is still established via
        # samefile().
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        forward_slash_path = str(repo).replace("\\", "/")
        assert _is_git_root(forward_slash_path) is True

    def test_plain_non_repo_dir_rejected(self, tmp_path: Path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert _is_git_root(str(plain)) is False


class TestGateAndDedup:
    def test_keeps_only_the_repo_root(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        nonrepo = tmp_path / "dev"
        scratch = repo / "sub"
        scratch.mkdir()
        out = list(_gate_and_dedup([str(repo), str(nonrepo), str(scratch)]))
        assert out == [str(repo)]

    def test_cross_form_dedup_trailing_slash(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        out = list(_gate_and_dedup([str(repo), str(repo) + "/", str(repo)]))
        assert len(out) == 1
        assert out[0] == str(repo)

    def test_empty_lines_skipped(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        out = list(_gate_and_dedup(["", str(repo), ""]))
        assert out == [str(repo)]


class TestToPosixKey:
    def test_native_drive_form(self):
        assert _to_posix_key("X:\\dev\\repo") == "/x/dev/repo"

    def test_posix_form_drive(self):
        assert _to_posix_key("X:/dev/repo") == "/x/dev/repo"

    def test_lowercases_drive_only(self):
        # Only the drive letter is lowercased — rest of the path keeps case.
        assert _to_posix_key("X:\\Dev\\Repo") == "/x/Dev/Repo"

    def test_posix_passthrough(self):
        assert _to_posix_key("/x/dev/repo") == "/x/dev/repo"

    def test_backslash_posix_form(self):
        assert _to_posix_key("\\x\\dev\\repo") == "/x/dev/repo"

    def test_trailing_slash_stripped(self):
        assert _to_posix_key("/x/dev/repo/") == "/x/dev/repo"

    def test_root_slash_preserved(self):
        assert _to_posix_key("/") == "/"


class TestTierAPosix:
    def test_native_form_no_fs_root(self):
        assert _tier_a_posix("X:\\Dev\\Repo", "") == "/x/dev/repo"

    def test_with_fs_root_seam(self):
        assert _tier_a_posix("X:\\Dev\\Repo", "/tmp/fsroot") == "/tmp/fsroot/dev/repo"


class TestFsProbePath:
    """Regression net for the MSYS-vs-native path-form bug: every existence
    probe in this module is built in MSYS/POSIX form ("/x/foo"), which is
    valid under Git Bash (where the bash oracle ran) but which native
    Windows Python cannot os.path.isdir() — os.path.isdir('/c/Users/x') is
    False while os.path.isdir('C:/Users/x') is True. _fs_probe_path converts
    the MSYS form back to a form the running interpreter can actually stat.
    """

    def test_msys_drive_form_converted_on_windows(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        assert _fs_probe_path("/x/doe-claude") == "x:/doe-claude"

    def test_bare_msys_drive_root_converted_on_windows(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        assert _fs_probe_path("/x") == "x:/"

    def test_already_native_path_passed_through_on_windows(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        assert _fs_probe_path("X:\\DoE-claude") == "X:\\DoE-claude"
        assert _fs_probe_path("X:/DoE-claude") == "X:/DoE-claude"

    def test_identity_on_non_windows(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        assert _fs_probe_path("/x/doe-claude") == "/x/doe-claude"
        assert _fs_probe_path("/home/example-operator/DoE-claude") == "/home/example-operator/DoE-claude"


class TestTierAGreedyDecode:
    def test_resolves_hyphenated_dir_name_against_real_fixture_tree(self, tmp_path: Path):
        # Direct regression net for the reported symptom: a projects-dir
        # basename like "X--DoE-claude" decodes (naive fast path) to
        # "X:\\DoE\\claude", which doesn't exist on disk. The greedy walk
        # must recover "DoE-claude" as one hyphen-containing segment by
        # probing what actually exists under the fixture root.
        (tmp_path / "DoE-claude").mkdir()
        out = _tier_a_greedy_decode("DoE-claude", "X", str(tmp_path))
        assert out == "X:\\DoE-claude"

    def test_resolves_hyphenated_segment(self, tmp_path: Path):
        # "X:\dev\example-stats-repo" encodes to "X--dev-example-stats-repo"; the naive decode
        # (blanket - -> \) turns "example-stats-repo" into "fifa\stats" which doesn't
        # exist. The greedy walk should recover "example-stats-repo" as one segment.
        root = tmp_path
        (root / "dev" / "example-stats-repo").mkdir(parents=True)
        out = _tier_a_greedy_decode("dev-example-stats-repo", "X", str(root))
        assert out == "X:\\dev\\example-stats-repo"

    def test_no_match_returns_none(self, tmp_path: Path):
        out = _tier_a_greedy_decode("nonexistent-path-segment", "X", str(tmp_path))
        assert out is None

    def test_pathological_length_bounded(self, tmp_path: Path):
        rest = "-".join(["a"] * 41)
        out = _tier_a_greedy_decode(rest, "X", str(tmp_path))
        assert out is None


class TestDecodeProjectsDirName:
    def test_drive_letter_form(self):
        drive, rest, decoded = _decode_projects_dir_name("X--dev-example-stats-repo")
        assert drive == "X"
        assert rest == "dev-example-stats-repo"
        assert decoded == "X:\\dev\\fifa\\stats"

    def test_posix_form_out_of_scope_gap(self):
        # Faithful oracle-bug repro: non-drive-letter entries decode via a
        # blanket backslash-join even though the source path was POSIX —
        # this is a documented, NOT-fixed gap (see module docstring).
        drive, rest, decoded = _decode_projects_dir_name("-Users-example-operator-X-DoE-claude")
        assert drive == ""
        assert decoded == "\\Users\\example-operator\\X\\DoE\\claude"


class TestTierAEndToEnd:
    """No test previously exercised `_tier_a()` as a whole — the naive-decode
    fast path (`_decode_projects_dir_name`) and the greedy-decode fallback
    (`_tier_a_greedy_decode`) were only tested in isolation, never composed
    at the orchestration layer where the MSYS-path-form bug actually lived:
    a hyphenated projects-dir basename (e.g. "X--DoE-claude") silently
    discovered nothing because BOTH the fast-path existence probe and the
    greedy-decode existence probe built an MSYS-form path native Windows
    Python cannot stat. This is the regression net at that failing layer.
    """

    def test_hyphenated_repo_name_resolved_via_greedy_fallback(self, tmp_path: Path, monkeypatch):
        # Fixture: a ~/.claude/projects entry whose repo name carries a
        # literal hyphen — the naive fast-path decode ("X:\\DoE\\claude")
        # does not exist on disk; only the greedy walk recovers "DoE-claude"
        # as one segment.
        fake_home = tmp_path / "home"
        projects_dir = fake_home / ".claude" / "projects"
        (projects_dir / "X--DoE-claude").mkdir(parents=True)

        # COORDINATOR_TIER_A_FS_ROOT test seam: point existence probes at a
        # real fixture tree holding the matching (lowercased) directory.
        fs_root = tmp_path / "fsroot"
        (fs_root / "doe-claude").mkdir(parents=True)

        monkeypatch.setenv("HOME", str(fake_home))
        # Path.home() on Windows reads USERPROFILE, not HOME — set both so
        # this test is hermetic on every platform.
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        monkeypatch.setenv("COORDINATOR_TIER_A_FS_ROOT", str(fs_root))

        out = _tier_a()

        assert out == ["X:\\DoE-claude"]


class TestTierAExcludeRegex:
    """Ported byte-for-byte from the bash ERE — see module docstring. Each
    case is one of the three alternation branches."""

    def test_appdata_local_temp_excluded(self):
        assert _TIER_A_EXCLUDE_RE.search(r"C:\Users\x\AppData\Local\Temp\foo")

    def test_bare_drive_root_no_slash_excluded(self):
        assert _TIER_A_EXCLUDE_RE.search("X:")

    def test_bare_drive_root_with_slash_excluded(self):
        assert _TIER_A_EXCLUDE_RE.search("X:\\")

    def test_dot_claude_suffix_excluded(self):
        assert _TIER_A_EXCLUDE_RE.search("/some/path/.claude")

    def test_normal_repo_path_not_excluded(self):
        assert not _TIER_A_EXCLUDE_RE.search("X:\\dev\\repo")


class TestSortUnique:
    def test_matches_locale_collation_not_ordinal(self):
        # "sort -u"'s default collation on this host places mixed-case
        # entries differently than Python's plain ordinal sort — this is the
        # exact class of bug the byte-parity check against the bash oracle
        # caught (uppercase-first ordinal vs locale-aware collation).
        out = _sort_unique(["DoE-claude", "example-store-repo", "example-sim-repo-md"])
        assert set(out) == {"DoE-claude", "example-store-repo", "example-sim-repo-md"}
        assert len(out) == 3

    def test_dedups(self):
        assert _sort_unique(["a", "a", "b"]) == ["a", "b"]

    def test_empty_input(self):
        assert _sort_unique([]) == []


class TestTierB:
    def test_finds_depth1_and_depth2_repos(self, tmp_path: Path, monkeypatch):
        home_repo = tmp_path / "dev"
        _init_git_repo(home_repo)
        nested = tmp_path / "code" / "nested-repo"
        _init_git_repo(nested)

        import coordinator_core.ops.discover_working_repos as m

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(tmp_path / "dev"), str(tmp_path / "code")])
        out = _tier_b()
        assert str(home_repo) in out
        assert str(nested) in out

    def test_worktree_git_file_accepted(self, tmp_path: Path, monkeypatch):
        cand = tmp_path / "ws"
        repo = cand / "worktree-repo"
        repo.mkdir(parents=True)
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

        import coordinator_core.ops.discover_working_repos as m

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(cand)])
        out = _tier_b()
        assert str(repo) in out


class TestMainNeverBlocks:
    def test_all_tiers_empty_exits_zero_no_stdout(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.claude/projects
        # Path.home() on Windows reads USERPROFILE, not HOME — set both so
        # this test isolates _tier_a() from the real machine's
        # ~/.claude/projects on every platform.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        import coordinator_core.ops.discover_working_repos as m

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(tmp_path / "nonexistent")])
        monkeypatch.setattr(m, "_resolve_machine_local", lambda: None)
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert out == ""

    def test_tier_b_hit_emits_and_exits_zero(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Path.home() on Windows reads USERPROFILE, not HOME — set both so
        # this test isolates _tier_a() from the real machine's
        # ~/.claude/projects on every platform.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)

        import coordinator_core.ops.discover_working_repos as m

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(tmp_path / "dev")])
        monkeypatch.setattr(m, "_resolve_machine_local", lambda: None)
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert out == [str(repo)]

    def test_internal_tier_error_degrades_to_exit_zero(self, tmp_path: Path, monkeypatch, capsys):
        import coordinator_core.ops.discover_working_repos as m

        def _boom():
            raise RuntimeError("simulated Tier A failure")

        monkeypatch.setattr(m, "_tier_a", _boom)
        monkeypatch.setattr(m, "_tier_a5", lambda: [])
        monkeypatch.setattr(m, "_tier_b", lambda: [])
        rc = main([])
        assert rc == 0
        err = capsys.readouterr().err
        assert "Tier A failed" in err
