"""Tests for coordinator_core.install.substrate_migrate — Port A native
migration.

Covers AC A1-A6: the four per-file guards (incl. divergent -> fail-loud),
recursive machine-local walk with dotfiles + empty subdirs, setup/-NOT-
migrated, idempotent second run, --dry-run mutates-nothing, POSIX symlink
install, and a platform-gated/mocked Windows junction branch.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from coordinator_core.install import substrate_migrate as sm
from coordinator_core.testing import symlink_capability


# ---------------------------------------------------------------------------
# _copy_one_file — the four per-file guards (AC A1)
# ---------------------------------------------------------------------------


def test_copy_one_file_source_absent_is_noop(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    assert sm._copy_one_file(src, dst, check_only=False) == 0
    assert not dst.exists()


def test_copy_one_file_dest_absent_copies_with_parent_mkdir(tmp_path):
    # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is always
    # 0o666 there regardless of the os.chmod() argument. Assert the real
    # invariant under test — mode preserved across the copy — by comparing
    # against whatever mode chmod actually produced on this platform,
    # rather than asserting the literal POSIX octal.
    src = tmp_path / "src.txt"
    src.write_text("hello")
    os.chmod(src, 0o640)
    mode_before = stat.S_IMODE(src.stat().st_mode)
    dst = tmp_path / "nested" / "dir" / "dst.txt"

    assert sm._copy_one_file(src, dst, check_only=False) == 0
    assert dst.is_file()
    assert dst.read_text() == "hello"
    # shutil.copy2 preserves mode (mirrors `cp -p`).
    assert stat.S_IMODE(dst.stat().st_mode) == mode_before


def test_copy_one_file_both_present_identical_is_noop(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("same content")
    dst.write_text("same content")
    mtime_before = dst.stat().st_mtime

    assert sm._copy_one_file(src, dst, check_only=False) == 0
    assert dst.stat().st_mtime == mtime_before


def test_copy_one_file_both_present_divergent_fails_loud(tmp_path, capsys):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("source content")
    dst.write_text("dest content")

    rc = sm._copy_one_file(src, dst, check_only=False)

    assert rc == 1
    assert dst.read_text() == "dest content"  # unmodified — no silent winner-pick
    err = capsys.readouterr().err
    assert "DIVERGENT FILE" in err
    assert str(src) in err
    assert str(dst) in err


def test_copy_one_file_dry_run_mutates_nothing(tmp_path, capsys):
    src = tmp_path / "src.txt"
    src.write_text("hello")
    dst = tmp_path / "nested" / "dst.txt"

    rc = sm._copy_one_file(src, dst, check_only=True)

    # dst is absent -- a real run would copy it, so check-only fails loud
    # rather than silently reporting an always-green "would copy".
    assert rc == 1
    assert not dst.exists()
    assert not dst.parent.exists()
    err = capsys.readouterr().err
    assert "check failed" in err
    assert str(dst) in err


def test_copy_one_file_dry_run_identical_content_is_fresh(tmp_path, capsys):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("same content")
    dst.write_text("same content")

    rc = sm._copy_one_file(src, dst, check_only=True)

    assert rc == 0
    assert dst.read_text() == "same content"


# ---------------------------------------------------------------------------
# _migrate_tree — recursive walk incl. dotfiles + empty subdirs (AC A2)
# ---------------------------------------------------------------------------


def test_migrate_tree_source_absent_is_noop(tmp_path):
    src = tmp_path / "nope"
    dst = tmp_path / "dst"
    assert sm._migrate_tree(src, dst, check_only=False) == 0
    assert not dst.exists()


def test_migrate_tree_recursive_with_dotfiles_and_empty_subdirs(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "empty_sub").mkdir()
    (src / "sub" / "nested_empty").mkdir()
    (src / "top.toml").write_text("top")
    (src / ".hidden").write_text("dotfile")
    (src / "sub" / "child.toml").write_text("child")

    rc = sm._migrate_tree(src, dst, check_only=False)

    assert rc == 0
    assert (dst / "top.toml").read_text() == "top"
    assert (dst / ".hidden").read_text() == "dotfile"
    assert (dst / "sub" / "child.toml").read_text() == "child"
    # Empty subdirs are created even though a files-only walk would skip them.
    assert (dst / "empty_sub").is_dir()
    assert (dst / "sub" / "nested_empty").is_dir()


def test_migrate_tree_stops_on_divergent_file(tmp_path, capsys):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.toml").write_text("source-a")
    (dst / "a.toml").write_text("dest-a-different")
    (src / "b.toml").write_text("source-b")

    rc = sm._migrate_tree(src, dst, check_only=False)

    assert rc == 1
    err = capsys.readouterr().err
    assert "DIVERGENT FILE" in err


def test_migrate_tree_dry_run_mutates_nothing(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "empty_sub").mkdir()
    (src / "a.toml").write_text("content")
    dst = tmp_path / "dst"

    rc = sm._migrate_tree(src, dst, check_only=True)

    # dst is entirely absent -- a real run would populate it, so check-only
    # fails loud on the first missing file rather than silently reporting 0.
    assert rc == 1
    assert not dst.exists()
    err = capsys.readouterr().err
    assert "check failed" in err


def test_migrate_tree_dry_run_fails_loud_on_missing_empty_subdir_only(tmp_path, capsys):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.toml").write_text("content")
    (dst / "a.toml").write_text("content")  # files already migrated
    (src / "empty_sub").mkdir()  # dst has no matching empty subdir yet

    rc = sm._migrate_tree(src, dst, check_only=True)

    assert rc == 1
    assert not (dst / "empty_sub").exists()
    err = capsys.readouterr().err
    assert "check failed" in err
    assert "empty_sub" in err


# ---------------------------------------------------------------------------
# _install_compat_pointer — POSIX symlink + mocked Windows junction (AC A3)
# ---------------------------------------------------------------------------


def test_install_compat_pointer_posix_symlink(tmp_path, monkeypatch):
    from coordinator_core.install import substrate as substrate_mod

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "Darwin")
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    (legacy / "leftover.toml").write_text("x")
    dst.mkdir(parents=True)

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 0
    assert legacy.is_symlink()
    assert legacy.resolve() == dst.resolve()


def test_install_compat_pointer_dry_run_mutates_nothing_posix(tmp_path, monkeypatch, capsys):
    from coordinator_core.install import substrate as substrate_mod

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "Darwin")

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    dst.mkdir(parents=True)

    rc = sm._install_compat_pointer(legacy, dst, check_only=True)

    # legacy_ml is still a real dir -- a real run would replace it, so
    # check-only fails loud rather than reporting an always-green no-op.
    assert rc == 1
    assert legacy.is_dir() and not legacy.is_symlink()
    err = capsys.readouterr().err
    assert "check failed" in err
    assert "symlink" in err


def test_install_compat_pointer_windows_junction_mocked(tmp_path, monkeypatch):
    """Platform-gated Windows branch, exercised via mocks since this host is
    POSIX: uname reports MINGW, cygpath/mklink are mocked to succeed."""
    from coordinator_core.install import substrate as substrate_mod

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    dst.mkdir(parents=True)

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "MINGW64_NT")
    monkeypatch.setattr(sm.shutil, "which", lambda name: "/usr/bin/cygpath")
    monkeypatch.setattr(
        substrate_mod, "_cygpath_w", lambda p: p.replace("/", "\\")
    )
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    class _FakeProc:
        returncode = 0

    monkeypatch.setattr(substrate_mod, "_run", lambda argv, **kw: _FakeProc())

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 0
    # The real dir was removed to make way for the junction (mklink is mocked,
    # so no actual junction exists on this POSIX host — only the pre-junction
    # rmdir/rmtree side effect is observable).
    assert not legacy.is_dir() or legacy.is_symlink()


def test_install_compat_pointer_windows_cygpath_absent_fails_loud(tmp_path, monkeypatch, capsys):
    from coordinator_core.install import substrate as substrate_mod

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    dst.mkdir(parents=True)

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "MSYS_NT")
    monkeypatch.setattr(sm.shutil, "which", lambda name: None)

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 1
    assert legacy.is_dir()  # untouched on fail-loud
    err = capsys.readouterr().err
    assert "cygpath not found" in err


def test_install_compat_pointer_windows_mklink_failure_fails_loud(tmp_path, monkeypatch, capsys):
    from coordinator_core.install import substrate as substrate_mod

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    dst.mkdir(parents=True)

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "CYGWIN_NT")
    monkeypatch.setattr(sm.shutil, "which", lambda name: "/usr/bin/cygpath")
    monkeypatch.setattr(substrate_mod, "_cygpath_w", lambda p: p)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    class _FakeProc:
        returncode = 1

    monkeypatch.setattr(substrate_mod, "_run", lambda argv, **kw: _FakeProc())

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 1
    err = capsys.readouterr().err
    assert "mklink /J failed" in err


def test_install_compat_pointer_dry_run_windows_reports_junction(tmp_path, monkeypatch, capsys):
    from coordinator_core.install import substrate as substrate_mod

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    dst.mkdir(parents=True)

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "MINGW64_NT")

    rc = sm._install_compat_pointer(legacy, dst, check_only=True)

    assert rc == 1
    assert legacy.is_dir() and not legacy.is_symlink()
    err = capsys.readouterr().err
    assert "junction" in err


# ---------------------------------------------------------------------------
# migrate_substrate_to_settings_home — full end-to-end behavior (AC A1-A6)
# ---------------------------------------------------------------------------


def _mock_posix(monkeypatch):
    from coordinator_core.install import substrate as substrate_mod

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "Darwin")
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def test_full_migration_fresh_tree_nothing_to_migrate(tmp_path, capsys):
    claude_base = tmp_path / ".claude"
    claude_base.mkdir()
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to migrate" in out


def test_full_migration_copies_machine_local_and_manifest_and_installs_pointer(tmp_path, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("repos.foo = 1")
    (claude_base / "settings-manifest.md").write_text("# manifest")
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    assert (settings_home_path / "machine-local" / "registry.local.toml").read_text() == "repos.foo = 1"
    assert (settings_home_path / "settings-manifest.md").read_text() == "# manifest"
    assert ml.is_symlink()
    assert ml.resolve() == (settings_home_path / "machine-local").resolve()


def test_full_migration_does_not_migrate_setup(tmp_path, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    (claude_base / "machine-local").mkdir(parents=True)
    setup_dir = claude_base / "setup"
    setup_dir.mkdir()
    (setup_dir / "config.toml").write_text("setup content")
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    assert not (settings_home_path / "setup").exists()


def test_full_migration_divergent_manifest_fails_loud_and_stops(tmp_path, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    claude_base.mkdir()
    (claude_base / "settings-manifest.md").write_text("legacy content")
    settings_home_path = tmp_path / "settings-home"
    settings_home_path.mkdir()
    (settings_home_path / "settings-manifest.md").write_text("different content")

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 1
    # Not clobbered — fail-loud left the divergent dest untouched.
    assert (settings_home_path / "settings-manifest.md").read_text() == "different content"


def test_full_migration_idempotent_second_run_no_writes(tmp_path, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("x = 1")
    settings_home_path = tmp_path / "settings-home"

    rc1 = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)
    assert rc1 == 0
    assert ml.is_symlink()

    dst_file = settings_home_path / "machine-local" / "registry.local.toml"
    mtime_before = dst_file.stat().st_mtime

    rc2 = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc2 == 0
    assert ml.is_symlink()  # still a clean pointer, no data loss
    assert dst_file.stat().st_mtime == mtime_before  # no rewrite on second run


def test_full_migration_dry_run_mutates_nothing(tmp_path, monkeypatch, capsys):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("x = 1")
    (claude_base / "settings-manifest.md").write_text("# manifest")
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=True)

    # A fresh, never-migrated tree is a genuinely stale/not-yet-migrated
    # state -- dry-run now fails loud on the first unmigrated file rather
    # than silently reporting an always-green 0, matching the fail-loud-on-
    # stale-or-absent contract for install-time legs.
    assert rc == 1
    assert not settings_home_path.exists()
    assert ml.is_dir() and not ml.is_symlink()
    assert (claude_base / "settings-manifest.md").is_file()
    err = capsys.readouterr().err
    assert "check failed" in err


def test_full_migration_dry_run_reports_compat_pointer_stale_when_files_already_migrated(tmp_path, monkeypatch, capsys):
    """Isolated regression test: once every file under `machine-local/` is
    already migrated but the legacy real dir has not yet been replaced with
    the compat pointer, dry-run must surface THAT specific staleness (not
    silently report fresh) even though no file content is out of date."""
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("x = 1")
    settings_home_path = tmp_path / "settings-home"
    dst_ml = settings_home_path / "machine-local"
    dst_ml.mkdir(parents=True)
    (dst_ml / "registry.local.toml").write_text("x = 1")  # already migrated

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=True)

    assert rc == 1
    assert not ml.is_symlink()  # dry-run never writes the pointer
    err = capsys.readouterr().err
    assert f"{ml} is a real dir, not yet a symlink" in err
    assert str(dst_ml) in err


# ---------------------------------------------------------------------------
# is_pointer / _is_windows_junction — Finding 2 (AC A3/A4 Windows idempotence)
# ---------------------------------------------------------------------------


@symlink_capability.requires_symlink_capability
def test_is_pointer_true_for_posix_symlink(tmp_path):
    from coordinator_core.install._shared import is_pointer

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)

    assert is_pointer(link) is True


def test_is_pointer_false_for_real_dir(tmp_path):
    from coordinator_core.install._shared import is_pointer

    real = tmp_path / "real"
    real.mkdir()

    assert is_pointer(real) is False


def test_is_windows_junction_noop_on_non_windows(tmp_path, monkeypatch):
    """`_is_windows_junction` must short-circuit False on any non-Windows
    `os.name` (host is macOS here) without touching ctypes/os.path.isjunction
    at all — this is the fail-safe half of Finding 2's fix, exercised on the
    platform this suite actually runs on."""
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared.os, "name", "posix")
    real = tmp_path / "real"
    real.mkdir()

    assert _shared._is_windows_junction(str(real)) is False


def test_is_windows_junction_uses_os_path_isjunction_when_available(tmp_path, monkeypatch):
    """On the `os.name == "nt"` branch, prefer the stdlib `os.path.isjunction`
    (Python 3.12+) over the ctypes fallback when it's present."""
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared.os, "name", "nt")
    monkeypatch.setattr(_shared.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(_shared.os.path, "isjunction", lambda p: True, raising=False)

    assert _shared._is_windows_junction("C:\\fake\\junction") is True


def test_is_windows_junction_falls_back_to_ctypes_pre_312(tmp_path, monkeypatch):
    """When `os.path.isjunction` is absent (pre-3.12), fall through to the
    ctypes reparse-tag probe rather than silently reporting False."""
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared.os, "name", "nt")
    monkeypatch.setattr(_shared.os.path, "isdir", lambda p: True)
    monkeypatch.delattr(_shared.os.path, "isjunction", raising=False)

    called = {}

    def _fake_ctypes_probe(path):
        called["path"] = path
        return True

    monkeypatch.setattr(_shared, "_is_windows_junction_ctypes", _fake_ctypes_probe)

    assert _shared._is_windows_junction("C:\\fake\\junction") is True
    assert called["path"] == "C:\\fake\\junction"


