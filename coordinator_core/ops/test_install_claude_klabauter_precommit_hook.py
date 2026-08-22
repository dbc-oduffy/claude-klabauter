"""Tests for coordinator_core.ops.install_claude_klabauter_precommit_hook.

Deliberately BEHAVIORAL where it matters: the hook body is executed via `sh`
against stub gate scripts, not merely grepped for marker substrings — see
`coordinator_core.ops.install_meta_repo_precommit_hook`'s own test-file
docstring for why substring presence alone is an insufficient regression
guard (a gate can be textually present but dead code after a stray trailing
`exit 0`).

The highest-priority guard in this file is the exit-code-clamping suite:
this hook must NEVER exit anything other than 0 or 1, even when the
underlying gate script (`detect-staged-rollback.py`) exits 2 (its own
documented engine-root-resolution/transport-failure code) — see the module
docstring's "Exit-code clamping" section for why exit 2 from a pre-commit
hook is a bricking-class failure, not a cosmetic one.

Every test builds throwaway repos under `tmp_path` — never this live repo.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.install_claude_klabauter_precommit_hook import (
    _GATE_REGISTRY,
    main,
)
import coordinator_core.ops.install_claude_klabauter_precommit_hook as _mod
from coordinator_core.testing.sh_interpreter import require_sh_interpreter

# Declared, not excused: this file behaviorally executes the generated hook body
# via real `sh` against stub gate scripts, and validates it with `sh -n`, to prove
# the exit-code-clamping contract (never anything but 0/1) that no textual/mocked
# check could demonstrate -- the property under test is the hook script's real
# subprocess exit-code behaviour. Every test builds its own throwaway tmp_path
# repo via `_git_init`, so there is no shared state to hoist. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the route
# for this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-commit"


def _write_stub_gates(fake_bin: Path, exit_map: dict | None = None) -> None:
    """Write a stub Python script for every registered gate. Each stub prints
    `RAN:<filename>` and exits 0, unless overridden via `exit_map`."""
    exit_map = exit_map or {}
    fake_bin.mkdir(parents=True, exist_ok=True)
    for gate in _GATE_REGISTRY:
        rc = exit_map.get(gate.filename, 0)
        script = fake_bin / gate.filename
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"print('RAN:{gate.filename}')\n"
            f"sys.exit({rc})\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(script, 0o755)


def _run_hook(
    hook: Path, cwd: Path, path_env: str | None = None, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path_env is not None:
        env["PATH"] = path_env
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [require_sh_interpreter(), str(hook)], cwd=str(cwd), capture_output=True, text=True, env=env
    )


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_map: dict | None = None) -> Path:
    """Set up a throwaway repo that resolves as "claude-klabauter" (identity
    anchor monkeypatched to this tmp_path repo), stub its gate script(s),
    and run the installer once. Returns the repo path."""
    repo = tmp_path / "fake-claude-klabauter"
    repo.mkdir()
    _git_init(repo)
    # Gate scripts live at the repo-root-relative location the emitted
    # hook actually references (coordinator/bin/), not an arbitrary dir.
    fake_bin = repo / "coordinator" / "bin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(repo))
    _write_stub_gates(fake_bin, exit_map=exit_map)
    assert main([str(repo)]) == 0
    return repo


# ---------------------------------------------------------------------------
# Identity guard
# ---------------------------------------------------------------------------

def test_target_not_a_git_repo(tmp_path, capsys):
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    rc = main([str(notgit)])
    assert rc == 0
    assert "not in a git repo — skipping." in capsys.readouterr().err


def test_non_claude_klabauter_repo_root_is_skipped_cleanly(tmp_path, monkeypatch, capsys):
    other = tmp_path / "some-other-repo"
    other.mkdir()
    _git_init(other)
    # Identity anchor points somewhere else entirely — `other` is not it.
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(tmp_path / "not-this-one"))

    rc = main([str(other)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not claude-klabauter" in err
    assert not _hook_path(other).exists()


def test_default_target_is_cwd(tmp_path, monkeypatch, capsys):
    other = tmp_path / "cwdrepo"
    other.mkdir()
    _git_init(other)
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(tmp_path / "not-this-one"))
    monkeypatch.chdir(other)
    rc = main([])
    assert rc == 0
    assert "not claude-klabauter" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------

def test_fresh_install_writes_executable_hook_containing_the_gate(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "fake-claude-klabauter"
    repo.mkdir()
    _git_init(repo)
    # Gate scripts live at the repo-root-relative location the emitted
    # hook actually references (coordinator/bin/), not an arbitrary dir.
    fake_bin = repo / "coordinator" / "bin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(repo))
    _write_stub_gates(fake_bin)

    rc = main([str(repo)])
    assert rc == 0
    hook = _hook_path(repo)
    assert hook.is_file()
    content = hook.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh\n")
    for gate in _GATE_REGISTRY:
        assert gate.marker in content
    assert content.rstrip("\n").endswith("exit 0")
    if os.name != "nt":
        # git runs this hook directly, so the POSIX exec bit is the property under
        # test. Windows has no equivalent and os.access(X_OK) degrades to F_OK there,
        # which would assert nothing while reading as though it did.
        assert os.access(hook, os.X_OK)
    assert "installed" in capsys.readouterr().err


def test_fresh_install_gate_actually_executes(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 0
    assert any(line.startswith("RAN:") for line in result.stdout.splitlines())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_rerun_is_idempotent_and_does_not_duplicate(tmp_path, monkeypatch, capsys):
    repo = _install(tmp_path, monkeypatch)
    before = _hook_path(repo).read_text(encoding="utf-8")

    rc = main([str(repo)])
    assert rc == 0
    after = _hook_path(repo).read_text(encoding="utf-8")
    assert before == after
    assert "already installed" in capsys.readouterr().err
    for gate in _GATE_REGISTRY:
        assert after.count(f"# --- Gate: {gate.label} ({gate.marker}) ---") == 1


# ---------------------------------------------------------------------------
# Append path — foreign existing hook, including a trailing `exit 0` trap
# ---------------------------------------------------------------------------

def test_appends_to_existing_foreign_hook_and_strips_trailing_exit0(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "fake-claude-klabauter"
    repo.mkdir()
    _git_init(repo)
    # Gate scripts live at the repo-root-relative location the emitted
    # hook actually references (coordinator/bin/), not an arbitrary dir.
    fake_bin = repo / "coordinator" / "bin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(repo))
    # The last gate's stub exits 1 — proves it isn't dead code after the
    # human hook's own trailing `exit 0`.
    _write_stub_gates(fake_bin, exit_map={_GATE_REGISTRY[-1].filename: 1})

    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\necho "custom hook"\nexit 0\n', encoding="utf-8")
    if os.name != "nt":
        os.chmod(hook, 0o755)

    rc = main([str(repo)])
    assert rc == 0
    content = hook.read_text(encoding="utf-8")
    assert 'echo "custom hook"' in content
    for gate in _GATE_REGISTRY:
        assert gate.marker in content
    assert "appended gate(s)" in capsys.readouterr().err

    result = _run_hook(hook, repo)
    assert result.returncode == 1
    assert "custom hook" in result.stdout
    assert any(line.startswith("RAN:") for line in result.stdout.splitlines())


# ---------------------------------------------------------------------------
# Fail loud — missing script / missing interpreter, always exit 1
# ---------------------------------------------------------------------------

def test_missing_gate_script_blocks_loudly_with_exit_1(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    fake_bin = _mod._bin_dir()
    gate = _GATE_REGISTRY[0]
    (fake_bin / gate.filename).unlink()

    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert gate.label in result.stderr
    assert gate.marker in result.stderr


def test_stale_baked_interpreter_blocks_loudly_with_exit_1(tmp_path, monkeypatch):
    """2026-08-21 (C17): the emitted hook no longer walks `$PATH` at commit
    time — it execs an absolute interpreter path baked in at install time
    (`_baked_interpreter_path`). Removing python from `$PATH` therefore no
    longer breaks the hook (AC14 covers that this hook stays interpreter-
    path-independent). The contract that must still hold: if the BAKED path
    itself goes stale (interpreter moved/uninstalled since install), the
    hook's self-heal (`[ -x "$_py" ] || _py=""`) clears it and the existing
    missing-interpreter CANNOT-PROCEED branch fires — loud BLOCK, exit 1,
    never a silent skip — with remediation naming a RUNNABLE SCRIPT (re-run
    the installer), never a slash command (project doctrine: cold-path
    remediation names a runnable script)."""
    repo = _install(tmp_path, monkeypatch)
    hook = _hook_path(repo)
    content = hook.read_text(encoding="utf-8")
    stale = content.replace(
        _mod._baked_interpreter_path(), "/nonexistent-baked-interpreter/python3"
    )
    assert stale != content, "fixture must actually stale the baked interpreter path"
    hook.write_text(stale, encoding="utf-8")

    result = _run_hook(hook, repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "interpreter" in result.stderr
    assert "cannot proceed" in result.stderr
    # Remediation names a runnable script (re-run the installer), never a
    # slash command.
    assert "install-claude-klabauter-precommit-hook" in result.stderr
    assert "re-run" in result.stderr


def test_missing_gate_script_override_bypasses_the_block(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    fake_bin = _mod._bin_dir()
    gate = _GATE_REGISTRY[0]
    (fake_bin / gate.filename).unlink()

    result = _run_hook(_hook_path(repo), repo, extra_env={gate.override_env: "1"})
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr


# ---------------------------------------------------------------------------
# Exit-code clamping — the highest-priority guard in this file: 1, NEVER 2.
# ---------------------------------------------------------------------------

def test_gate_script_exit_2_is_clamped_to_1(tmp_path, monkeypatch):
    """`detect-staged-rollback.py`'s own trampoline exits 2 on an engine-root
    resolution/import failure — a transport failure distinct from a finding.
    That 2 must never escape this hook: the wrapper clamps ANY nonzero gate
    exit code to exactly 1."""
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 2})

    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr


def test_gate_script_exit_1_also_clamped_to_1(tmp_path, monkeypatch):
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 1})
    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 1


def test_gate_script_arbitrary_nonzero_exit_clamped_to_1(tmp_path, monkeypatch):
    """Any surprise nonzero exit code from the gate script — not just 1 or 2
    — must still surface as exactly 1, never propagated raw."""
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 47})
    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 1


def test_transport_failure_exit_2_is_never_overridable(tmp_path, monkeypatch):
    """2026-08-21 fix: exit 2 is `detect-staged-rollback.py`'s own transport-
    failure/usage-error code, never a real check finding — it is deliberately
    ABSENT from `gate.finding_override_env`, so no key (not the rollback key,
    not the mass-deletion key, not both together) can turn it into a SKIP.
    Regression guard for the pre-fix shape, where a single flat
    `gate.override_env` bypassed ANY nonzero `$_gate_rc` including this one."""
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 2})
    all_keys = {k for keys in gate.finding_override_env.values() for k in keys}
    result = _run_hook(_hook_path(repo), repo, extra_env={k: "1" for k in all_keys})
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "SKIPPED" not in result.stderr


# ---------------------------------------------------------------------------
# Per-check exit-code override scoping. Check 1 (exact-blob rollback) was
# KILLED 2026-08-21 (PM ruling) — see `detect_staged_rollback`'s module
# docstring. The only surviving finding is the mass-deletion tripwire, which
# now IS `EXIT_MASS_DELETION_FINDING` (1) in `detect_staged_rollback`'s own
# contract, gated by exactly one key: `COORDINATOR_OVERRIDE_PRECOMMIT_MASS_
# DELETION`. There is no longer a second key or a second exit-code arm to
# scope against.
# ---------------------------------------------------------------------------

_MASS_DELETION_KEY = "COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION"


def test_mass_deletion_only_finding_is_bypassed_by_its_own_key(tmp_path, monkeypatch):
    """COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION reaches and honours the
    (sole surviving) mass-deletion finding branch, exit 1 —
    `detect_staged_rollback.EXIT_MASS_DELETION_FINDING`."""
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 1})
    result = _run_hook(_hook_path(repo), repo, extra_env={_MASS_DELETION_KEY: "1"})
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr


def test_arbitrary_unmapped_exit_code_blocks_unconditionally(tmp_path, monkeypatch):
    """A future surprise exit code the registry entry's `finding_override_env`
    does not name must fail LOUD, never fall through to success — mirrors the
    transport-failure-2 guard above for a code that isn't even a documented
    contract value."""
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 47})
    all_keys = {k for keys in gate.finding_override_env.values() for k in keys}
    result = _run_hook(_hook_path(repo), repo, extra_env={k: "1" for k in all_keys})
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr


def test_hook_body_never_contains_a_bare_exit_dollar_question():
    """Regression guard for the exact defect this clamping fixes: the
    meta-repo installer's own `_gate_block` uses `|| exit $?`, which
    propagates a raw gate exit code (including a 2) straight out of the
    hook. This module's emitted body must never contain that shape."""
    content = _mod._hook_body(_GATE_REGISTRY)
    assert "exit $?" not in content
    assert "|| exit" not in content


