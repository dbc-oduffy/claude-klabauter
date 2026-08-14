"""
coordinator_core.install.test_forwarder_trust_guard — resolve-claude-klabauter-bin
contract tests for the ``_write_agent_forwarder`` naked-Python forwarder
template (coordinator_core/install/substrate.py) and its co-located
``_resolve_claude_klabauter.py`` ladder module (coordinator/lib/resolve-claude-klabauter/).

``b644d5a9`` (DoE, 2026-07-22) relocated DoE-claude's entire executable
surface into claude-klabauter's own ``coordinator/bin/`` — the prior forwarder
template still exec'd the now-empty DoE-side tree and the old
``.doe-root``/``CLAUDE_PLUGIN_ROOT`` trust-prefix guard this file used to
test no longer exists (see ``_write_agent_forwarder``'s docstring for why
that trust posture was deliberately NOT carried forward). This file's
filename is kept for git-history continuity across the port; its content is
a full replacement covering the resolve-claude-klabauter-bin ladder instead.

M1 (forwarder-ladder extraction): the ladder moved out of the emitted
forwarder body and into a single shared ``_resolve_claude_klabauter.py`` module,
installed alongside every forwarder. ``_write_forwarder`` below now also
copies the real on-disk ``_resolve_claude_klabauter.py`` into the same tmp_path
directory as the forwarder, so the subprocess exec below finds it via the
forwarder's own ``sys.path.insert(0, <own dir>)`` — exactly mirroring how
the real installer co-locates the two files in bin_dst.

Every test writes the real emitted forwarder body (+ the real
_resolve_claude_klabauter.py module) to disk and executes the forwarder as a
subprocess against a fixture ``machine-local/`` + fake claude-klabauter-root tree —
not a substring check on the generated source — so a regression in the
actual resolution/exec behavior fails these tests even if the template text
still "looks right".

Spec backlink:
    DoE-claude coordinator/snippets/resolve-claude-klabauter-bin.md (DoE commit ad7fb0d1)
    cross-repo/inbox/2026-07-22-claude-central-em-forwarder-template-still-execs-dead-doe-bin.md
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.install.substrate import (
    _LEGACY_CMD_MARKER,
    SubstrateFatalError,
    _agent_cmd_dest_name,
    _derive_agent_helper_names,
    _derive_agent_helper_target_map,
    _prune_orphaned_static_bin_names,
    _read_bin_manifest,
    _resolve_agent_cmd_dest_collisions,
    _static_bin_family_names,
    _sweep_orphaned_agent_helpers,
    _write_agent_cmd_forwarder,
    _write_agent_forwarder,
    _write_bin_manifest,
)

@pytest.fixture(autouse=True)
def _allow_machine_mutation_in_tmp_path(monkeypatch):
    """This file's `_sweep_orphaned_agent_helpers`/`_prune_orphaned_static_bin_names`
    coverage writes/deletes real files entirely within `tmp_path`, and those two
    call sites pass `check_temp_path=False` to `_refuse_machine_mutation` (they are
    plain filesystem mutations the test sandbox correctly redirects, not the
    machine-state class trigger 2 exists for -- see that parameter's docstring).
    That leaves trigger 1, `COORDINATOR_DISABLE_MACHINE_MUTATION=1`, which
    `coordinator_core/conftest.py::_quarantine_real_home` sets SUITE-WIDE as a
    belt-and-braces opt-out -- unset it locally here so this file's own delete
    assertions aren't masked by that unrelated incident guard. Does not touch the
    conftest-wide default itself."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


_RESOLVE_CLAUDE_KLABAUTER_SRC = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)
_BIN_TEMPLATES_MANIFEST_SRC = (
    Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "bin-templates-manifest.py"
)


def _write_forwarder(tmp_path: Path, name: str = "cross-repo-memo", target: str | None = None) -> Path:
    """`target` defaults to `name` — the identity case that holds for every
    extensionless CLI. Callers exercising a name/target divergence (any
    `.py`-suffixed CLI whose installed name is stripped) MUST pass it
    explicitly, mirroring `_install_bin_resolvers`, which always sources it
    from `_derive_agent_helper_target_map`."""
    dst = tmp_path / name
    _write_agent_forwarder(name=name, dst=dst, check_only=False, target=target or name)
    # Co-locate the real shared ladder module — the forwarder body imports
    # it via a sys.path insert of its own directory, mirroring the real
    # installer's bin_dst layout.
    shutil.copyfile(_RESOLVE_CLAUDE_KLABAUTER_SRC, tmp_path / "_resolve_claude_klabauter.py")
    return dst


def _make_claude_klabauter_fixture(root: Path, target_name: str = "cross-repo-memo", sentinel_executable: bool = True) -> None:
    """Build a fixture claude-klabauter root: coordinator/bin/ with an executable
    archive-stamp-cli sentinel and a target CLI stub that echoes a
    distinctive sentinel string to stdout, so a test can positively prove
    exec was reached rather than merely asserting an error string absent.

    The target is written as a Python script, not a `#!/bin/sh` script:
    ``2dea51482`` ("exec_cli: converge the POSIX leg onto
    interpreter-targeted execv", 2026-07-31) deliberately made the POSIX
    ``exec_cli`` leg always run targets as `python target_path` (every
    `coordinator/bin/` target is a Python CLI), so a shell-script fixture
    now fails with a Python `SyntaxError` instead of exercising the
    resolution ladder under test. The sentinel itself is only probed for
    existence/exec-bit, never exec'd as a target, so it stays a shell
    script."""
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    sentinel = bin_dir / "archive-stamp-cli"
    sentinel.write_text("#!/bin/sh\necho SENTINEL\n", encoding="utf-8")
    sentinel.chmod(0o755 if sentinel_executable else 0o644)
    if sentinel_executable and os.name == "nt":
        # `_resolve_claude_klabauter.py`'s Windows-side executability probe checks
        # PATHEXT extensions, not stat mode bits (NTFS has neither) — the
        # extensionless POSIX sentinel above is invisible to it. The real
        # on-disk archive-stamp-cli ships an actual `.cmd` companion
        # (coordinator/bin/archive-stamp-cli.cmd) for exactly this reason;
        # mirror that shape here rather than a bare chmod, which is a
        # silent no-op on Windows (os.chmod cannot set S_IXUSR/GRP/OTH on
        # NTFS — verified empirically).
        (bin_dir / "archive-stamp-cli.cmd").write_text("@echo SENTINEL\r\n", encoding="utf-8")
    target = bin_dir / target_name
    target.write_text(f'print("TARGET_REACHED_{target_name}")\n', encoding="utf-8")
    target.chmod(0o755)


def _run(forwarder: Path, ml_dir: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    # POSIX: exec the forwarder directly, exercising its own `#!/usr/bin/env
    # python3` shebang + exec bit — the real invocation shape a Unix shell
    # uses. Windows: `CreateProcess` never interprets a `#!` line (WinError
    # 193, "%1 is not a valid Win32 application" if invoked directly), and
    # NTFS has no exec bit for `os.chmod` to set in the first place — the
    # REAL Windows invocation path is always through the co-located `.cmd`
    # twin (`_write_agent_cmd_forwarder`), which resolves an interpreter and
    # runs `<interpreter> <forwarder>` explicitly. Emulate that real
    # mechanism here rather than a direct exec Windows has no equivalent of.
    if os.name == "nt":
        argv = [sys.executable, str(forwarder)]
    else:
        os.chmod(forwarder, 0o755)
        argv = [str(forwarder)]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MACHINE_LOCAL_REGISTRY_DIR": str(ml_dir),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, **no_console_creationflags()
    )