# ---------------------------------------------------------------------------
# _install_compat_pointer — COORDINATOR_DISABLE_MACHINE_MUTATION coverage
# (Finding 1: the real-directory rmdir/rmtree delete legs were never gated)
# ---------------------------------------------------------------------------


def test_install_compat_pointer_posix_refuses_when_mutation_disabled(tmp_path, monkeypatch, capsys):
    from coordinator_core.install import substrate as substrate_mod

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "Darwin")
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    (legacy / "leftover.toml").write_text("x")
    dst.mkdir(parents=True)

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 1
    assert legacy.is_dir() and not legacy.is_symlink()
    assert (legacy / "leftover.toml").is_file()  # untouched — no rmtree happened
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "COORDINATOR_DISABLE_MACHINE_MUTATION" in err


def test_install_compat_pointer_posix_proceeds_when_mutation_enabled(tmp_path, monkeypatch):
    from coordinator_core.install import substrate as substrate_mod

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "Darwin")
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    dst.mkdir(parents=True)

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 0
    assert legacy.is_symlink()
    assert legacy.resolve() == dst.resolve()


def test_install_compat_pointer_windows_refuses_when_mutation_disabled(tmp_path, monkeypatch, capsys):
    """Windows-junction branch: the guard must fire ahead of the
    rmdir/rmtree fallback even though mklink itself is mocked."""
    from coordinator_core.install import substrate as substrate_mod

    legacy = tmp_path / "machine-local"
    dst = tmp_path / "settings-home" / "machine-local"
    legacy.mkdir()
    (legacy / "leftover.toml").write_text("x")
    dst.mkdir(parents=True)

    monkeypatch.setattr(substrate_mod, "_quiet_output", lambda argv, env=None: "MINGW64_NT")
    monkeypatch.setattr(sm.shutil, "which", lambda name: "/usr/bin/cygpath")
    monkeypatch.setattr(substrate_mod, "_cygpath_w", lambda p: p.replace("/", "\\"))

    class _FakeProc:
        returncode = 0

    monkeypatch.setattr(substrate_mod, "_run", lambda argv, **kw: _FakeProc())
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    rc = sm._install_compat_pointer(legacy, dst, check_only=False)

    assert rc == 1
    assert legacy.is_dir()
    assert (legacy / "leftover.toml").is_file()  # untouched — no rmdir/rmtree happened
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "COORDINATOR_DISABLE_MACHINE_MUTATION" in err


