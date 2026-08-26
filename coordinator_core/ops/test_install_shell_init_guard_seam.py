"""
Co-located pytest for coordinator_core.ops.install_shell_init_guard_seam
(install.md § 3.5b.1 native port, DoE-claude repo). Covers: --check-only
reports without mutating the rc file; a live run writes the sentinel-guarded
block and is idempotent on re-run (append-not-clobber — pre-existing rc
content is preserved, and a second live run is a silent no-op rather than a
second append); and the graceful skip when no claude-klabauter guard script is
resolvable (or not executable).

Never touches a real rc file — every rc path in this suite is under
tmp_path, and REPO_CLAUDE_KLABAUTER is set explicitly per test so resolution
never falls through to the operator's real machine-local registry.

Converted 2026-08-16 (C7b): `resolve_claude_klabauter_clone`'s tier-2 rung now reads
the machine-local registry in-process (`machine_resolver.registry_get`).
The autouse `_isolate_env` fixture below additionally points
`MACHINE_LOCAL_REGISTRY_DIR` at an empty, scratch `tmp_path` subdirectory so
the one test that relies on tier-2 falling through
(`test_graceful_skip_when_claude_klabauter_not_resolvable`) cannot read the
operator's REAL registry either -- per
`state/lessons/2026-07-17-redirect-state-home-env-to-tmp-in-unit-t-*.yaml`.

Spec backlink: coordinator/commands/install.md § 3.5b.1 [DoE-claude repo]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import install_shell_init_guard_seam as seam


@pytest.fixture()
def claude_klabauter_clone(tmp_path: Path) -> Path:
    """A fake claude-klabauter checkout carrying shell-init-guard.py at the mode
    claude-klabauter's real `bin/` uses: 100644, no execute bit. Every happy-path test
    below therefore doubles as a regression against re-introducing an
    exec-bit gate (bug fix, 2026-08-22)."""
    clone = tmp_path / "claude-klabauter"
    (clone / "bin").mkdir(parents=True)
    guard = clone / "bin" / "shell-init-guard.py"
    guard.write_text("#!/usr/bin/env python3\nprint('')\n")
    guard.chmod(0o644)
    return clone


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Never let resolution fall through to the real machine-local registry
    or the real $SHELL/$HOME — every test sets what it needs explicitly."""
    monkeypatch.delenv("REPO_CLAUDE_KLABAUTER", raising=False)
    monkeypatch.delenv("COORDINATOR_SHIM_RC", raising=False)
    empty_registry = tmp_path / "ml-registry"
    empty_registry.mkdir(exist_ok=True)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty_registry))


# ---------------------------------------------------------------------------
# --check-only: report only, never mutate
# ---------------------------------------------------------------------------


def test_check_only_reports_would_install_and_does_not_mutate(
    capsys, monkeypatch, tmp_path, claude_klabauter_clone
):
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\n")

    rc = seam.main(["--check-only", "--rc", str(rc_path)])

    # Sentinel absent -- a real run would install it, so check-only now
    # fails loud rather than reporting an always-green 0.
    assert rc == 1
    out = capsys.readouterr().out
    assert f"shell_init_guard: check failed: sentinel absent in {rc_path} (would install)" in out
    # No mutation whatsoever.
    assert rc_path.read_text() == "# pre-existing content\n"


def test_check_only_when_sentinel_already_present_reports_ready(
    capsys, monkeypatch, tmp_path, claude_klabauter_clone
):
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text(f"# pre-existing content\n{seam.SENTINEL}\nsome-line\n")
    before = rc_path.read_text()

    rc = seam.main(["--check-only", "--rc", str(rc_path)])

    assert rc == 0
    assert f"shell_init_guard: ready (no-op) ({rc_path})" in capsys.readouterr().out
    assert rc_path.read_text() == before


# ---------------------------------------------------------------------------
# Live run: writes sentinel-guarded block, append-not-clobber, idempotent
# ---------------------------------------------------------------------------