def test_registry_rung_wins_over_sentinel(tmp_path: Path):
    """Rung 1 (registry.local.toml key) must be preferred even when rung 2
    (the .claude-klabauter-root sentinel) also resolves — to a DIFFERENT, non-fixture
    path that would fail if it were the one actually used."""
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    registry_root = tmp_path / "registry-claude-klabauter"
    _make_claude_klabauter_fixture(registry_root)
    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{registry_root}\'\n', encoding="utf-8"
    )
    (ml_dir / ".claude-klabauter-root").write_text(str(tmp_path / "sentinel-claude-klabauter-nonexistent"), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 0
    assert "TARGET_REACHED_cross-repo-memo" in result.stdout


def test_sentinel_only_resolves(tmp_path: Path):
    """With no registry.local.toml at all, the .claude-klabauter-root sentinel alone
    must resolve — the documented fallback for a machine provisioned by the
    older convention."""
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    sentinel_root = tmp_path / "sentinel-claude-klabauter"
    _make_claude_klabauter_fixture(sentinel_root)
    (ml_dir / ".claude-klabauter-root").write_text(str(sentinel_root), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 0
    assert "TARGET_REACHED_cross-repo-memo" in result.stdout


def test_missing_both_fails_loud(tmp_path: Path):
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    result = _run(forwarder, ml_dir)

    assert result.returncode == 1
    assert "cannot resolve claude-klabauter" in result.stderr
    assert "machine-local set repos.claude_klabauter" in result.stderr


def test_traversal_segment_rejected(tmp_path: Path):
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    (ml_dir / ".claude-klabauter-root").write_text(str(tmp_path / "some" / ".." / "claude-klabauter"), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 1
    assert "traversal segment" in result.stderr


def test_root_resolved_but_coordinator_bin_missing_message(tmp_path: Path):
    """repo-root resolved but coordinator/bin/ missing = wrong/incomplete
    checkout — must produce a DISTINCT message from the sentinel-missing
    case below, not a generic 'not found'."""
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    incomplete_root = tmp_path / "incomplete-claude-klabauter"
    incomplete_root.mkdir()
    (ml_dir / ".claude-klabauter-root").write_text(str(incomplete_root), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 1
    assert "has no coordinator/bin/ directory" in result.stderr
    assert "stale or partial" not in result.stderr


def test_coordinator_bin_present_but_sentinel_absent_message(tmp_path: Path):
    """coordinator/bin/ exists but archive-stamp-cli is absent = stale/partial
    migration — distinct message from the root-incomplete case above."""
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    stale_root = tmp_path / "stale-claude-klabauter"
    (stale_root / "coordinator" / "bin").mkdir(parents=True)
    (ml_dir / ".claude-klabauter-root").write_text(str(stale_root), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 1
    assert "stale or partial claude-klabauter migration" in result.stderr
    assert "has no coordinator/bin/ directory" not in result.stderr


def test_non_executable_sentinel_rejected(tmp_path: Path):
    """The sentinel probe checks the exec bit, not mere existence — a
    present-but-non-executable archive-stamp-cli must still be rejected."""
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    root = tmp_path / "non-exec-sentinel-claude-klabauter"
    _make_claude_klabauter_fixture(root, sentinel_executable=False)
    (ml_dir / ".claude-klabauter-root").write_text(str(root), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 1
    assert "stale or partial claude-klabauter migration" in result.stderr


def test_py_suffixed_cli_forwarder_execs_py_target(tmp_path: Path):
    """A `.py`-suffixed CLI's installed name is stem-stripped, but the
    forwarder must exec the real on-disk `.py` target — the extensionless
    installed name never exists on disk. (Formerly exercised via the pinned
    `mint-deliverable-id.sh` -> `.py` divergence before that CLI's installed
    name was made extensionless; now a generic `.py`-suffixed CLI shape.)"""
    forwarder = _write_forwarder(tmp_path, name="wsc-close", target="wsc-close.py")
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    root = tmp_path / "wsc-close-claude-klabauter"
    _make_claude_klabauter_fixture(root, target_name="wsc-close.py")
    (ml_dir / ".claude-klabauter-root").write_text(str(root), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 0
    assert "TARGET_REACHED_wsc-close.py" in result.stdout
    content = forwarder.read_text(encoding="utf-8")
    assert 'exec_cli("wsc-close.py")' in content
    assert 'exec_cli("wsc-close")' not in content


def test_write_agent_cmd_forwarder_targets_installed_name_not_stem(tmp_path: Path):
    """The generated `.cmd` body must invoke `%~dp0<installed-name>` — the
    co-located Unix-half forwarder's OWN filename — never the stem-stripped
    `.cmd` destination name and never the claude-klabauter-side `.py` target. This is
    the divergent-installed-name case: an installed name that carries its
    own suffix (e.g. a hypothetical `foo.sh` — the shape the retired
    `mint-deliverable-id.sh` divergence used to exercise) installs the Unix
    half as `foo.sh`, the `.cmd` twin as `foo.cmd`, but the `.cmd` body must
    still target `%~dp0foo.sh`."""
    dst = tmp_path / "foo.cmd"
    _write_agent_cmd_forwarder(
        "foo.sh", dst, False, python3_cmd_resolved_bin="",
    )
    content = dst.read_text(encoding="utf-8")
    assert '"%~dp0foo.sh"' in content
    assert "foo.py" not in content
    assert "foo.cmd" not in content


def test_write_agent_cmd_forwarder_substitutes_resolved_interpreter(tmp_path: Path):
    """Unlike the retired copy-a-source-.cmd approach (whose `__PYTHON_BIN__`
    placeholder was never substituted for agent-helper `.cmd`s), the
    generator does the substitution for real at generation time."""
    dst = tmp_path / "cross-repo-memo.cmd"
    _write_agent_cmd_forwarder(
        "cross-repo-memo", dst, False,
        python3_cmd_resolved_bin=r"C:\Python311\python.exe",
    )
    content = dst.read_text(encoding="utf-8")
    assert r'set "_py=C:\Python311\python.exe"' in content
    assert "__PYTHON_BIN__" not in content


def test_write_agent_cmd_forwarder_empty_resolved_bin_falls_through(tmp_path: Path):
    """An empty resolved bin (nothing bakeable at install time) must fall
    through to the `where python.exe` / `py -3` ladder rungs, not emit a
    quoted empty-string exec that would blow up as `"" "%~dp0..."`."""
    dst = tmp_path / "cross-repo-memo.cmd"
    _write_agent_cmd_forwarder(
        "cross-repo-memo", dst, False, python3_cmd_resolved_bin="",
    )
    content = dst.read_text(encoding="utf-8")
    assert 'set "_py="' in content
    assert "where python.exe" in content
    assert "py -3" in content
    assert "exit /b 127" in content


def test_write_agent_cmd_forwarder_check_only_does_not_write(tmp_path: Path):
    dst = tmp_path / "cross-repo-memo.cmd"
    # dst is absent -- a real run would write it, so check-only now fails
    # loud rather than silently no-op-ing.
    with pytest.raises(SubstrateFatalError, match="absent"):
        _write_agent_cmd_forwarder(
            "cross-repo-memo", dst, True, python3_cmd_resolved_bin="",
        )
    assert not dst.exists()


def test_write_agent_cmd_forwarder_check_only_fresh_is_no_op(tmp_path: Path):
    dst = tmp_path / "cross-repo-memo.cmd"
    _write_agent_cmd_forwarder("cross-repo-memo", dst, False, python3_cmd_resolved_bin="")
    before = dst.read_text(encoding="utf-8")

    _write_agent_cmd_forwarder("cross-repo-memo", dst, True, python3_cmd_resolved_bin="")

    assert dst.read_text(encoding="utf-8") == before


def test_forwarder_missing_target_exits_127_without_traceback(tmp_path: Path):
    """A resolved, valid coordinator/bin/ whose target CLI is absent must
    fail cleanly. Regression for the sh->Python port: a bare `os.execv`
    surfaces a FileNotFoundError traceback and exit 1; this forwarder must
    exit 127 (POSIX command-not-found) with a one-line remediation instead.

    ``2dea51482`` (2026-07-31) added an explicit `os.path.isfile`/`os.access`
    pre-check ahead of `exec_cli`'s POSIX leg that emits "... is missing —
    re-run coordinator:install to repair the plugin tree" for a genuinely
    absent target, reserving "missing or not executable" for the narrower
    `OSError` path reached only once exec is actually attempted.
    """
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    root = tmp_path / "missing-target-claude-klabauter"
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    sentinel = bin_dir / "archive-stamp-cli"
    sentinel.write_text("#!/bin/sh\necho SENTINEL\n", encoding="utf-8")
    sentinel.chmod(0o755)
    if os.name == "nt":
        # See `_make_claude_klabauter_fixture`'s matching comment: the Windows-side
        # executability probe is PATHEXT-based, not stat-mode-based.
        (bin_dir / "archive-stamp-cli.cmd").write_text("@echo SENTINEL\r\n", encoding="utf-8")
    # cross-repo-memo itself deliberately absent from bin_dir.
    (ml_dir / ".claude-klabauter-root").write_text(str(root), encoding="utf-8")

    result = _run(forwarder, ml_dir)

    assert result.returncode == 127, (
        f"missing target must exit 127 (POSIX command-not-found), got {result.returncode}"
    )
    assert "Traceback" not in result.stderr
    assert "is missing" in result.stderr
    assert "coordinator:install" in result.stderr


# ---------------------------------------------------------------------------
# AC7 parity: the INSTALLED `.cmd` sibling's filename must never carry the
# installed name's own extension (a naive f"{name}.cmd" would malform e.g.
# `foo.sh.cmd` for an installed name that carries its own suffix — the shape
# the retired, asymmetric `mint-deliverable-id.sh` divergence used to
# exercise before that CLI's installed name was made extensionless; every
# other current installed name is extensionless, so the bug is invisible
# without a synthetic suffixed name to probe it).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("coordinator-doc-new", "coordinator-doc-new.cmd"),
        ("cross-repo-memo", "cross-repo-memo.cmd"),
        ("foo.sh", "foo.cmd"),
        ("claude-doe", "claude-doe.cmd"),
    ],
)
def test_agent_cmd_dest_name_never_double_suffixes(name: str, expected: str):
    assert _agent_cmd_dest_name(name) == expected


# ---------------------------------------------------------------------------
# _derive_agent_helper_names — replaces the former hand-maintained
# _AGENT_HELPER_NAMES tuple with a derivation over coordinator/bin/'s own
# directory listing.
# ---------------------------------------------------------------------------


def _touch(path: Path, executable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stub", encoding="utf-8")
    if executable:
        path.chmod(0o755)


def test_derive_agent_helper_names_basic_triplet_dedup(tmp_path: Path):
    """A `<name>.py` + `<name>.cmd` (+ `.ps1`) triplet collapses to ONE
    installed name (the .py-stripped stem) — the same triplet-handling
    shape mint-deliverable-id.py/.cmd exercises, generalized to every CLI
    in the directory."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "coordinator-doc-new.py")
    _touch(agent_bin / "coordinator-doc-new.cmd", executable=False)
    _touch(agent_bin / "coordinator-doc-new.ps1", executable=False)

    names = _derive_agent_helper_names(agent_bin)

    assert names.count("coordinator-doc-new") == 1
    assert "coordinator-doc-new.cmd" not in names
    assert "coordinator-doc-new.ps1" not in names


def test_derive_agent_helper_names_extensionless_polyglot_kept_verbatim(tmp_path: Path):
    """Extensionless polyglots (claude-doe, verify-coverage) keep their
    bareword name — there is no suffix to strip."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "claude-doe")
    _touch(agent_bin / "verify-coverage")
    _touch(agent_bin / "verify-coverage.cmd", executable=False)

    names = _derive_agent_helper_names(agent_bin)

    assert "claude-doe" in names
    assert "verify-coverage" in names


def test_derive_agent_helper_names_excludes_private_dirs_and_test_files(tmp_path: Path):
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "_queue_append_locator.py", executable=False)
    _touch(agent_bin / "lib" / "cli_shared.py")
    _touch(agent_bin / "coordinator-tasks-mirror.py")
    _touch(agent_bin / "test_coordinator_tasks_mirror.py")
    _touch(agent_bin / ".wsc-inline-budget-baseline", executable=False)
    _touch(agent_bin / "doctor-probes.toml", executable=False)

    names = _derive_agent_helper_names(agent_bin)

    assert "_queue_append_locator" not in names
    assert not any("cli_shared" in n for n in names)
    assert "coordinator-tasks-mirror" in names
    assert "coordinator-tasks-mirror.test" not in names
    assert not any(n.endswith(".test.py") for n in names)
    assert ".wsc-inline-budget-baseline" not in names
    assert "doctor-probes" not in names
    assert "doctor-probes.toml" not in names
    # The `test_coordinator_tasks_mirror.py` fixture above was already
    # present here but unasserted, which is exactly how the pytest-form
    # escape went unnoticed — see the dedicated test below.
    assert "test_coordinator_tasks_mirror" not in names


def test_derive_agent_helper_names_excludes_pytest_collection_artifacts(tmp_path: Path):
    """Regression, 2026-07-29: `coordinator/bin/` was wired into pytest's
    `testpaths` on 2026-07-25 and now holds ~42 `test_*.py` modules plus a
    tree-wide `conftest.py`. The derivation's only test-file exclusion was
    the `<cli>.test.py` companion convention, so every pytest module derived
    a forwarder and shipped as a bareword PATH entry — including `conftest`,
    a name with no `main()` at all — and 40 of them were seeded into
    `docs/install/bin-inventory.json` as tracked oracles.

    A CLI whose name legitimately starts with `test-` (hyphen — the CLI
    convention in this tree) must still derive: the rule is pytest's
    filename convention, not a "looks like a test" heuristic."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "conftest.py", executable=False)
    _touch(agent_bin / "test_sweep_boot.py", executable=False)
    _touch(agent_bin / "test_sweep_boot.cmd", executable=False)
    _touch(agent_bin / "test-machine-path-leak.py")
    _touch(agent_bin / "sweep-boot.py")

    names = _derive_agent_helper_names(agent_bin)

    assert "conftest" not in names
    assert "test_sweep_boot" not in names
    assert not any(n.startswith("test_") for n in names)
    assert "test-machine-path-leak" in names
    assert "sweep-boot" in names


def test_derive_agent_helper_names_excludes_reserved_names(tmp_path: Path):
    """machine-local/claude-home/resolve-coordinator-clone/etc. are already
    installed by ml_family/ch_family from a different source — if the raw
    scan finds a same-named file in coordinator/bin/ (it does, for at least
    machine-local and claude-home), it must NOT surface in the derived set,
    or the agent-helper loop would silently overwrite the real install with
    a resolve-claude-klabauter-bin forwarder stub."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "machine-local")
    _touch(agent_bin / "machine-local.cmd", executable=False)
    _touch(agent_bin / "claude-home")
    _touch(agent_bin / "claude-home.cmd", executable=False)
    _touch(agent_bin / "cross-repo-memo")

    names = _derive_agent_helper_names(agent_bin)

    assert "machine-local" not in names
    assert "claude-home" not in names
    assert "cross-repo-memo" in names


def test_derive_agent_helper_names_mint_deliverable_id_stem_stripped(tmp_path: Path):
    """coordinator/bin/ ships mint-deliverable-id.py + .cmd — the derived
    set must surface the ordinary stem-stripped, extensionless installed
    name `mint-deliverable-id`, exactly like every other `.py`-suffixed CLI
    (the former pinned-`.sh`-divergence special case has been retired)."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "mint-deliverable-id.py")
    _touch(agent_bin / "mint-deliverable-id.cmd", executable=False)

    names = _derive_agent_helper_names(agent_bin)

    assert "mint-deliverable-id" in names
    assert "mint-deliverable-id.sh" not in names


def test_derive_agent_helper_names_missing_agent_bin_returns_empty(tmp_path: Path):
    assert _derive_agent_helper_names(tmp_path / "nonexistent") == ()


# ---------------------------------------------------------------------------
# _derive_agent_helper_target_map + _write_agent_forwarder(target=...) —
# regression for the rc=127 fresh-install break: every .py-suffixed CLI got
# a forwarder execing the extensionless (nonexistent) installed name,
# because _write_agent_forwarder used to re-derive its exec target from the
# installed name verbatim instead of the real on-disk filename. See
# cross-repo/inbox/2026-07-23-claude-central-em-claude-klabauter-
# pickup-assemble-heads-up.md § 0.
# ---------------------------------------------------------------------------


def test_derive_agent_helper_target_map_py_suffixed_cli_targets_real_file(tmp_path: Path):
    """A `.py`-suffixed CLI's installed name is `.py`-stripped, but the
    target-map value must stay the REAL on-disk filename (with `.py`) — not
    the stripped installed name, which does not exist on disk."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "wsc-close.py")
    _touch(agent_bin / "wsc-close.cmd", executable=False)

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping["wsc-close"] == "wsc-close.py"
    assert (agent_bin / mapping["wsc-close"]).is_file()


def test_derive_agent_helper_target_map_extensionless_cli_targets_itself(tmp_path: Path):
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "claude-doe")

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping["claude-doe"] == "claude-doe"
    assert (agent_bin / mapping["claude-doe"]).is_file()


def test_derive_agent_helper_target_map_extensionless_and_py_twin_prefers_py(
    tmp_path: Path, capsys: pytest.CaptureFixture,
):
    """An extensionless CLI (`foo`) and its `.py` twin (`foo.py`) both derive
    installed name `"foo"` -- this is the one collision shape with an
    explicit precedence rule (the .py twin wins) rather than a fatal error,
    because it WAS realized in the live tree (aggregate-chain-loe,
    audit-roadmap -- both since deduped at source; see
    `test_derive_agent_helper_target_map_real_live_tree_succeeds`). The rule
    is retained as a guard against the next occurrence of this shape. Must
    emit a visible warning naming both files and the winner, so a live
    duplicate stays noisy rather than becoming invisible."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "foo")
    _touch(agent_bin / "foo.py")

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping["foo"] == "foo.py"
    warning = capsys.readouterr().out
    assert "foo" in warning
    assert "foo.py" in warning


def test_derive_agent_helper_target_map_non_py_suffix_does_not_collide(tmp_path: Path):
    """`foo.py` and `foo.sh` do NOT collide -- `.sh` is not `.py`, so its
    installed name is its own literal filename (`"foo.sh"`), never the
    stripped stem `"foo"`. Documents the boundary of the collision guard:
    given this map's key derivation (`.py`-suffix stem-strips, every other
    suffix keeps the literal name), the *only* two on-disk filenames that
    can ever derive the same installed name are an exact `<X>` / `<X>.py`
    pair -- so the fatal branch in the scan loop, while real defense-in-
    depth against a future change to that derivation, has no other
    currently-reachable trigger shape to assert against."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "foo.py")
    _touch(agent_bin / "foo.sh")

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping["foo"] == "foo.py"
    assert mapping["foo.sh"] == "foo.sh"


def test_derive_agent_helper_target_map_real_live_tree_succeeds():
    """Regression for the live-tree break: `_derive_agent_helper_target_map`
    must succeed (not raise) against the REAL `coordinator/bin/`. Fixture-only
    coverage is exactly what let the fatal guard ship broken -- this runs
    against actual disk, not a synthetic tree.

    Historically (until the dedupe-at-source fix) this tree carried two live
    extensionless-vs-`.py`-twin collisions (`aggregate-chain-loe`,
    `audit-roadmap`) that exercised the precedence-rule branch in
    `_derive_agent_helper_target_map`. Both were deduped at source -- the
    extensionless sibling deleted, the `.py` twin kept -- so there is no
    longer a live collision anywhere on disk; the collision-resolution branch
    is now exercised only by the synthetic fixture above
    (`test_derive_agent_helper_target_map_extensionless_and_py_twin_prefers_py`).
    This test asserts the current, non-colliding reality: both installed
    names still resolve to their `.py` file via the ordinary (single-file,
    non-colliding) stem-strip path, and neither extensionless sibling exists
    on disk any more."""
    agent_bin = Path(__file__).resolve().parents[2] / "coordinator" / "bin"

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping
    assert mapping["aggregate-chain-loe"] == "aggregate-chain-loe.py"
    assert mapping["audit-roadmap"] == "audit-roadmap.py"
    assert not (agent_bin / "aggregate-chain-loe").exists()
    assert not (agent_bin / "audit-roadmap").exists()


def test_derive_agent_helper_target_map_publish_namespaced_alias_additive():
    """D1 (2026-07-25-posix-bareword-path-provisioning.md, PM-ratified
    2026-07-26): `publish` was the sole bare generic verb in the installed
    forwarder set. `coordinator-publish.py` (`coordinator/bin/`) installs a
    namespaced `coordinator-*` alias ADDITIVELY -- the bare `publish` name
    must keep resolving too, since `coordinator/skills/percolate/SKILL.md`
    (DoE-claude) still invokes it by that bareword. This is a real-tree
    regression test: both installed names must be present and each must
    resolve to its own distinct on-disk file, never collide."""
    agent_bin = Path(__file__).resolve().parents[2] / "coordinator" / "bin"

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping["publish"] == "publish.py"
    assert mapping["coordinator-publish"] == "coordinator-publish.py"
    assert (agent_bin / "publish.py").exists()
    assert (agent_bin / "coordinator-publish.py").exists()


def test_derive_agent_helper_target_map_mint_deliverable_id_stem_stripped(tmp_path: Path):
    """The former pinned-`.sh`-divergence special case is retired —
    mint-deliverable-id.py now resolves via the ordinary stem-strip path,
    same as any other `.py`-suffixed CLI."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "mint-deliverable-id.py")
    _touch(agent_bin / "mint-deliverable-id.cmd", executable=False)

    mapping = _derive_agent_helper_target_map(agent_bin)

    assert mapping["mint-deliverable-id"] == "mint-deliverable-id.py"
    assert "mint-deliverable-id.sh" not in mapping


def test_derive_agent_helper_target_map_keys_match_derive_names(tmp_path: Path):
    """The two derivations must never drift apart — same installed-name set,
    just one also exposing the resolved target."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "wsc-close.py")
    _touch(agent_bin / "wsc-close.cmd", executable=False)
    _touch(agent_bin / "claude-doe")
    _touch(agent_bin / "mint-deliverable-id.py")
    _touch(agent_bin / "mint-deliverable-id.cmd", executable=False)

    names = _derive_agent_helper_names(agent_bin)
    mapping = _derive_agent_helper_target_map(agent_bin)

    assert set(names) == set(mapping.keys())


def test_write_agent_forwarder_explicit_target_round_trip_all_kinds(tmp_path: Path):
    """Full round-trip over a fixture coordinator/bin/ containing a
    .py-suffixed CLI, an extensionless CLI, and a second .py-suffixed CLI
    (mint-deliverable-id.py, no longer a pinned-divergence special case):
    for every installed name, generate a forwarder
    with the EXPLICIT target from _derive_agent_helper_target_map (the shape
    the real install loop in _install_bin_resolvers uses) and assert the
    emitted forwarder's exec_cli(...) argument names a file that actually
    EXISTS on disk in the fixture coordinator/bin/."""
    agent_bin = tmp_path / "coordinator" / "bin"
    _touch(agent_bin / "wsc-close.py")
    _touch(agent_bin / "wsc-close.cmd", executable=False)
    _touch(agent_bin / "review-brightline-gate.py")
    _touch(agent_bin / "claude-doe")
    _touch(agent_bin / "mint-deliverable-id.py")
    _touch(agent_bin / "mint-deliverable-id.cmd", executable=False)

    mapping = _derive_agent_helper_target_map(agent_bin)
    out_dir = tmp_path / "bin_dst"
    out_dir.mkdir()

    for name, target in mapping.items():
        dst = out_dir / name
        _write_agent_forwarder(name, dst, check_only=False, target=target)
        content = dst.read_text(encoding="utf-8")
        assert f'exec_cli("{target}")' in content
        assert (agent_bin / target).is_file(), (
            f"forwarder for installed name {name!r} execs {target!r}, "
            f"which does not exist in the fixture coordinator/bin/"
        )


def test_write_agent_forwarder_target_is_required_keyword_only(tmp_path: Path):
    """`target` is deliberately non-defaultable: the only available default
    would be the installed-name re-derivation that produced the rc=127
    ceremony-spine break in the first place, so a caller that forgets it
    must fail loudly at call time rather than silently emit a forwarder
    execing a nonexistent extensionless path."""
    with pytest.raises(TypeError):
        _write_agent_forwarder("wsc-close", tmp_path / "wsc-close", check_only=False)


def test_forwarder_trailing_slash_and_crlf_normalized(tmp_path: Path):
    """Windows-first-class: a sentinel file written with a trailing slash
    and CRLF line ending must still resolve — mirrors the prior template's
    deliberate `\\r\\n` (not just `\\n`) stripping."""
    forwarder = _write_forwarder(tmp_path)
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    root = tmp_path / "crlf-claude-klabauter"
    _make_claude_klabauter_fixture(root)
    (ml_dir / ".claude-klabauter-root").write_bytes((str(root) + "/\r\n").encode("utf-8"))

    result = _run(forwarder, ml_dir)

    assert result.returncode == 0
    assert "TARGET_REACHED_cross-repo-memo" in result.stdout


# ---------------------------------------------------------------------------
# _resolve_agent_cmd_dest_collisions — _agent_cmd_dest_name strips the
# installed name's own extension, so distinct installed names can collapse
# onto the same .cmd destination filename (e.g. `render-handoff-tracker`
# and `render-handoff-tracker.js`). Unconditional .cmd generation (every
# forwarder now gets one, see _write_agent_cmd_forwarder's docstring) turns
# that from latent into active — this resolver's non-.js-wins precedence,
# and its fail-loud on anything the precedence doesn't resolve, is what
# keeps the collision from silently landing as an installer-iteration-
# order-dependent overwrite.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "colliding_pair",
    [
        ("emit-artifact-shape-contract", "emit-artifact-shape-contract.js"),
        ("normalize-consumed-frontmatter", "normalize-consumed-frontmatter.js"),
        ("refresh-queries", "refresh-queries.js"),
        ("render-handoff-tracker", "render-handoff-tracker.js"),
    ],
)
def test_resolve_agent_cmd_dest_collisions_non_js_wins(colliding_pair: "tuple[str, str]"):
    """The four real live-tree collisions (verified 2026-07-23, 291 entries)
    all take this exact shape: an extensionless/`.py`-stripped name vs its
    `.js` twin. The non-`.js` name must win the shared .cmd destination;
    the `.js` name gets no .cmd entry at all (not a silent overwrite,
    dropped explicitly)."""
    non_js, js = colliding_pair
    target_map = {non_js: non_js, js: js}

    resolved = _resolve_agent_cmd_dest_collisions(target_map)

    assert resolved == {non_js: f"{non_js}.cmd"}
    assert js not in resolved


def test_resolve_agent_cmd_dest_collisions_no_collision_passthrough():
    target_map = {"wsc-close": "wsc-close.py", "claude-doe": "claude-doe"}

    resolved = _resolve_agent_cmd_dest_collisions(target_map)

    assert resolved == {"wsc-close": "wsc-close.cmd", "claude-doe": "claude-doe.cmd"}


def test_resolve_agent_cmd_dest_collisions_unresolvable_raises_fatal():
    """A collision the non-.js-wins rule cannot arbitrate — here, two
    NON-.js names sharing a stem (e.g. a stray `.sh` twin never covered by
    the .js-vs-non-.js precedence) — must fail loud at install time, never
    pick a silent iteration-order winner. (The mirror case, two `.js` names
    colliding, cannot occur in practice: _agent_cmd_dest_name derives the
    destination from Path(name).stem, and two distinct `.js`-suffixed
    installed names can only share a stem if they are the same string.)"""
    target_map = {"foo-bar": "foo-bar", "foo-bar.sh": "foo-bar.sh"}

    with pytest.raises(SubstrateFatalError, match="collision"):
        _resolve_agent_cmd_dest_collisions(target_map)


# ---------------------------------------------------------------------------
# Install-DESTINATION gate — the two suites above (this file's
# _write_agent_forwarder round-trip, and coordinator/tests/
# test_cross_platform_invocability_gate.py) both test halves where the
# invariant already held; neither ever calls _install_bin_resolvers or opens
# an installed .cmd body, so the "generated .cmd body execs a nonexistent
# claude-klabauter-side .py at the install destination" break survived undetected.
# This gate runs the REAL install loop into a tmpdir and asserts BOTH halves
# resolve to a real file THERE — not in claude-klabauter's own coordinator/bin/.
# ---------------------------------------------------------------------------

_DP0_TARGET_RE = re.compile(r'"%~dp0([^"]+)"')


def _make_install_bin_resolvers_fixture(tmp_path: Path) -> "tuple[Path, Path, Path]":
    """Builds a minimal-but-complete fixture tree for a real
    ``_install_bin_resolvers`` call: a synthetic ``coordinator/bin/`` +
    ``coordinator/lib/resolve-claude-klabauter/`` (the claude-klabauter-side tree,
    CLAUDE_KLABAUTER_ROOT-pointed), plus ``templates/bin`` (``ml_bin``) and
    ``claude-home`` (``ch_bin``) stub dirs covering every filename
    ``ml_family``/``ch_family`` install. Content is a stub for the
    ml_bin/ch_bin files (this gate asserts on forwarder-pair resolution,
    not on those unrelated resolvers' own behavior) but the
    ``_resolve_claude_klabauter.py`` copy is the REAL module, keeping the fixture
    honest against drift in that module's own name/exclusion behavior.

    Returns ``(claude_klabauter_root, ml_bin, ch_bin)``.
    """
    claude_klabauter_root = tmp_path / "fixture-claude-klabauter"
    agent_bin = claude_klabauter_root / "coordinator" / "bin"
    resolve_claude_klabauter_lib = claude_klabauter_root / "coordinator" / "lib" / "resolve-claude-klabauter"
    agent_bin.mkdir(parents=True)
    resolve_claude_klabauter_lib.mkdir(parents=True)
    shutil.copyfile(_RESOLVE_CLAUDE_KLABAUTER_SRC, resolve_claude_klabauter_lib / "_resolve_claude_klabauter.py")
    # `_install_bin_resolvers` loads coordinator/lib/bin-templates-manifest.py
    # (C12) off the resolved claude-klabauter root before its write loops run — copy
    # the REAL manifest into this fixture root rather than hand-authoring a
    # second copy that could drift from it.
    shutil.copyfile(
        _BIN_TEMPLATES_MANIFEST_SRC,
        claude_klabauter_root / "coordinator" / "lib" / "bin-templates-manifest.py",
    )

    # A representative slice: a .py-suffixed CLI, an extensionless CLI, and
    # a second .py-suffixed CLI (mint-deliverable-id.py, no longer a pinned
    # special case).
    _touch(agent_bin / "wsc-close.py")
    _touch(agent_bin / "review-brightline-gate.py")
    _touch(agent_bin / "claude-doe")
    _touch(agent_bin / "mint-deliverable-id.py")
    _touch(agent_bin / "mint-deliverable-id.cmd", executable=False)
    # A real .cmd-dest-name collision pair (mirrors the live-tree
    # render-handoff-tracker / render-handoff-tracker.js shape) — exercises
    # _resolve_agent_cmd_dest_collisions' non-.js-wins precedence through
    # the full install loop, not just in isolation.
    _touch(agent_bin / "render-handoff-tracker")
    _touch(agent_bin / "render-handoff-tracker.js")

    ml_bin = tmp_path / "templates-bin"
    ch_bin = tmp_path / "claude-home"
    ml_bin.mkdir()
    ch_bin.mkdir()
    for f, exec_bit in (
        ("machine-local", True), ("_machine_local.py", False),
        ("machine-local.cmd", False), ("python3.cmd", False),
        ("resolve-coordinator-clone", True), ("resolve-coordinator-clone.cmd", False),
        ("coordinator-settings-home", True), ("coordinator-settings-home.cmd", False),
        ("coordinator-settings-home.ps1", False),
        ("claude_machine_local.py", False), ("claude-machine-local.sh", False),
        ("claude-machine-local.ps1", False),
        ("platform-localize.py", True), ("platform-localize.cmd", False),
    ):
        _touch(ml_bin / f, executable=exec_bit)
    for f, exec_bit in (
        ("claude-home", True), ("_claude_home.py", False), ("claude-home.cmd", False),
    ):
        _touch(ch_bin / f, executable=exec_bit)

    return claude_klabauter_root, ml_bin, ch_bin


def test_install_bin_resolvers_agent_helper_pairs_resolve_at_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """For every agent-helper forwarder the real install loop emits, BOTH
    the Unix half's exec_cli(...) target AND the generated .cmd half's
    %~dp0<name> target must resolve to a file that actually exists — the
    Unix half's target inside the fixture coordinator/bin/, the .cmd half's
    target inside the SAME install destination dir it was written to (i.e.
    the co-located Unix-half sibling, never the claude-klabauter-side .py file).

    Against the retired copy-a-source-.cmd approach this fails outright:
    that .cmd body targeted %~dp0<target>.py (e.g. %~dp0wsc-close.py),
    which is never present at bin_dst — only inside claude-klabauter's
    own coordinator/bin/. The generator fix makes it pass by construction.
    """
    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    from coordinator_core.install.substrate import _install_bin_resolvers

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        False,
        python3_cmd_resolved_bin="",
    )

    agent_bin = claude_klabauter_root / "coordinator" / "bin"
    mapping = _derive_agent_helper_target_map(agent_bin)
    assert mapping, "fixture produced no agent-helper forwarders — test is vacuous"

    cmd_dest_map = _resolve_agent_cmd_dest_collisions(mapping)
    # Sanity: the fixture's real collision pair (render-handoff-tracker vs
    # render-handoff-tracker.js) must have resolved to the non-.js winner.
    assert cmd_dest_map.get("render-handoff-tracker") == "render-handoff-tracker.cmd"
    assert "render-handoff-tracker.js" not in cmd_dest_map

    for dest_dir in (bin_dst,):
        for name, target in sorted(mapping.items()):
            unix_half = dest_dir / name
            assert unix_half.is_file(), f"Unix-half forwarder missing at {unix_half}"
            unix_content = unix_half.read_text(encoding="utf-8")
            assert f'exec_cli("{target}")' in unix_content
            assert (agent_bin / target).is_file(), (
                f"{name}: Unix-half execs {target!r}, not present in the "
                f"fixture coordinator/bin/ — claude-klabauter-side target is dead"
            )

            cmd_dest_name = cmd_dest_map.get(name)
            if cmd_dest_name is None:
                # A collision loser (e.g. render-handoff-tracker.js) — no
                # .cmd is written under its own dest name, since that name
                # was resolved to a DIFFERENT installed name's forwarder.
                continue

            cmd_dst = dest_dir / cmd_dest_name
            assert cmd_dst.is_file(), f".cmd half missing at {cmd_dst}"
            cmd_content = cmd_dst.read_text(encoding="utf-8")
            m = _DP0_TARGET_RE.search(cmd_content)
            assert m, f".cmd half for {name!r} has no %~dp0<target> reference"
            cmd_target = m.group(1)
            assert cmd_target == name, (
                f".cmd half for {name!r} targets {cmd_target!r} via "
                f"%~dp0 — expected the co-located Unix-half forwarder's own "
                f"installed name {name!r}"
            )
            assert (dest_dir / cmd_target).is_file(), (
                f"{name}: .cmd half targets %~dp0{cmd_target}, which does "
                f"NOT exist at the install destination {dest_dir} — this is "
                f"the break-class defect this gate exists to catch"
            )
            assert "__PYTHON_BIN__" not in cmd_content, (
                f"{name}: .cmd half left the __PYTHON_BIN__ placeholder "
                f"unsubstituted"
            )


def test_write_agent_cmd_forwarder_no_delayed_expansion(tmp_path: Path):
    """`enabledelayedexpansion` + unguarded `%*` silently corrupts any
    forwarded argument containing a literal `!` (cmd.exe scans the whole
    command line -- including whatever %* substitutes in -- for `!...!`
    tokens before running it). The generated body must forward %* with NO
    delayed expansion in effect at all, not merely avoid `!ERRORLEVEL!`.
    Regression for the P1 finding in
    state/review-trail/findings/2026-07-23-codereview-slicecmd-forwarder-fix-17db70f4-coordinator-core-install-substrate-py-co.md."""
    dst = tmp_path / "cross-repo-memo.cmd"
    _write_agent_cmd_forwarder(
        "cross-repo-memo", dst, False, python3_cmd_resolved_bin=r"C:\Python311\python.exe",
    )
    content = dst.read_text(encoding="utf-8")
    assert not _DIRECTIVE_ENABLES_DELAYED_EXPANSION_RE.search(content)
    non_comment_lines = "\n".join(
        line for line in content.splitlines() if not line.strip().upper().startswith("REM")
    )
    assert "!" not in non_comment_lines, (
        "no `!...!` delayed-expansion token may appear in any executable "
        "line of a body that forwards %* without delayed expansion enabled "
        "(a mention inside an explanatory REM comment is fine)"
    )
    assert "%*" in content
    assert "%ERRORLEVEL%" in content


# Matches the actual `setlocal enabledelayedexpansion` DIRECTIVE (optionally
# combined with other setlocal options, e.g. `setlocal enabledelayedexpansion
# enableextensions`) -- deliberately NOT a bare substring check, since a REM
# comment is allowed to mention "enabledelayedexpansion" by name (as this
# file's own generated launchers now do, explaining why it's absent) without
# tripping the gate.
_DIRECTIVE_ENABLES_DELAYED_EXPANSION_RE = re.compile(
    r"^\s*setlocal\b.*\benabledelayedexpansion\b", re.IGNORECASE | re.MULTILINE
)


def _assert_cmd_body_never_combines_delayed_expansion_with_arg_forward(
    content: str, label: str
) -> None:
    """Shared assertion: a `.cmd` body that forwards `%*` must never have
    `enabledelayedexpansion` in effect. Checked as a whole-body invariant
    (not merely "no `!ERRORLEVEL!`") because the defect is cmd.exe scanning
    the ENTIRE command line for `!...!` once delayed expansion is on for the
    script -- there is no shape where `%*` and `enabledelayedexpansion`
    safely coexist. Matches the actual `setlocal ... enabledelayedexpansion`
    directive, not a bare substring -- a REM comment explaining the fix (as
    every regenerated launcher's header now does) must not trip this gate."""
    has_delayed_expansion = bool(_DIRECTIVE_ENABLES_DELAYED_EXPANSION_RE.search(content))
    forwards_argv = "%*" in content
    assert not (has_delayed_expansion and forwards_argv), (
        f"{label}: `setlocal enabledelayedexpansion` combined with a "
        f"forwarded %* -- any argument containing a literal `!` gets "
        f"silently mangled (unmatched `!` truncates the line; a matched "
        f"`!token!` that isn't a defined env var becomes empty string)"
    )


def test_generated_cmd_never_combines_delayed_expansion_with_arg_forward(tmp_path: Path):
    """Direct regression for the P1 finding, asserted on the actual emitted
    shape rather than on a comment or a single token's absence."""
    dst = tmp_path / "some-cli.cmd"
    for resolved_bin in ("", r"C:\Python311\python.exe"):
        _write_agent_cmd_forwarder(
            "some-cli", dst, False, python3_cmd_resolved_bin=resolved_bin,
        )
        _assert_cmd_body_never_combines_delayed_expansion_with_arg_forward(
            dst.read_text(encoding="utf-8"), f"_write_agent_cmd_forwarder({resolved_bin!r})"
        )


def test_source_cmd_corpus_never_combines_delayed_expansion_with_arg_forward():
    """Live-tree gate over the hand-authored/generated source-side `.cmd`
    twins in coordinator/bin/ -- the templates `_write_agent_cmd_forwarder`'s
    docstring says it mirrors. Asserts on the actual emitted shape of every
    `.cmd` file on disk, not on a comment claiming the invariant holds."""
    bin_dir = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
    cmd_files = sorted(bin_dir.glob("*.cmd"))
    assert cmd_files, f"no .cmd files found under {bin_dir} -- gate is vacuous"
    for cmd_path in cmd_files:
        content = cmd_path.read_text(encoding="utf-8")
        _assert_cmd_body_never_combines_delayed_expansion_with_arg_forward(
            content, str(cmd_path.relative_to(bin_dir.parents[1]))
        )


# ---------------------------------------------------------------------------
# _sweep_orphaned_agent_helpers -- install-time cleanup for agent-helper
# forwarder launchers this installer previously wrote but would no longer
# write on the current run (deleted/renamed CLI in coordinator/bin/, or a
# collision loser's stale .cmd -- see the function's own docstring for the
# full provenance-mechanism rationale). Positive content-marker
# identification, never a "not in the current derived set" name heuristic.
# ---------------------------------------------------------------------------

_REAL_AGENT_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def test_sweep_removes_orphaned_unix_forwarder(tmp_path: Path):
    """A Unix-half forwarder for a CLI no longer present in the current
    derived map (the deleted-CLI shape -- e.g. the real
    verify-cc-root-source-guard-sync.py CLI that no longer exists) must be
    removed."""
    orphan = tmp_path / "verify-cc-root-source-guard-sync"
    _write_agent_forwarder(
        "verify-cc-root-source-guard-sync", orphan, False,
        target="verify-cc-root-source-guard-sync.py",
    )
    assert orphan.is_file()

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert not orphan.exists()


def test_sweep_removes_orphaned_cmd_forwarder(tmp_path: Path):
    """A `.cmd` half whose installed name is absent from the current run's
    cmd-dest map (deleted CLI, or a collision loser's stale `.cmd` from an
    older on-disk set) must be removed. Framed against `old-cli.cmd`, which
    is exactly the shape a collision loser's now-unreferenced `.cmd` takes:
    present on disk, carrying the generator marker, but not a value in this
    run's `agent_cmd_dest_map`."""
    orphan = tmp_path / "old-cli.cmd"
    _write_agent_cmd_forwarder("old-cli", orphan, False, python3_cmd_resolved_bin="")
    assert orphan.is_file()

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert not orphan.exists()


def test_sweep_does_not_remove_orphan_named_file_lacking_the_marker(tmp_path: Path):
    """Same orphaned NAME as the deleted-CLI shape above, but content that
    does not carry this module's generator marker (e.g. a hand-authored
    script, or a forwarder generated by some other tool entirely) -- proves
    the sweep is content-gated, not name-gated. A blind "not in the current
    derived set" sweep would delete this; the positive marker check must
    not."""
    decoy = tmp_path / "verify-cc-root-source-guard-sync"
    decoy.write_text("#!/bin/sh\necho 'hand-authored, not ours'\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert decoy.is_file()
    assert decoy.read_text(encoding="utf-8") == "#!/bin/sh\necho 'hand-authored, not ours'\n"


def test_sweep_does_not_remove_other_family_files(tmp_path: Path):
    """Files belonging to the OTHER families that share bin_dst
    (ml_family, ch_family, platform-localize, resolve-coordinator-clone,
    the reserved names, an operator-customized file) must never be swept,
    regardless of whether their name happens to collide with something the
    agent-helper derivation would exclude. None of these carry either
    generator marker, so the positive-identification check must leave every
    one of them alone."""
    decoys = {
        "machine-local": "#!/usr/bin/env python3\n# real machine-local CLI\n",
        "claude-home": "#!/usr/bin/env python3\n# real claude-home CLI\n",
        "platform-localize.cmd": "@echo off\nrem DoE template, not ours\n",
        "resolve-coordinator-clone.cmd": "@echo off\nrem DoE template, not ours\n",
        "python3.cmd": "@echo off\nrem rendered python3 launcher shim\n",
        "my-custom-alias": "#!/bin/sh\necho operator customized this\n",
    }
    for name, content in decoys.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    for name, content in decoys.items():
        p = tmp_path / name
        assert p.is_file(), f"{name} was swept but belongs to another family"
        assert p.read_text(encoding="utf-8") == content


def test_sweep_check_only_reports_without_deleting(tmp_path: Path, capsys: pytest.CaptureFixture):
    orphan = tmp_path / "verify-cc-root-source-guard-sync"
    _write_agent_forwarder(
        "verify-cc-root-source-guard-sync", orphan, False,
        target="verify-cc-root-source-guard-sync.py",
    )

    # An orphan present on disk is a genuinely stale state -- check-only now
    # fails loud rather than silently reporting an always-green no-op.
    with pytest.raises(SubstrateFatalError, match="orphaned"):
        _sweep_orphaned_agent_helpers(tmp_path, {}, {}, True)

    assert orphan.is_file()
    out = capsys.readouterr().out
    assert "verify-cc-root-source-guard-sync" in out
    assert "is orphaned agent-helper forwarder" in out


def test_sweep_check_only_clean_reports_no_op(tmp_path: Path, capsys: pytest.CaptureFixture):
    kept = tmp_path / "cross-repo-memo"
    _write_agent_forwarder("cross-repo-memo", kept, False, target="cross-repo-memo.py")

    _sweep_orphaned_agent_helpers(tmp_path, {"cross-repo-memo": "cross-repo-memo.py"}, {}, True)

    assert kept.is_file()
    out = capsys.readouterr().out
    assert "no orphaned agent-helper forwarders" in out


def test_sweep_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture):
    orphan = tmp_path / "verify-cc-root-source-guard-sync"
    _write_agent_forwarder(
        "verify-cc-root-source-guard-sync", orphan, False,
        target="verify-cc-root-source-guard-sync.py",
    )

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)
    assert not orphan.exists()
    capsys.readouterr()  # discard first-run output

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)
    out = capsys.readouterr().out
    assert "removed orphaned agent-helper forwarder" not in out