def test_full_migration_source_absent_manifest_and_ml_both_missing(tmp_path):
    claude_base = tmp_path / ".claude"
    claude_base.mkdir()
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    assert not settings_home_path.exists()


# ---------------------------------------------------------------------------
# resolution-journal wiring (C7 of docs/research/2026-08-06-install-receipt-
# persistence-design.md) — the _migrate_tree ShapedClause (clause index 1).
# ---------------------------------------------------------------------------


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def test_migrate_tree_clause_index_is_one():
    # `_MIGRATE_TREE_CLAUSE_INDEX` must track clause 2 (index 1) of
    # WRITE_SURFACE — the ShapedClause — not clause 1 (the StaticClause
    # manifest copy).
    from coordinator_core.install.write_surface import ShapedClause

    assert sm._MIGRATE_TREE_CLAUSE_INDEX == 1
    assert isinstance(sm.WRITE_SURFACE.clauses[sm._MIGRATE_TREE_CLAUSE_INDEX], ShapedClause)


def test_journal_records_actually_copied_files(tmp_path, _journal_env, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("x = 1")
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    journal = _journal_env.read_journal()
    resolution = journal["substrate-migrate"][sm._MIGRATE_TREE_CLAUSE_INDEX]
    paths = [e.path for e in resolution.entries]
    assert str(settings_home_path / "machine-local" / "registry.local.toml") in paths


def test_journal_records_empty_tuple_when_nothing_to_migrate(tmp_path, _journal_env):
    claude_base = tmp_path / ".claude"
    claude_base.mkdir()
    settings_home_path = tmp_path / "settings-home"

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 0
    journal = _journal_env.read_journal()
    resolution = journal["substrate-migrate"][sm._MIGRATE_TREE_CLAUSE_INDEX]
    assert resolution.entries == ()


def test_journal_never_written_on_check_only(tmp_path, _journal_env, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "registry.local.toml").write_text("x = 1")
    settings_home_path = tmp_path / "settings-home"

    sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=True)

    assert _journal_env.read_journal() == {}


def test_journal_records_partial_progress_on_divergent_abort(tmp_path, _journal_env, monkeypatch):
    _mock_posix(monkeypatch)
    claude_base = tmp_path / ".claude"
    ml = claude_base / "machine-local"
    ml.mkdir(parents=True)
    (ml / "a-first.toml").write_text("copied fine")
    (ml / "z-divergent.toml").write_text("source content")
    settings_home_path = tmp_path / "settings-home"
    dst_ml = settings_home_path / "machine-local"
    dst_ml.mkdir(parents=True)
    (dst_ml / "z-divergent.toml").write_text("different dest content")

    rc = sm.migrate_substrate_to_settings_home(claude_base, settings_home_path, check_only=False)

    assert rc == 1
    journal = _journal_env.read_journal()
    resolution = journal["substrate-migrate"][sm._MIGRATE_TREE_CLAUSE_INDEX]
    paths = [e.path for e in resolution.entries]
    # Only the file that genuinely copied before the divergent abort is
    # journaled — never a phantom entry for the divergent file itself.
    assert str(dst_ml / "a-first.toml") in paths
    assert str(dst_ml / "z-divergent.toml") not in paths