def test_live_run_writes_sentinel_block_and_preserves_prior_content(
    capsys, monkeypatch, tmp_path, claude_klabauter_clone
):
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\nexport FOO=bar\n")

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    assert f"shell_init_guard: installed ({rc_path})" in capsys.readouterr().out

    contents = rc_path.read_text()
    # Prior content preserved verbatim (append, not clobber).
    assert contents.startswith("# pre-existing content\nexport FOO=bar\n")
    assert seam.SENTINEL in contents
    guard_src = str(claude_klabauter_clone / "bin" / "shell-init-guard.py")
    assert guard_src in contents
    assert 'eval "$(python3 "$_cc_fsize_guard" 2>/dev/null)"' in contents
    # `-f`, never `-x`: the guard ships 100644 and is interpreter-invoked, so
    # an `-x` test in the emitted block would make the eval dead on every
    # clone (bug fix, 2026-08-22).
    assert 'if [ -f "$_cc_fsize_guard" ]; then' in contents
    assert 'if [ -x "$_cc_fsize_guard" ]' not in contents
    # END marker closes the block (chunk C6) -- present on every fresh
    # write, positioned after the BEGIN sentinel.
    assert seam.SENTINEL_END in contents
    assert contents.index(seam.SENTINEL) < contents.index(seam.SENTINEL_END)


def test_legacy_begin_only_block_still_detected_as_already_installed(
    capsys, monkeypatch, tmp_path, claude_klabauter_clone
):
    """A machine with the pre-C6 BEGIN-only block (no END marker at all)
    must still be detected as already-installed -- detection is by
    `SENTINEL` line-membership alone and must never require `SENTINEL_END`.
    No retrofit: the legacy block is left byte-for-byte untouched."""
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    guard_src = str(claude_klabauter_clone / "bin" / "shell-init-guard.py")
    legacy_block = (
        "# pre-existing content\n"
        f"\n{seam.SENTINEL}\n"
        f'_cc_fsize_guard="{guard_src}"\n'
        'if [ -x "$_cc_fsize_guard" ]; then eval "$(python3 "$_cc_fsize_guard" 2>/dev/null)"; fi\n'
        "unset _cc_fsize_guard\n"
    )
    rc_path.write_text(legacy_block)

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    assert f"shell_init_guard: ready (no-op) ({rc_path})" in capsys.readouterr().out
    # No retrofit, no second block, no END marker inserted after the fact.
    assert rc_path.read_text() == legacy_block
    assert seam.SENTINEL_END not in rc_path.read_text()


def test_live_run_is_idempotent_on_second_invocation(
    capsys, monkeypatch, tmp_path, claude_klabauter_clone
):
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\n")

    rc1 = seam.main(["--rc", str(rc_path)])
    assert rc1 == 0
    after_first = rc_path.read_text()
    assert after_first.count(seam.SENTINEL) == 1

    capsys.readouterr()  # drain first-run output

    rc2 = seam.main(["--rc", str(rc_path)])
    assert rc2 == 0
    assert f"shell_init_guard: ready (no-op) ({rc_path})" in capsys.readouterr().out

    after_second = rc_path.read_text()
    # No second append — byte-identical to after the first run.
    assert after_second == after_first
    assert after_second.count(seam.SENTINEL) == 1


# ---------------------------------------------------------------------------
# Graceful skip when no guard is resolvable
# ---------------------------------------------------------------------------


def test_graceful_skip_when_claude_klabauter_not_resolvable(capsys, tmp_path):
    """Condition 1 of 3 — the repo is not registered at all. The message must
    name registration, and must not be shared with the two conditions below
    (bug fix, 2026-08-22: all three printed "claude-klabauter not found")."""
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\n")

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "shell_init_guard: skipped (claude-klabauter not registered — "
        "set REPO_CLAUDE_KLABAUTER or machine-local repos.claude_klabauter)"
    ) in out
    # No mutation — graceful no-op leaves the rc file untouched.
    assert rc_path.read_text() == "# pre-existing content\n"