def test_sweep_leaves_currently_installed_names_alone(tmp_path: Path):
    """A forwarder for a name that IS in the current run's derived map must
    survive -- the sweep only targets what this run would NOT install."""
    kept = tmp_path / "cross-repo-memo"
    _write_agent_forwarder("cross-repo-memo", kept, False, target="cross-repo-memo.py")
    kept_cmd = tmp_path / "cross-repo-memo.cmd"
    _write_agent_cmd_forwarder("cross-repo-memo", kept_cmd, False, python3_cmd_resolved_bin="")

    _sweep_orphaned_agent_helpers(
        tmp_path,
        {"cross-repo-memo": "cross-repo-memo.py"},
        {"cross-repo-memo": "cross-repo-memo.cmd"},
        False,
    )

    assert kept.is_file()
    assert kept_cmd.is_file()


def test_sweep_against_real_coordinator_bin_derivation_does_not_remove_live_forwarders(
    tmp_path: Path,
):
    """Real-derivation coverage (not a synthetic fixture): derive the
    current forwarder set from claude-klabauter's OWN coordinator/bin/, write real
    forwarders for one live name into a scratch dir, and confirm the sweep
    -- run with the REAL derived maps -- does not remove them. Guards
    against the sweep's exclusion checks drifting out of sync with what
    `_derive_agent_helper_target_map`/`_resolve_agent_cmd_dest_collisions`
    actually produce on this checkout."""
    mapping = _derive_agent_helper_target_map(_REAL_AGENT_BIN)
    assert mapping, f"no agent-helper forwarders derived from {_REAL_AGENT_BIN} -- test is vacuous"
    cmd_dest_map = _resolve_agent_cmd_dest_collisions(mapping)

    live_name, live_target = sorted(mapping.items())[0]
    live_unix = tmp_path / live_name
    _write_agent_forwarder(live_name, live_unix, False, target=live_target)

    live_cmd_dest = cmd_dest_map.get(live_name)
    live_cmd = None
    if live_cmd_dest is not None:
        live_cmd = tmp_path / live_cmd_dest
        _write_agent_cmd_forwarder(live_name, live_cmd, False, python3_cmd_resolved_bin="")

    _sweep_orphaned_agent_helpers(tmp_path, mapping, cmd_dest_map, False)

    assert live_unix.is_file(), f"real, currently-derived forwarder {live_name!r} was swept"
    if live_cmd is not None:
        assert live_cmd.is_file(), f"real, currently-derived .cmd {live_cmd_dest!r} was swept"


