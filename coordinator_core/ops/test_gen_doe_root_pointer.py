"""Characterization + parity tests for coordinator_core.ops.gen_doe_root_pointer.

Spec backlink: DoE-claude:pln-coordinator-maximalist-install-e73afa § C1
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from coordinator_core import trusted_root_guard
from coordinator_core.ops import gen_doe_root_pointer
from coordinator_core.ops.gen_doe_root_pointer import main
from coordinator_core.testing.fake_machine_local import write_fake_machine_local
from coordinator_core.testing.home_sandbox import sandbox_home


def _make_doe_root(tmp_path: Path, name: str = "doe-clone") -> Path:
    root = tmp_path / name
    (root / "coordinator").mkdir(parents=True)
    return root


def _make_fake_machine_local(tmp_path: Path, value: str, rc: int = 0) -> Path:
    """Write a fake `machine-local` executable that echoes `value` for `get repos.doe_claude`.

    Returns the bin dir (caller puts it on PATH) -- resolution goes through
    `shutil.which("machine-local")` in `gen_doe_root_pointer.py`, which honours
    Windows PATHEXT and finds the `.cmd` launcher this writes on that platform.
    See `coordinator_core.testing.fake_machine_local` for why a plain POSIX
    shebang script can't be resolved/exec'd cross-platform.
    """
    bin_dir = tmp_path / "fakebin"
    python_body = (
        "import sys\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == 'get' and sys.argv[2] == 'repos.doe_claude':\n"
        f"    sys.stdout.write({value!r})\n"
        f"    sys.exit({rc})\n"
        "sys.exit(9)\n"
    )
    write_fake_machine_local(bin_dir, python_body)
    return bin_dir


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own HOME/PATH sandbox, VERIFIED before the test body runs.

    The sandbox goes through `sandbox_home` rather than a bare
    `setenv("HOME", ...)`: the code under test resolves its target via
    `os.path.expanduser("~")`, which on Windows prefers `USERPROFILE` and
    ignores `HOME` entirely — so the HOME-only form left this docstring true on
    POSIX and false on Windows, and the suite wrote pytest tmpdirs into the real
    `~/.claude/.doe-root` on three machines (2026-07-20 memos from
    claude-central-em and example-cockpit-repo-em).

    A monkeypatched env var that fails to redirect `expanduser` is exactly how
    that bug survived undetected — the sandbox silently no-oped and the tests
    still passed (they never assert against the real home, only against the
    path they *believe* is sandboxed). So this fixture asserts its own
    precondition: `expanduser("~")` MUST resolve under `tmp_path` before any
    test body runs, on every platform. A regression here fails loud in the
    fixture, not silently in production.
    """
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    home = sandbox_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # Strip PATH so a real machine-local (if installed on the dev box) never leaks in.
    monkeypatch.setenv("PATH", "")

    resolved_home = Path(os.path.expanduser("~")).resolve()
    expected_root = tmp_path.resolve()
    assert resolved_home == home.resolve(), (
        "home-sandbox isolation did not take effect: os.path.expanduser('~') "
        f"resolved to {resolved_home!s}, expected the sandbox {home!s}. This "
        "would mean the test is about to read/write the REAL home directory "
        "instead of tmp_path — refusing to proceed."
    )
    assert expected_root in resolved_home.parents or resolved_home == expected_root, (
        f"sandboxed home {resolved_home!s} escaped tmp_path {expected_root!s}"
    )
    return home


def _pointer_path(home):
    r"""Where the generator writes the pointer: `<settings-home>/machine-local/.doe-root`.

    It used to be `<home>/.claude/.doe-root`. That lived inside the git-TRACKED
    `~/.claude` meta-repo, which syncs between machines, so each machine committed
    its own absolute clone path over the previous machine's — a Mac writing
    `/Users/...` and a Windows box writing `X:\...` fought over one tracked file,
    and the loser mis-resolved silently. Since `trusted_root_guard._doe_root`
    consumes this pointer, a foreign-OS value there made the trust anchor reject
    the real clone and abort the installer.

    Not a new rung: `_doe_root` already ranked this path as rung 2 (durable mirror)
    ABOVE `~/.claude/.doe-root` at rung 3, which it labels the legacy fallback. Only
    the writer was still on the legacy location.
    """
    return Path(home) / ".coordinator-claude-settings" / "machine-local" / ".doe-root"