def test_graceful_skip_when_guard_script_missing(capsys, monkeypatch, tmp_path):
    """Condition 2 of 3 — the repo IS registered and the guard file is not
    there. The message names the path it looked for; it must never claim the
    repo is missing, which sends the operator hunting for a repo that is
    present."""
    empty_clone = tmp_path / "claude-klabauter-empty"
    empty_clone.mkdir()
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(empty_clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\n")

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    out = capsys.readouterr().out
    guard_src = str(empty_clone / "bin" / "shell-init-guard.py")
    assert f"shell_init_guard: skipped (guard script absent: {guard_src})" in out
    assert "not found" not in out
    assert rc_path.read_text() == "# pre-existing content\n"


def test_graceful_skip_when_guard_script_not_readable(capsys, monkeypatch, tmp_path):
    """Condition 3 of 3 — the guard file is present but `python3` could not
    read it. Readability, not the execute bit, is what decides whether the
    interpreter can run it; the message names that condition and no other."""
    clone = tmp_path / "claude-klabauter-unreadable"
    (clone / "bin").mkdir(parents=True)
    guard = clone / "bin" / "shell-init-guard.py"
    guard.write_text("#!/usr/bin/env python3\nprint('')\n")
    guard.chmod(0o644)
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\n")

    # `chmod(0o000)` is a no-op for the owner on Windows and for root on
    # POSIX, so the unreadable state is stubbed at the seam the production
    # code queries rather than asked of the filesystem. The real_access
    # fallback keeps every other probe genuine.
    real_access = seam.os.access

    def _fake_access(path, mode):
        if mode == os.R_OK and os.fspath(path) == str(guard):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(seam.os, "access", _fake_access)

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"shell_init_guard: skipped (guard script unreadable: {guard})" in out
    assert "not found" not in out
    assert "absent" not in out
    assert rc_path.read_text() == "# pre-existing content\n"


def test_non_executable_guard_still_installs(capsys, monkeypatch, tmp_path, claude_klabauter_clone):
    """The exec bit is not a condition. Claude-klabauter's `bin/` is committed 100644
    and invoked through `python3 <path>`, and no mode bit survives a Windows
    checkout — gating on one skipped the install on every clone, under a
    message that named a missing repo (bug fix, 2026-08-22)."""
    guard = claude_klabauter_clone / "bin" / "shell-init-guard.py"
    # Precondition holds only where an execute bit exists to be absent from —
    # Windows' os.access(X_OK) reports true for any readable file regardless
    # of chmod, so there is no "non-executable" state to assert there. The
    # behavioral assertions below (installed, never "skipped") are what this
    # test actually guards, on every platform.
    if sys.platform != "win32":
        assert not os.access(guard, os.X_OK)
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    rc_path.write_text("# pre-existing content\n")

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"shell_init_guard: installed ({rc_path})" in out
    assert "skipped" not in out
    assert seam.SENTINEL in rc_path.read_text()


def test_seam_never_consults_the_execute_bit(monkeypatch, tmp_path, claude_klabauter_clone):
    """Fails loudly if any future edit re-introduces an X_OK probe on the
    guard path, at either the install gate or anywhere else in `main()`."""
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / ".bashrc"
    real_access = seam.os.access
    x_ok_probes = []

    def _recording_access(path, mode):
        if mode == os.X_OK:
            x_ok_probes.append(os.fspath(path))
        return real_access(path, mode)

    monkeypatch.setattr(seam.os, "access", _recording_access)

    assert seam.main(["--rc", str(rc_path)]) == 0
    assert x_ok_probes == []


def test_rc_created_fresh_when_absent(monkeypatch, tmp_path, claude_klabauter_clone):
    """rc_path doesn't exist yet — a genuinely fresh operator env."""
    monkeypatch.setenv("REPO_CLAUDE_KLABAUTER", str(claude_klabauter_clone))
    rc_path = tmp_path / "new-rc-file"
    assert not rc_path.exists()

    rc = seam.main(["--rc", str(rc_path)])

    assert rc == 0
    assert rc_path.exists()
    assert seam.SENTINEL in rc_path.read_text()