def test_install_bin_resolvers_sweeps_orphan_in_install_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: an orphaned forwarder pre-existing in bin_dst (a settings-
    home bin/ gone stale after a CLI was deleted from coordinator/bin/) is
    swept by a normal `_install_bin_resolvers` run. (The former
    ``~/.claude/bin/`` compat mirror this test used to also assert against is
    retired -- `_install_bin_resolvers` no longer writes there at all.)"""
    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    _write_agent_forwarder(
        "deleted-cli", bin_dst / "deleted-cli", False, target="deleted-cli.py",
    )
    _write_agent_cmd_forwarder(
        "deleted-cli", bin_dst / "deleted-cli.cmd", False, python3_cmd_resolved_bin="",
    )

    from coordinator_core.install.substrate import _install_bin_resolvers

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        False,
        python3_cmd_resolved_bin="",
    )

    assert not (bin_dst / "deleted-cli").exists(), "orphan Unix-half survived"
    assert not (bin_dst / "deleted-cli.cmd").exists(), "orphan .cmd survived"


def test_install_bin_resolvers_sweep_idempotent_over_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
):
    """Two consecutive full install runs against the same destination dirs
    must sweep the orphan exactly once -- the second run reports no
    deletions and produces no churn."""
    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    _write_agent_forwarder("deleted-cli", bin_dst / "deleted-cli", False, target="deleted-cli.py")

    from coordinator_core.install.substrate import _install_bin_resolvers

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst, False, python3_cmd_resolved_bin="",
    )
    assert not (bin_dst / "deleted-cli").exists()
    capsys.readouterr()

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst, False, python3_cmd_resolved_bin="",
    )
    out = capsys.readouterr().out
    assert "removed orphaned agent-helper forwarder" not in out


# ---------------------------------------------------------------------------
# Legacy-marker sweep coverage. Every real pre-existing agent-helper `.cmd`
# orphan in the fleet predates `_write_agent_cmd_forwarder` and carries
# `_LEGACY_CMD_MARKER` (stamped by the retired coordinator/bin/
# gen-launcher-shim.py copy-verbatim approach), not `_AGENT_CMD_FORWARDER_
# MARKER` -- a live dry-run against a real install caught the strict-
# marker-only sweep missing exactly this shape. The legacy marker is NOT
# exclusive to agent-helper forwarders (DoE's platform-localize.cmd /
# resolve-coordinator-clone.cmd carry it too), so these files must survive
# via `_static_bin_family_names()` membership, not via the marker check.
# ---------------------------------------------------------------------------

def test_sweep_removes_legacy_marker_orphan_cmd(tmp_path: Path):
    """A pre-`_write_agent_cmd_forwarder`-era `.cmd` orphan (legacy marker,
    no current generator marker) for a CLI no longer derived must still be
    swept -- this is the exact real-world shape
    (verify-cc-root-source-guard-sync.cmd) that motivated the task."""
    orphan = tmp_path / "verify-cc-root-source-guard-sync.cmd"
    orphan.write_text(
        "@echo off\n"
        "setlocal\n"
        f"REM {_LEGACY_CMD_MARKER} -- do NOT hand-edit; regenerate.\n"
        '"%~dp0..\\..\\coordinator\\bin\\verify-cc-root-source-guard-sync.py" %*\n',
        encoding="utf-8",
    )

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert not orphan.exists()


def test_sweep_does_not_remove_legacy_marker_files_from_other_families(tmp_path: Path):
    """platform-localize.cmd and resolve-coordinator-clone.cmd are real DoE
    templates generated by the same gen-launcher-shim.py tool and DO carry
    `_LEGACY_CMD_MARKER` on a live install -- confirmed empirically. Both
    names are members of `_static_bin_family_names()` (they're installed
    every run via `coordinator/lib/bin-templates-manifest.py`'s
    `ML_FAMILY_FILES`/`PLATFORM_LOCALIZE_FILES` groups), so the
    completeness check must keep them safe despite the marker match. This
    is the exact false-positive a marker-only widening would have
    introduced."""
    legacy_body = (
        "@echo off\nsetlocal\n"
        f"REM {_LEGACY_CMD_MARKER} -- do NOT hand-edit; regenerate.\n"
        '"%~dp0some-target.py" %*\n'
    )
    decoys = {
        "platform-localize.cmd": legacy_body,
        "resolve-coordinator-clone.cmd": legacy_body,
    }
    for name, content in decoys.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    for name, content in decoys.items():
        p = tmp_path / name
        assert p.is_file(), f"{name} was swept despite being a protected static-family name"
        assert p.read_text(encoding="utf-8") == content


def test_static_bin_family_names_includes_platform_localize_and_resolve_coordinator_clone():
    names = _static_bin_family_names()
    assert "platform-localize.cmd" in names
    assert "platform-localize.py" in names
    assert "resolve-coordinator-clone.cmd" in names
    assert "resolve-coordinator-clone" in names
    # Dynamic agent-helper names are NEVER members -- that half of the
    # completeness set comes from the caller-supplied maps, not this one.
    assert "cross-repo-memo" not in names


def test_install_bin_resolvers_sweeps_legacy_marker_orphan_but_keeps_doe_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end reproduction of the exact bug the team lead's live
    dry-run caught: a legacy-marker `.cmd` orphan for a deleted CLI is
    swept by a real `_install_bin_resolvers` run, while the real,
    installed-every-run `platform-localize.cmd` (which also carries the
    legacy marker) survives untouched."""
    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    # Fixture's platform-localize.cmd stub predates real content -- give it
    # a legacy-marker body so this test actually exercises the collision
    # the completeness check exists to prevent.
    (ml_bin / "platform-localize.cmd").write_text(
        f"@echo off\nsetlocal\nREM {_LEGACY_CMD_MARKER}\n", encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    orphan = bin_dst / "deleted-cli.cmd"
    orphan.write_text(f"@echo off\nsetlocal\nREM {_LEGACY_CMD_MARKER}\n", encoding="utf-8")

    from coordinator_core.install.substrate import _install_bin_resolvers

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst, False, python3_cmd_resolved_bin="",
    )

    assert not orphan.exists(), "legacy-marker orphan for a deleted CLI must be swept"
    installed_platform_localize = bin_dst / "platform-localize.cmd"
    assert installed_platform_localize.is_file(), (
        "platform-localize.cmd (legacy marker, but a protected static-family name) "
        "must survive the sweep"
    )
    assert _LEGACY_CMD_MARKER in installed_platform_localize.read_text(encoding="utf-8")