# ---------------------------------------------------------------------------
# Env-override tier (REPO_DOE_CLAUDE)
# ---------------------------------------------------------------------------


def test_env_override_writes_pointer(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main([])

    assert rc == 0
    pointer = _pointer_path(_isolated_env)
    assert pointer.read_text() == f"{doe_root}\n"


def test_env_override_strips_trailing_slash(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root) + "/")

    rc = main([])

    assert rc == 0
    pointer = _pointer_path(_isolated_env)
    assert pointer.read_text() == f"{doe_root}\n"


def test_nonexistent_root_fails_loud(tmp_path, monkeypatch, capsys, _isolated_env):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path / "does-not-exist"))

    rc = main([])

    assert rc == 1
    assert "resolved root not found" in capsys.readouterr().err
    assert not (_pointer_path(_isolated_env)).exists()


def test_root_missing_coordinator_subdir_fails_loud(tmp_path, monkeypatch, capsys, _isolated_env):
    bare_root = tmp_path / "bare-clone"
    bare_root.mkdir()
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(bare_root))

    rc = main([])

    assert rc == 1
    assert "coordinator/ subdir absent" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# machine-local registry tier
# ---------------------------------------------------------------------------


def test_machine_local_registry_resolves_root(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    bin_dir = _make_fake_machine_local(tmp_path, str(doe_root))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/bin" + os.pathsep + "/usr/bin")

    rc = main([])

    assert rc == 0
    pointer = _pointer_path(_isolated_env)
    assert pointer.read_text() == f"{doe_root}\n"


def test_machine_local_absent_fails_loud(capsys, _isolated_env):
    rc = main([])

    assert rc == 1
    assert "machine-local not found" in capsys.readouterr().err


def test_machine_local_get_nonzero_exit_fails_loud(tmp_path, monkeypatch, capsys, _isolated_env):
    bin_dir = _make_fake_machine_local(tmp_path, "", rc=1)
    monkeypatch.setenv("PATH", str(bin_dir))

    rc = main([])

    assert rc == 1
    assert "machine-local get repos.doe_claude failed" in capsys.readouterr().err


def test_machine_local_empty_value_fails_loud(tmp_path, monkeypatch, capsys, _isolated_env):
    bin_dir = _make_fake_machine_local(tmp_path, "")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/bin" + os.pathsep + "/usr/bin")

    rc = main([])

    assert rc == 1
    assert "repos.doe_claude is unset in the registry" in capsys.readouterr().err


def test_env_override_wins_over_registry(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path, "env-root")
    other_root = _make_doe_root(tmp_path, "registry-root")
    bin_dir = _make_fake_machine_local(tmp_path, str(other_root))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main([])

    assert rc == 0
    pointer = _pointer_path(_isolated_env)
    assert pointer.read_text() == f"{doe_root}\n"


# ---------------------------------------------------------------------------
# --check-only dry-run safety
# ---------------------------------------------------------------------------


def test_check_only_does_not_write_live_pointer(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main(["--check-only"])

    # No live pointer exists yet -- a real run would create it, so
    # --check-only now fails loud rather than reporting an always-green 0.
    assert rc == 1
    assert not (_pointer_path(_isolated_env)).exists()
    err = capsys.readouterr().err
    assert "--check-only FAILED" in err
    assert str(doe_root) in err


def test_check_only_fresh_pointer_is_no_op(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    assert main([]) == 0
    capsys.readouterr()

    rc = main(["--check-only"])

    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_check_only_leaves_existing_pointer_byte_unchanged(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    other_root = _make_doe_root(tmp_path, "other-root")
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    # Seed a live pointer with a DIFFERENT value first.
    rc = main([])
    assert rc == 0
    pointer = _pointer_path(_isolated_env)
    original_bytes = pointer.read_bytes()

    # Now run --check-only against a DIFFERENT root -- must not mutate the
    # live file, but MUST fail loud (pointer is stale relative to this run).
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(other_root))
    rc = main(["--check-only"])

    assert rc == 1
    assert pointer.read_bytes() == original_bytes


def test_check_only_fails_on_missing_root(tmp_path, monkeypatch, capsys, _isolated_env):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path / "nope"))

    rc = main(["--check-only"])

    assert rc == 1
    assert "resolved root not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_run_is_idempotent_noop(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc1 = main([])
    assert rc1 == 0
    pointer = _pointer_path(_isolated_env)
    mtime1 = pointer.stat().st_mtime_ns
    content1 = pointer.read_bytes()
    capsys.readouterr()  # drain rc1's "wrote ..." message before asserting rc2 is silent

    rc2 = main([])
    assert rc2 == 0
    # No output on the idempotent no-op path (mirrors the bash oracle: silent skip).
    assert capsys.readouterr().err == ""
    assert pointer.read_bytes() == content1
    # mtime may or may not tick depending on filesystem resolution; content identity
    # (not rewritten) is the load-bearing assertion, verified via no-op stderr above.
    assert pointer.stat().st_mtime_ns >= mtime1


def test_pointer_refreshed_when_root_changes(tmp_path, monkeypatch, _isolated_env):
    doe_root_a = _make_doe_root(tmp_path, "root-a")
    doe_root_b = _make_doe_root(tmp_path, "root-b")
    pointer = _pointer_path(_isolated_env)

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root_a))
    assert main([]) == 0
    assert pointer.read_text() == f"{doe_root_a}\n"

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root_b))
    assert main([]) == 0
    assert pointer.read_text() == f"{doe_root_b}\n"