def test_hook_sh_syntax_is_valid(tmp_path, monkeypatch):
    """`sh -n` (parse-only, no execution) must accept the emitted hook body
    on every code path this module can emit — a syntax error inside a hook
    is itself a way to leak a non-0/1 exit status (a shell parse error is
    commonly exit 2)."""
    repo = _install(tmp_path, monkeypatch)
    result = subprocess.run(
        [require_sh_interpreter(), "-n", str(_hook_path(repo))], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_hook_always_exits_0_or_1_across_every_branch(tmp_path, monkeypatch):
    """Sweep every branch this hook can take (clean, missing script, missing
    interpreter, gate rc 1, gate rc 2, each with and without override) and
    assert the observed exit code is always in {0, 1} — never anything
    else."""
    gate = _GATE_REGISTRY[0]
    seen_codes = set()

    for case_index, (exit_map, path_env, extra_env) in enumerate([
        (None, None, None),
        ({gate.filename: 1}, None, None),
        ({gate.filename: 2}, None, None),
        ({gate.filename: 2}, None, {gate.override_env: "1"}),
        (None, "/nonexistent-empty-path-dir", None),
    ]):
        # Build a fresh throwaway repo per case (can't reuse the same repo
        # root across repeated _install calls within one test).
        # Index, not a repr of the case tuple: `exit_map`/`extra_env` are dicts
        # whose repr contains `:` and `'`, neither legal in a Windows path
        # component (WinError 267). The old form built the name from those
        # reprs, so this test could only ever run on POSIX -- and it is the
        # test pinning the 0/1 exit-code clamping contract on the commit hot
        # path, i.e. exactly the coverage a Windows-first repo cannot afford
        # to have silently dead. Windows is first-class (CLAUDE.md).
        case_root = tmp_path / f"case_{case_index}"
        case_root.mkdir(parents=True, exist_ok=True)
        repo = _install(case_root, monkeypatch, exit_map=exit_map)
        result = _run_hook(_hook_path(repo), repo, path_env=path_env, extra_env=extra_env)
        seen_codes.add(result.returncode)

    assert seen_codes <= {0, 1}
    assert 1 in seen_codes  # sanity: at least one branch actually blocked


def test_hook_survives_the_repo_being_relocated(tmp_path, monkeypatch):
    """The point of emitting a repo-root-relative gate path.

    An absolute path baked at install time breaks the moment the checkout is
    moved or renamed: the gate script is suddenly missing, and because a
    missing gate is (correctly) a hard BLOCK, every commit in the relocated
    clone fails until the installer is re-run. That is the same defect class
    that silently disarmed the meta-repo gates when the executable surface
    moved at b644d5a9.

    Install, move the whole repo, and the hook must still resolve its gate and
    pass on a clean index — with no re-install.
    """
    repo = _install(tmp_path, monkeypatch)
    hook_before = _hook_path(repo).read_text()

    moved = tmp_path / "relocated-elsewhere"
    repo.rename(moved)

    result = _run_hook(_hook_path(moved), moved)
    assert result.returncode == 0, (
        f"relocated repo must still resolve its gate: {result.stdout}\n{result.stderr}"
    )
    assert "RAN:" in (result.stdout + result.stderr), "the gate must actually have executed"

    # And the artifact itself is unchanged — nothing re-wrote it to make this pass.
    assert _hook_path(moved).read_text() == hook_before

    # Belt-and-braces: no absolute path from the original location leaked into
    # the body, which is what would have made this test pass by accident.
    assert str(repo) not in hook_before


def test_stale_gate_body_is_refreshed_not_reported_as_installed(tmp_path, monkeypatch):
    """Marker presence is not currency.

    A gate's marker survives any change to the body that runs it, so keying
    idempotency on the marker leaves an OUTDATED hook in place while reporting
    success — "looks installed while enforcing something else", the exact
    failure the gate chain exists to prevent, reproduced in the installer.
    Hit for real on 2026-07-28: the fix making the emitted gate path
    repo-root-relative was a silent no-op on a box that already had the
    absolute-path version.
    """
    repo = _install(tmp_path, monkeypatch)
    hook = _hook_path(repo)

    # Simulate an older body: same gate marker, different (stale) content.
    current = hook.read_text()
    stale = current.replace(
        '_gate_script="coordinator/bin/', '_gate_script="/baked/absolute/path/'
    )
    assert stale != current, "fixture must actually differ from the current body"
    hook.write_text(stale)

    assert main([str(repo)]) == 0
    refreshed = hook.read_text()
    assert refreshed == current, "a stale body this installer wrote must be rewritten"
    assert "/baked/absolute/path/" not in refreshed

    # And the refreshed hook still works.
    assert _run_hook(hook, repo).returncode == 0


def test_custom_hook_with_stale_gate_region_is_surfaced_not_silently_rewritten(
    tmp_path, monkeypatch
):
    """A hand-edited hook is never rewritten around. A stale gate region inside
    one is reported loudly (nonzero) so a human resolves it, rather than this
    installer clobbering someone else's edits or — worse — reporting success
    while the stale region keeps running."""
    repo = _install(tmp_path, monkeypatch)
    hook = _hook_path(repo)

    gate = _GATE_REGISTRY[0]
    custom = (
        "#!/bin/sh\n# someone's own hook\necho custom >&2\n"
        + hook.read_text().replace(
            '_gate_script="coordinator/bin/', '_gate_script="/baked/absolute/path/'
        )
    )
    hook.write_text(custom)

    rc = main([str(repo)])
    assert rc == 1, "a stale region in a foreign hook must be surfaced, not silently accepted"
    assert hook.read_text() == custom, "a custom hook must not be rewritten"
    assert gate.marker in hook.read_text()