def test_sweep_against_real_derivation_uses_full_static_completeness_set(tmp_path: Path):
    """Real-tree coverage for the completeness fix specifically: run the
    sweep with the REAL `_static_bin_family_names()` unioned in (mirroring
    exactly what `_install_bin_resolvers` passes implicitly via its own
    call), and confirm a legacy-marker file named after a real static
    family member (platform-localize.cmd) survives even with an EMPTY
    agent-helper map -- proving protection comes from the static set, not
    from the agent-helper derivation."""
    protected = tmp_path / "platform-localize.cmd"
    protected.write_text(f"@echo off\nREM {_LEGACY_CMD_MARKER}\n", encoding="utf-8")
    assert "platform-localize.cmd" in _static_bin_family_names()

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert protected.is_file()


# ---------------------------------------------------------------------------
# _prune_orphaned_static_bin_names / bin-manifest — AC18's general
# install-time prune. `_sweep_orphaned_agent_helpers` above solves the
# orphan problem for the DYNAMIC agent-helper family via content-marker
# provenance; this mechanism solves it for a STATIC-family rename (the
# platform-localize.sh -> {.py,.cmd} shape this chunk's uninstall_legs.py
# fix also addresses) via a manifest THIS installer writes and reads back —
# never a "present in bin_dst" name heuristic, so an operator's own file
# sharing bin_dst is never touched.
# ---------------------------------------------------------------------------