# ---------------------------------------------------------------------------
# Settings-home precedence (COORDINATOR_SETTINGS_HOME / CLAUDE_HOME)
#
# The pointer target now follows the SETTINGS-HOME chain, not the claude-config
# chain: COORDINATOR_SETTINGS_HOME, falling back to CLAUDE_HOME, falling back to
# the platform home directory (Path.home() — USERPROFILE on Windows, HOME or the
# passwd entry on POSIX), with `.coordinator-claude-settings` appended, then
# `/machine-local/.doe-root`. Until 2026-07-28 the target was
# `${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME}/.claude}/.doe-root` — inside the
# git-synced `~/.claude` tree, where `.doe-root` was a TRACKED file, so a
# Windows-written pointer overwrote the macOS one and vice versa.
# `CLAUDE_CONFIG_DIR` no longer participates — see `_pointer_path` for why the
# write moved off the tracked `~/.claude` tree. These tests pin the never-synced
# settings-home target and the removal of the old precedence.
# ---------------------------------------------------------------------------


def test_claude_config_dir_no_longer_affects_pointer_location(tmp_path, monkeypatch, _isolated_env):
    """``CLAUDE_CONFIG_DIR`` used to select the pointer's directory. It must now be
    inert: it names the *claude config* tree (tracked, cross-machine synced), which
    is exactly the tree this pointer was moved out of. A machine setting it must not
    drag the pointer back onto the synced surface."""
    doe_root = _make_doe_root(tmp_path)
    config_dir = tmp_path / "explicit-config-dir"
    config_dir.mkdir()
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    rc = main([])

    assert rc == 0
    assert _pointer_path(_isolated_env).read_text() == f"{doe_root}\n"
    assert not (config_dir / ".doe-root").exists()


