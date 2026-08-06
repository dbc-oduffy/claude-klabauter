"""
coordinator_core.install.test_sandbox_check — parity tests for
coordinator_core.install.sandbox_check.

Port of: install-sandbox-check.sh (example-doctrine-repo b5a4192c, 2026-07-20).

Independently re-derives expected behavior (Reporter counting semantics,
subprocess timeout/stdin-guard behavior, example-doctrine-repo-clone resolution precedence,
transport-vs-business exit-code contract) from the bash oracle's own
documented contract rather than re-asserting this port's own transcription.
Also drives a full :func:`run_all` pass against a hand-built minimal fake
Example-doctrine-repo clone (NOT the real sibling example-doctrine-repo checkout — this validator's own
job is to exercise *other* install scripts via subprocess, so a synthetic
fixture with a stub ``claude-doe`` is the honest independent oracle here,
not a copy of the real coordinator/bin/ tree).

Spec backlink: docs/plans/2026-07-04-doe-maximalist-execution-plugin-dir.md § W4.1
Port backlink: docs/plans/2026-07-16-clean-slate-residual-migration.md
    (BIG_PORT Wave C, item install-sandbox-check)
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.install.sandbox_check import (
    Reporter,
    SandboxCheckTransportError,
    _run,
    main,
    resolve_doe_clone,
    run_all,
)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def test_reporter_counts_pass_fail_independently_of_skip_and_info():
    r = Reporter()
    r.ok("a")
    r.ok("b")
    r.bad("c")
    r.skip("d")
    r.info("e")
    assert r.pass_count == 2
    assert r.fail_count == 1
    assert any(line.startswith("PASS: a") for line in r.lines)
    assert any(line.startswith("FAIL: c") for line in r.lines)
    assert any(line.startswith("SKIP: d") for line in r.lines)
    assert any(line.startswith("INFO: e") for line in r.lines)


# ---------------------------------------------------------------------------
# _run — addendum A2 (timeout + stdin=DEVNULL), A4 (no-console on nt)
# ---------------------------------------------------------------------------


def test_run_applies_stdin_devnull_so_a_stdin_reading_child_does_not_hang():
    # `cat` with no args reads stdin until EOF; DEVNULL supplies immediate EOF.
    cp = _run(["cat"], timeout=5)
    assert cp.returncode == 0
    assert cp.stdout == ""


def test_run_timeout_converts_to_synthetic_rc_124_not_an_exception():
    cp = _run(["sleep", "5"], timeout=1)
    assert cp.returncode == 124
    assert "TIMEOUT" in cp.stderr


def test_run_missing_executable_converts_to_synthetic_rc_127_not_an_exception():
    cp = _run(["/no/such/binary/exists-xyz"], timeout=5)
    assert cp.returncode == 127


# ---------------------------------------------------------------------------
# resolve_doe_clone — REPO_EXAMPLE_DOCTRINE_REPO precedence over machine-local
# ---------------------------------------------------------------------------


def test_resolve_doe_clone_prefers_env_var(monkeypatch):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "/tmp/fake-doe-clone")
    clone, resolved = resolve_doe_clone()
    assert resolved is True
    assert clone == "/tmp/fake-doe-clone"


def test_resolve_doe_clone_returns_unresolved_when_nothing_available(monkeypatch):
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir-xyz")
    clone, resolved = resolve_doe_clone()
    assert resolved is False
    assert clone == ""


# ---------------------------------------------------------------------------
# run_all -- transport-failure path (addendum rule 3b, dedicated exit code)
# ---------------------------------------------------------------------------


def test_run_all_raises_transport_error_when_sandbox_creation_fails(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    with pytest.raises(SandboxCheckTransportError):
        run_all()


def test_main_returns_dedicated_transport_code_on_sandbox_creation_failure(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    rc = main([])
    assert rc == 3  # dedicated transport code -- must NOT collide with business 0/1


def test_main_help_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0


def test_main_unknown_argument_exits_transport_code(monkeypatch):
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    rc = main(["--totally-unknown-flag"])
    assert rc == 3


# ---------------------------------------------------------------------------
# run_all -- graceful degrade when example-doctrine-repo clone is unresolved (FAMILY-I contract)
# ---------------------------------------------------------------------------


def test_run_all_never_raises_when_doe_clone_unresolved(monkeypatch):
    """Mirrors the bash oracle's own graceful-skip design (lines 79-98):
    an unresolved clone must degrade every clone-dependent check to SKIP,
    never crash the harness. The single expected FAIL is the oracle's own
    'repos.example_doctrine_repo not resolved' assertion -- everything downstream must
    still complete without an uncaught exception."""
    monkeypatch.setattr(
        "coordinator_core.install.sandbox_check.resolve_doe_clone", lambda: ("", False)
    )
    r, sandbox = run_all()
    assert not os.path.isdir(sandbox)  # cleaned up (default keep_sandbox=False)
    assert r.fail_count >= 1  # the "not resolved" assertion itself
    assert any("repos.example_doctrine_repo not resolved" in line for line in r.lines)


def test_run_all_keep_sandbox_preserves_directory(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.install.sandbox_check.resolve_doe_clone", lambda: ("", False)
    )
    r, sandbox = run_all(keep_sandbox=True)
    try:
        assert os.path.isdir(sandbox)
    finally:
        import shutil

        shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# run_all -- full pass against a synthetic fake example-doctrine-repo clone (independent fixture)
# ---------------------------------------------------------------------------


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_doe_clone(tmp_path: Path) -> Path:
    """A minimal synthetic example-doctrine-repo-clone shape: .git dir, coordinator/ dir, a
    stub claude-doe wrapper that emits the exact contract line the
    validator's own checks 7/7b assert on, and a stub shim template (the
    one on-disk artifact ``gen_claude_doe_shim`` still requires, since it
    has no default template path of its own -- see that module's
    docstring). The example-doctrine-repo-side ``.sh`` bridges themselves (gen-doe-root-
    pointer.sh, gen-claude-doe-shim.sh, gen-settings-hooks.sh,
    resolve-coordinator-clone.sh) are deliberately ABSENT -- the port no
    longer reads any of them (native in-process calls replace the former
    subprocess-exec), so their absence must NOT block the checks that used
    to gate on ``os.path.isfile(<script>)``; only the empty ``hooks.json``
    (no hook entries to seed) and this sandbox's lack of a real
    ``machine-local``/registry keep a few checks legitimately RED."""
    clone = tmp_path / "fake-doe-clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "coordinator" / "bin").mkdir(parents=True)
    (clone / "coordinator" / "lib").mkdir(parents=True)
    (clone / "coordinator" / "hooks").mkdir(parents=True)
    (clone / "coordinator" / "hooks" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
    (clone / "coordinator" / "templates" / "shell").mkdir(parents=True)
    (clone / "coordinator" / "templates" / "shell" / "claude-doe-shim.sh.tmpl").write_text(
        # Minimal stand-in for the real example-doctrine-repo template: exports REPO_EXAMPLE_DOCTRINE_REPO
        # from the .doe-root pointer at SOURCE time (not inside the function
        # body -- checks 9/AC2 source this file directly and read the env var
        # back without ever calling claude()) and defines a claude() function,
        # using variable expansion only (no hardcoded $HOME/machine path).
        'if [ -f "${CLAUDE_HOME:-$HOME}/.claude/.doe-root" ]; then\n'
        '  export REPO_EXAMPLE_DOCTRINE_REPO="$(cat "${CLAUDE_HOME:-$HOME}/.claude/.doe-root")"\n'
        "fi\n"
        "\n"
        "claude() {\n"
        '  command claude "$@"\n'
        "}\n",
        encoding="utf-8",
    )

    # Python, not sh: the real claude-doe was ported from bash to python3
    # (example-doctrine-repo commit), and sandbox_check.py invokes it via
    # ``[sys.executable, wrapper_path, ...]`` directly (not shebang-exec
    # through a shell), so this fixture must be python source for that
    # invocation to succeed.
    wrapper = clone / "coordinator" / "bin" / "claude-doe"
    _write_executable(
        wrapper,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"clone = {str(clone)!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--dry-run':\n"
        "    print(f'exec claude --plugin-dir {clone}/coordinator')\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
    )
    return clone


def test_run_all_full_pass_against_synthetic_fake_clone_no_crash(fake_doe_clone: Path, monkeypatch):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(fake_doe_clone))
    coordinator_root = str(fake_doe_clone / "coordinator")

    r, sandbox = run_all(coordinator_root_override=coordinator_root)

    assert not os.path.isdir(sandbox)
    # example-doctrine-repo clone + coordinator/ dir + claude-doe wrapper checks must PASS.
    assert any("example-doctrine-repo clone present" in line for line in r.lines)
    assert any("example-doctrine-repo coordinator/ dir present" in line for line in r.lines)
    assert any("claude-doe wrapper source present" in line for line in r.lines)
    assert any("claude-doe --dry-run emitted exec line with --plugin-dir" in line for line in r.lines)
    assert any(
        "claude-doe --dry-run exec line references example-doctrine-repo coordinator dir" in line for line in r.lines
    )
    # Native in-process pointer/shim/resolver calls must succeed even though
    # the example-doctrine-repo .sh bridges are absent from this fixture (the whole point of
    # the port -- no dependency on those files existing).
    assert any("gen_doe_root_pointer.main() exited 0 against sandbox" in line for line in r.lines)
    assert any(".doe-root content matches registry repos.example_doctrine_repo" in line for line in r.lines)
    assert any("gen_claude_doe_shim.main() exited 0 against sandbox" in line for line in r.lines)
    assert any("claude-doe-shim.sh defines a claude() function" in line for line in r.lines)
    assert any(
        "AC5: resolve_coordinator_clone.resolve_content_root()" in line and "returned expected" in line
        for line in r.lines
    )
    # Empty hooks.json (no hook entries in this fixture) is the one
    # legitimately-still-red gen_settings_hooks assertion -- not caused by
    # bash removal, a pre-existing fixture limitation.
    assert any("settings.json hooks array empty" in line for line in r.lines)
    assert r.fail_count > 0
    assert r.pass_count > 0  # and the fixture's own present pieces PASS