def test_prune_orphaned_static_bin_names_removes_name_dropped_from_manifest(tmp_path: Path):
    (tmp_path / "platform-localize.sh").write_text("stub", encoding="utf-8")
    _write_bin_manifest(tmp_path, frozenset({"platform-localize.sh", "still-current"}))

    _prune_orphaned_static_bin_names(tmp_path, frozenset({"still-current"}), False)

    assert not (tmp_path / "platform-localize.sh").exists()


def test_prune_orphaned_static_bin_names_provenance_guard_ignores_unrecorded_file(
    tmp_path: Path,
):
    """AC18's provenance guard: a file merely PRESENT in bin_dst, with no
    prior-manifest entry recording this installer as its author, must never
    be removed — regardless of whether its name is outside current_names.
    This is what stops the prune from deleting an operator's own file that
    happens to share the directory."""
    operator_file = tmp_path / "my-own-alias"
    operator_file.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    # No manifest at all -- simulates the very first real run, or an
    # operator file this installer never wrote.
    assert _read_bin_manifest(tmp_path) == frozenset()

    _prune_orphaned_static_bin_names(tmp_path, frozenset({"something-else"}), False)

    assert operator_file.is_file()
    assert operator_file.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_prune_orphaned_static_bin_names_check_only_reports_without_deleting(
    tmp_path: Path, capsys: pytest.CaptureFixture,
):
    (tmp_path / "old-name").write_text("stub", encoding="utf-8")
    _write_bin_manifest(tmp_path, frozenset({"old-name"}))

    _prune_orphaned_static_bin_names(tmp_path, frozenset(), True)

    assert (tmp_path / "old-name").is_file()
    out = capsys.readouterr().out
    assert "would: prune orphaned coordinator bin file" in out
    # check_only must never overwrite the manifest with the (unpruned)
    # current-run set.
    assert _read_bin_manifest(tmp_path) == frozenset({"old-name"})