def test_settings_home_override_selects_pointer_location(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    settings_home = tmp_path / "alt-settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    rc = main([])

    assert rc == 0
    pointer = settings_home / "machine-local" / ".doe-root"
    assert pointer.read_text() == f"{doe_root}\n"


def test_claude_home_used_when_settings_home_unset(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    claude_home = tmp_path / "alt-claude-home"
    claude_home.mkdir()
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    rc = main([])

    assert rc == 0
    assert _pointer_path(claude_home).read_text() == f"{doe_root}\n"


def test_never_writes_the_synced_legacy_target(tmp_path, monkeypatch, _isolated_env):
    """No dual-write — a legacy copy would preserve the cross-machine clobber."""
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    assert main([]) == 0
    assert not (Path(_isolated_env) / ".claude" / ".doe-root").exists()


def test_creates_machine_local_dir_when_absent(tmp_path, monkeypatch, _isolated_env):
    """Fresh machine: settings-home/machine-local need not pre-exist."""
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    assert not _pointer_path(_isolated_env).parent.exists()

    assert main([]) == 0
    assert _pointer_path(_isolated_env).is_file()


# ---------------------------------------------------------------------------
# Write/read seam agreement (ported from the pythonw-shim branch at the
# 2026-07-28 consolidation — the generator and `trusted_root_guard._doe_root`
# must not drift apart independently).
# ---------------------------------------------------------------------------


def test_generator_writes_where_settings_home_resolver_points(tmp_path, monkeypatch, _isolated_env):
    """The pointer file's directory must be derived from the SAME
    `_settings_home_dir_from_env` helper the resolver uses -- not from an
    independently-hand-rolled join. If either side's join logic drifts from
    the other, this fails without needing to know today's literal path."""
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main([])

    assert rc == 0
    settings_home_dir = trusted_root_guard._settings_home_dir_from_env(dict(os.environ))
    assert settings_home_dir, "resolver could not determine a settings-home dir from this env"
    expected_pointer = Path(settings_home_dir) / "machine-local" / ".doe-root"
    assert expected_pointer.read_text() == f"{doe_root}\n"


def test_stale_legacy_pointer_does_not_shadow_fresh_write(tmp_path, monkeypatch, _isolated_env):
    """The exact multi-machine failure mode: `~/.claude/.doe-root` is git-tracked
    and syncs between machines, so a foreign/stale value can land there (e.g. a
    `git pull` bringing in another machine's absolute clone path) independently of
    anything this generator does locally.

    Seed a stale, foreign-looking value at the LEGACY location, run the generator
    for THIS machine's real root, then confirm the reader resolves to the fresh
    value -- never the stale legacy one.
    """
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    assert main([]) == 0

    legacy_pointer = _isolated_env / ".claude" / ".doe-root"
    legacy_pointer.parent.mkdir(parents=True, exist_ok=True)
    legacy_pointer.write_text("/Users/someone-else/DoE-claude\n")

    resolved = trusted_root_guard._doe_root(dict(os.environ))
    assert resolved == str(doe_root)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_unknown_argument_is_silently_ignored(tmp_path, monkeypatch, _isolated_env):
    """2026-07-23 M3/D9 collapse: unrecognized argv tokens (e.g. a caller
    forwarding a whole ``${ARGUMENTS}`` blob containing ``--non-interactive``/
    ``--reconfigure``) must not fail this generator — matches the sibling
    ``register_coordinator_mirror`` pass-through-tolerant convention. This
    supersedes the prior strict fail-loud contract."""
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main(["--bogus", "--non-interactive"])

    assert rc == 0
    pointer = _pointer_path(_isolated_env)
    assert pointer.read_text() == f"{doe_root}\n"


def test_help_prints_usage_and_exits_zero(capsys, _isolated_env):
    rc = main(["--help"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "Usage:" in err
    assert "--check-only" in err


def test_help_short_flag(capsys, _isolated_env):
    rc = main(["-h"])

    assert rc == 0
    assert "Usage:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --graceful-skip-unresolved + contract status rows (2026-07-23 M3/D9)
# ---------------------------------------------------------------------------


def test_graceful_skip_unresolved_exits_zero_with_skip_row(capsys, _isolated_env):
    rc = main(["--graceful-skip-unresolved"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "doe_root_pointer: skipped (repos.doe_claude not resolved" in out


def test_graceful_skip_unresolved_does_not_mask_check_only_failure(tmp_path, monkeypatch, capsys, _isolated_env):
    """--graceful-skip-unresolved only softens the LIVE unresolved path, never
    --check-only's own fail-loud validation contract."""
    rc = main(["--check-only", "--graceful-skip-unresolved"])

    assert rc == 1
    assert "machine-local not found" in capsys.readouterr().err


def test_written_row_on_fresh_write(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "doe_root_pointer: written (" in out


def test_ready_noop_row_on_second_run(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    assert main([]) == 0
    capsys.readouterr()

    rc = main([])
    assert rc == 0
    assert "doe_root_pointer: ready (no-op)" in capsys.readouterr().out


def test_would_write_row_under_check_only(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))

    rc = main(["--check-only"])

    assert rc == 1
    assert "doe_root_pointer: check failed:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# F1 -- Windows-shaped --check-only temp-dir resolution (2026-07-28 dispatch).
#
# Prior code: `tempfile.mkstemp(dir=os.environ.get("TMPDIR", "/tmp"))`. On
# Windows TMPDIR is unset and "/tmp" is not a real directory, so `mkstemp`
# raised an uncaught FileNotFoundError. The fix routes through
# `tempfile.gettempdir()` (honours TMPDIR/TEMP/TMP per-platform) instead of
# ever constructing a hardcoded POSIX "/tmp" literal.
# ---------------------------------------------------------------------------


def test_check_only_temp_dir_never_hardcodes_posix_tmp_literal(tmp_path, monkeypatch, _isolated_env):
    """Windows-shaped: TMPDIR unset, and tempfile.gettempdir() redirected to a
    sandboxed directory that is NOT "/tmp" -- the --check-only mkstemp() call
    must be made against the sandboxed gettempdir() result, never against a
    hardcoded "/tmp" fallback (which would raise FileNotFoundError on real
    Windows regardless of whether "/tmp" happens to exist on this dev box).
    """
    import coordinator_core.ops.gen_doe_root_pointer as mod

    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)

    sandboxed_tmp = tmp_path / "windows-shaped-temp"
    sandboxed_tmp.mkdir()
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(sandboxed_tmp))

    seen_dirs = []
    real_mkstemp = mod.tempfile.mkstemp

    def _spy_mkstemp(*args, **kwargs):
        seen_dirs.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(mod.tempfile, "mkstemp", _spy_mkstemp)

    rc = mod.main(["--check-only"])

    # Fresh pointer + no prior value on disk -- check-only reports "would
    # write, not written" (rc=1); the load-bearing assertion here is WHERE
    # mkstemp was told to write, not the reported rc (see
    # test_check_only_does_not_write_live_pointer for that contract).
    assert rc == 1
    assert seen_dirs == [str(sandboxed_tmp)]
    assert seen_dirs[0] != "/tmp"


def test_dual_seed_written_row(tmp_path, monkeypatch, capsys, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    bin_dir = tmp_path / "fakebin2"
    write_fake_machine_local(
        bin_dir,
        "import sys\n"
        "if len(sys.argv) >= 2 and sys.argv[1] == 'get':\n"
        "    sys.exit(1)\n"
        "if len(sys.argv) >= 2 and sys.argv[1] == 'set':\n"
        "    sys.exit(0)\n"
        "sys.exit(9)\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"plugin_mirror_source_path: written ({doe_root})" in out


# ---------------------------------------------------------------------------
# MSYS/Cygwin mount-form repair (Windows only)
#
# Regression cover for the bug this generator sat in the middle of: a DoE clone
# root resolved under Git-Bash arrives as `/x/DoE-claude`, and every consumer of
# the pointer this writes is a native-Windows process, which reads that leading
# `/x/` as drive-relative `X:\x\DoE-claude` — a path that does not exist. Both
# resolution tiers are covered because both can carry the mount form: the env
# override comes straight from a shell, and the registry tier must not assume
# its upstream normalized for it.
# ---------------------------------------------------------------------------


def _mount_form(path: Path) -> "tuple[str, str]":
    """Render `path` as its MSYS mount form. Returns (mount_form, native_form)."""
    native = path.as_posix()  # 'C:/Users/.../doe-clone'
    drive, rest = native[0], native[2:]
    return f"/{drive.lower()}{rest}", f"{drive.upper()}:{rest}"


@pytest.mark.skipif(os.name != "nt", reason="mount-form repair is os.name=='nt'-gated by design")
def test_env_override_msys_mount_form_is_repaired(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    mount_form, native_form = _mount_form(doe_root)
    monkeypatch.setenv("REPO_DOE_CLAUDE", mount_form)

    rc = main([])

    assert rc == 0
    assert _pointer_path(_isolated_env).read_text() == f"{native_form}\n"


@pytest.mark.skipif(os.name != "nt", reason="mount-form repair is os.name=='nt'-gated by design")
def test_registry_msys_mount_form_is_repaired(tmp_path, monkeypatch, _isolated_env):
    doe_root = _make_doe_root(tmp_path)
    mount_form, native_form = _mount_form(doe_root)
    bin_dir = _make_fake_machine_local(tmp_path, mount_form)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    rc = main([])

    assert rc == 0
    assert _pointer_path(_isolated_env).read_text() == f"{native_form}\n"


class TestLivePointerWriteRefusal:
    """`gen_doe_root_pointer.main` is the ONLY writer of
    `<settings-home>/machine-local/.doe-root`, and nothing stopped it writing the
    OPERATOR'S real pointer from inside a test -- so a test could leave a pytest
    tmpdir path there, which under concurrency hands a peer session a pointer into
    a deleted directory.

    Bug: state/bug-backlog/2026-08-26-a-test-writes-the-live-claude-machine-lo-
    6cdf6bc87771.yaml.

    The refusal is gated on the target being OUTSIDE the OS temp dir. Inside it,
    the write is sandbox-redirected and must still work -- the 34 sibling tests in
    this module are that assertion, and an earlier version of this guard that
    refused unconditionally on the kill switch broke 18 of them.
    """

    def test_env_name_matches_substrate_definition_site(self):
        """The kill-switch name is spelled in two places (import cost, § its
        comment in the module). Pin them equal so it cannot drift into a dead
        switch that silently never fires."""
        from coordinator_core.install import substrate

        assert (
            gen_doe_root_pointer._MUTATION_DISABLE_ENV
            == substrate._MACHINE_MUTATION_DISABLE_ENV
        )

    def test_temp_rooted_target_is_always_allowed(self, tmp_path, monkeypatch):
        """The sandbox-redirected shape proceeds even with the kill switch set --
        this is what the 18-test regression taught."""
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        target = tmp_path / "settings" / "machine-local" / ".doe-root"

        assert gen_doe_root_pointer._refuse_live_pointer_write(str(target)) is None

    def test_refuses_a_non_temp_target_under_pytest(self, monkeypatch):
        """The real defect: a live target reached from inside a test. Uses a
        synthetic path so the assertion never depends on -- or touches -- the
        operator's actual pointer."""
        monkeypatch.delenv("COORDINATOR_ALLOW_LIVE_DOE_ROOT_WRITE", raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic::nodeid")
        live_shaped = os.path.join(
            os.sep, "Users", "someone", ".coordinator-claude-settings",
            "machine-local", ".doe-root",
        )

        reason = gen_doe_root_pointer._refuse_live_pointer_write(live_shaped)

        assert reason is not None
        assert "outside the OS temp dir" in reason
        assert "COORDINATOR_ALLOW_LIVE_DOE_ROOT_WRITE" in reason

    def test_real_home_shaped_escape_is_closed(self, monkeypatch):
        """`@pytest.mark.real_home` returns EARLY from the quarantine fixture,
        before it sets the kill switch -- so a marked test has the real home and
        no switch. Simulated here by clearing the switch: the pytest trigger must
        still refuse, since it does not depend on the fixture having run."""
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
        monkeypatch.delenv("COORDINATOR_ALLOW_LIVE_DOE_ROOT_WRITE", raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic::nodeid")
        live_shaped = os.path.join(os.sep, "opt", "live-settings", "machine-local", ".doe-root")

        assert gen_doe_root_pointer._refuse_live_pointer_write(live_shaped) is not None

    def test_named_opt_in_reopens_the_write(self, monkeypatch):
        """A test that genuinely must write the live pointer asks for it by name,
        rather than inheriting the permission from a marker meaning 'read the
        real home'."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic::nodeid")
        monkeypatch.setenv("COORDINATOR_ALLOW_LIVE_DOE_ROOT_WRITE", "1")
        live_shaped = os.path.join(os.sep, "opt", "live-settings", "machine-local", ".doe-root")

        assert gen_doe_root_pointer._refuse_live_pointer_write(live_shaped) is None

    def test_operator_kill_switch_still_refuses_outside_temp(self, monkeypatch):
        """Trigger 2, with no pytest in the picture: the operator-facing switch
        `install.substrate` already honours now covers this writer too."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("COORDINATOR_ALLOW_LIVE_DOE_ROOT_WRITE", raising=False)
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        live_shaped = os.path.join(os.sep, "opt", "live-settings", "machine-local", ".doe-root")

        reason = gen_doe_root_pointer._refuse_live_pointer_write(live_shaped)

        assert reason == "COORDINATOR_DISABLE_MACHINE_MUTATION=1"

    def test_ordinary_operator_install_is_untouched(self, monkeypatch):
        """The path that must keep working: a real install, no pytest, no switch."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
        monkeypatch.delenv("COORDINATOR_ALLOW_LIVE_DOE_ROOT_WRITE", raising=False)
        live_shaped = os.path.join(os.sep, "opt", "live-settings", "machine-local", ".doe-root")

        assert gen_doe_root_pointer._refuse_live_pointer_write(live_shaped) is None
