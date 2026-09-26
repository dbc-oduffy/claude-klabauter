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
    _emit_form,
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
from coordinator_core.win_portability import no_console_passthrough_kwargs

# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-q", "-b", "main"],
        check=False,
        timeout=30,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.com"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True, timeout=30, **no_console_passthrough_kwargs())
    (path / "f").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, timeout=30, **no_console_passthrough_kwargs())


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
        out = list(_gate_and_dedup([str(repo), str(nonrepo), str(scratch)], set()))
        assert out == [_emit_form(str(repo))]

    def test_cross_form_dedup_trailing_slash(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        out = list(_gate_and_dedup([str(repo), str(repo) + "/", str(repo)], set()))
        assert len(out) == 1
        assert out[0] == _emit_form(str(repo))

    def test_empty_lines_skipped(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        out = list(_gate_and_dedup(["", str(repo), ""], set()))
        assert out == [_emit_form(str(repo))]

    def test_emitted_form_is_forward_slashed_regardless_of_input_form(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)
        native = str(repo)
        forward = native.replace(chr(92), "/")

        out = list(_gate_and_dedup([native, forward], set()))

        assert out == [forward]
        assert chr(92) not in out[0]

    def test_publish_mirror_is_never_emitted(self, tmp_path: Path):
        repo = tmp_path / "dev" / "realrepo"
        mirror = tmp_path / "dev" / "oss-mirror"
        _init_git_repo(repo)
        _init_git_repo(mirror)

        out = list(_gate_and_dedup([str(repo), str(mirror)], {_to_posix_key(str(mirror))}))

        assert out == [_emit_form(str(repo))]

    def test_mirror_match_is_separator_and_drive_case_insensitive(self, tmp_path: Path):
        mirror = tmp_path / "dev" / "oss-mirror"
        _init_git_repo(mirror)
        native = str(mirror)
        forward = native.replace(chr(92), "/")

        assert list(_gate_and_dedup([native], {_to_posix_key(forward)})) == []


class TestToPosixKey:
    def test_native_drive_form(self):
        assert _to_posix_key("X:\\dev\\repo") == "/x/dev/repo"

    def test_posix_form_drive(self):
        assert _to_posix_key("X:/dev/repo") == "/x/dev/repo"

    def test_lowercases_drive_only(self):
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
        (tmp_path / "DoE-claude").mkdir()
        out = _tier_a_greedy_decode("DoE-claude", "X", str(tmp_path))
        assert out == "X:\\DoE-claude"

    def test_resolves_hyphenated_segment(self, tmp_path: Path):
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
        drive, rest, decoded = _decode_projects_dir_name("-Users-example-operator-X-DoE-claude")
        assert drive == ""
        assert decoded == "\\Users\\example-operator\\X\\DoE\\claude"


class TestTierAEndToEnd:

    def test_hyphenated_repo_name_resolved_via_greedy_fallback(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        projects_dir = fake_home / ".claude" / "projects"
        (projects_dir / "X--DoE-claude").mkdir(parents=True)

        # COORDINATOR_TIER_A_FS_ROOT test seam: point existence probes at a
        fs_root = tmp_path / "fsroot"
        (fs_root / "doe-claude").mkdir(parents=True)

        monkeypatch.setenv("HOME", str(fake_home))
        # Path.home() on Windows reads USERPROFILE, not HOME — set both so
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        monkeypatch.setenv("COORDINATOR_TIER_A_FS_ROOT", str(fs_root))

        out = _tier_a()

        assert out == ["X:\\DoE-claude"]


class TestTierAExcludeRegex:

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


class TestTierA5EnvOverride:
    """`_merged_flat_registry` only merges
    the two registry TOML files and never consulted the per-key
    `MACHINE_LOCAL_<KEY>` env override rung that `machine_resolver.
    registry_get` honors, a silent behaviour loss vs. the old `machine-local
    get <key>` call site. Pins that `_tier_a5` now honors the override."""

    def test_env_override_redirects_a_discovered_repo(self, tmp_path: Path, monkeypatch):
        import coordinator_core.ops.discover_working_repos as m

        registered = tmp_path / "dev" / "registered-repo"
        _init_git_repo(registered)
        overridden = tmp_path / "dev" / "overridden-repo"
        _init_git_repo(overridden)

        reg_dir = tmp_path / "machine-local"
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "registry.toml").write_text(f'"repos.sibling" = "{registered.as_posix()}"\n')
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_SIBLING", str(overridden))

        out = m._tier_a5()
        assert out == [str(overridden)]

    def test_no_override_uses_registry_value(self, tmp_path: Path, monkeypatch):
        import coordinator_core.ops.discover_working_repos as m

        registered = tmp_path / "dev" / "registered-repo"
        _init_git_repo(registered)

        reg_dir = tmp_path / "machine-local"
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "registry.toml").write_text(f'"repos.sibling" = "{registered.as_posix()}"\n')
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

        out = m._tier_a5()
        assert out == [registered.as_posix()]


class TestMainNeverBlocks:
    def test_all_tiers_empty_exits_zero_no_stdout(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Path.home() on Windows reads USERPROFILE, not HOME — set both so
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        import coordinator_core.ops.discover_working_repos as m

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(tmp_path / "nonexistent")])
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-registry-dir"))
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert out == ""

    def test_tier_b_hit_emits_and_exits_zero(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Path.home() on Windows reads USERPROFILE, not HOME — set both so
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        repo = tmp_path / "dev" / "realrepo"
        _init_git_repo(repo)

        import coordinator_core.ops.discover_working_repos as m

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(tmp_path / "dev")])
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-registry-dir"))
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert out == [_emit_form(str(repo))]

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