def test_prune_orphaned_static_bin_names_writes_current_manifest_after_real_run(
    tmp_path: Path,
):
    _prune_orphaned_static_bin_names(tmp_path, frozenset({"a", "b"}), False)
    assert _read_bin_manifest(tmp_path) == frozenset({"a", "b"})


def test_prune_orphaned_static_bin_names_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture,
):
    (tmp_path / "old-name").write_text("stub", encoding="utf-8")
    _write_bin_manifest(tmp_path, frozenset({"old-name"}))

    _prune_orphaned_static_bin_names(tmp_path, frozenset(), False)
    assert not (tmp_path / "old-name").exists()
    capsys.readouterr()

    _prune_orphaned_static_bin_names(tmp_path, frozenset(), False)
    out = capsys.readouterr().out
    assert "pruned orphaned coordinator bin file" not in out


def test_install_bin_resolvers_prunes_retired_static_name_across_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end reproduction of the platform-localize.sh generalization
    (AC18): a name recorded in THIS installer's own manifest from a prior
    real run, but absent from the CURRENT run's static+agent-helper
    write-set, is pruned by `_install_bin_resolvers`'s Step 3e — without a
    hand-added literal anywhere."""
    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    (bin_dst / "platform-localize.sh").write_text("#!/bin/sh\nold\n", encoding="utf-8")
    _write_bin_manifest(bin_dst, _static_bin_family_names() | {"platform-localize.sh"})

    from coordinator_core.install.substrate import _install_bin_resolvers

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst, False, python3_cmd_resolved_bin="",
    )

    assert not (bin_dst / "platform-localize.sh").exists(), (
        "a retired static-family name recorded in the prior manifest but "
        "absent from this run's write-set must be pruned"
    )
    assert (bin_dst / "platform-localize.py").is_file(), (
        "current static-family member must still be installed normally"
    )


def test_install_bin_resolvers_prune_never_touches_operator_file_never_in_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Provenance-guard coverage through the real install loop: an
    operator's own file sharing bin_dst, with no manifest recording it as
    ours, survives a real `_install_bin_resolvers` run even though its name
    is outside every current write-set."""
    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    operator_file = bin_dst / "my-own-alias"
    operator_file.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    from coordinator_core.install.substrate import _install_bin_resolvers

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst, False, python3_cmd_resolved_bin="",
    )

    assert operator_file.is_file()
    assert operator_file.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "shutil.which() on Windows consults PATHEXT and does not resolve "
        "an extensionless file the way POSIX does; this suite's "
        "extensionless-forwarder PATH-resolution contract is POSIX-only"
    ),
)
def test_forwarder_is_which_resolvable_once_dir_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """AC8: a generated forwarder's directory, once on PATH, must be the
    directory ``shutil.which()`` actually resolves to — proving the
    forwarder is bareword-reachable, not merely present on disk."""
    forwarder = _write_forwarder(tmp_path, name="cross-repo-memo")

    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    resolved = shutil.which("cross-repo-memo")

    assert resolved is not None
    assert Path(resolved).resolve() == forwarder.resolve()


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "shutil.which() on Windows consults PATHEXT and does not resolve "
        "an extensionless file the way POSIX does; this suite's "
        "extensionless-forwarder PATH-resolution contract is POSIX-only"
    ),
)
def test_forwarder_not_which_resolvable_when_dir_absent_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Negative twin of the assertion above — without this, the positive
    test cannot actually fail, since ``shutil.which`` would resolve the
    name from whatever happens to already be on the real PATH."""
    _write_forwarder(tmp_path, name="cross-repo-memo")

    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    assert shutil.which("cross-repo-memo") is None


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "shutil.which() on Windows consults PATHEXT and does not resolve "
        "an extensionless file the way POSIX does; this suite's "
        "extensionless-forwarder PATH-resolution contract is POSIX-only"
    ),
)
def test_forwarder_append_ordering_earlier_path_entry_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """AC5's only mechanical check: C1 must APPEND ``settings-home/bin`` to
    PATH, never prepend. Exercises the REAL production writer
    (``shell_rc_guard.write_path_entry_guard_blocks``) rather than a
    hand-built PATH string — a hand-built string only proves
    ``shutil.which()``'s own search order, which is true unconditionally
    regardless of what C1 emits and would not catch C1's
    ``position="append"`` silently flipping to ``"prepend"``. Asserts the
    SEMANTIC property (the guarded entry lands after ``$PATH`` in the
    emitted ``export PATH=...`` assignment, not before it) rather than an
    exact byte-for-byte body string, so this survives an in-flight
    shell-metacharacter-escaping change to ``_build_path_entry_body``.
    This is what would silently regress if a later edit "tidied" the
    append into a prepend."""
    from coordinator_core.install import shell_rc_guard

    home = tmp_path / "home"
    home.mkdir()

    result = shell_rc_guard.write_path_entry_guard_blocks(
        path_entry="/settings-home/bin",
        sentinel_id="SETTINGS_HOME_BIN",
        position="append",
        home=home,
    )

    assert result["modified"] is True

    rc_path = home / ".zshrc"
    body = rc_path.read_text(encoding="utf-8")

    assignment_line = next(
        line for line in body.splitlines() if "export PATH=" in line
    )
    assignment = assignment_line[assignment_line.index("export PATH=") :]

    path_idx = assignment.index("$PATH")
    entry_idx = assignment.index("settings-home")
    assert path_idx < entry_idx, (
        "expected the guarded entry to be APPENDED after $PATH, not "
        f"prepended before it; got assignment: {assignment!r}"
    )
