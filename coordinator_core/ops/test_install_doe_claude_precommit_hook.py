"""Tests for coordinator_core.ops.install_doe_claude_precommit_hook.

Deliberately BEHAVIORAL where it matters: the hook body is executed via `sh`
against stub gate scripts, not merely grepped for marker substrings — the
now-deleted `coordinator_core.ops.install_claude_klabauter_precommit_hook`'s own
test-file docstring carried why substring presence alone is an insufficient
regression guard (a gate can be textually present but dead code after a
stray trailing `exit 0`).

The highest-priority guard in this file is the exit-code-clamping suite:
this hook must NEVER exit anything other than 0 or 1, even when the
underlying gate script exits some other nonzero code — see the module
docstring's "Exit-code clamping" section for why exit 2 (or anything but
0/1) from a pre-commit hook is a bricking-class failure, not a cosmetic one.

Second-highest priority: the identity-resolution suite around
`_resolve_doe_root()` / `_resolve_doe_claude_target()` — an unresolved
`repos.doe_claude` (AC3) must be a clean advisory skip, never a traceback,
never a block.

Every test builds throwaway repos under `tmp_path` — never this live repo,
and never the real DoE-claude checkout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.install_doe_claude_precommit_hook import (
    _GATE_REGISTRY,
    main,
)
import coordinator_core.ops.install_doe_claude_precommit_hook as _mod
from coordinator_core.testing.sh_interpreter import require_sh_interpreter
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

# Declared, not excused: this file behaviorally executes the generated hook body
# via real `sh` against stub gate scripts, and validates it with `sh -n`, to prove
# the exit-code-clamping contract (never anything but 0/1) that no textual/mocked
# check could demonstrate -- the property under test is the hook script's real
# subprocess exit-code behaviour. Every test builds its own throwaway tmp_path
# repo via `_git_init`, so there is no shared state to hoist.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, **no_console_passthrough_kwargs()
    )


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-commit"


def _write_stub_gates(gate_dir: Path, exit_map: dict | None = None) -> None:
    """Write a stub Python script for every registered gate. Each stub prints
    `RAN:<filename>` and exits 0, unless overridden via `exit_map`."""
    exit_map = exit_map or {}
    gate_dir.mkdir(parents=True, exist_ok=True)
    for gate in _GATE_REGISTRY:
        rc = exit_map.get(gate.filename, 0)
        script = gate_dir / gate.filename
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
        [require_sh_interpreter(), str(hook)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_map: dict | None = None) -> Path:
    """Set up a throwaway repo that resolves as "DoE-claude" (identity
    anchor monkeypatched to this tmp_path repo), stub its gate script(s),
    and run the installer once. Returns the repo path."""
    repo = tmp_path / "fake-doe-claude"
    repo.mkdir()
    _git_init(repo)
    # Gate scripts live at the repo-root-relative location the emitted
    # hook actually references (coordinator/hooks/scripts/), not an
    # arbitrary dir.
    gate_dir = repo / "coordinator" / "hooks" / "scripts"
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(repo))
    _write_stub_gates(gate_dir, exit_map=exit_map)
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


def test_non_doe_claude_repo_root_is_skipped_cleanly(tmp_path, monkeypatch, capsys):
    other = tmp_path / "some-other-repo"
    other.mkdir()
    _git_init(other)
    # Identity anchor resolves somewhere else entirely, but that somewhere
    # else is a real, existing directory — distinct from the stale-registry
    # case (test_stale_nonempty_doe_root_gets_its_own_registry_advisory),
    # which fires its own advisory when the resolved doe_root does not
    # canon at all.
    not_this_one = tmp_path / "not-this-one"
    not_this_one.mkdir()
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(not_this_one))

    rc = main([str(other)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not DoE-claude" in err
    assert not _hook_path(other).exists()


def test_default_target_is_cwd(tmp_path, monkeypatch, capsys):
    other = tmp_path / "cwdrepo"
    other.mkdir()
    _git_init(other)
    not_this_one = tmp_path / "not-this-one"
    not_this_one.mkdir()
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(not_this_one))
    monkeypatch.chdir(other)
    rc = main([])
    assert rc == 0
    assert "not DoE-claude" in capsys.readouterr().err


def test_stale_nonempty_doe_root_gets_its_own_registry_advisory(tmp_path, monkeypatch, capsys):
    """A non-empty but stale/wrong `repos.doe_claude` (moved, renamed, or
    deleted checkout) must not be conflated with either the empty-value
    advisory (AC3) or the genuine not-DoE-claude message -- it names the
    REGISTRY as the suspect, not the target repo, and still skips cleanly
    (exit 0), never a block."""
    repo = tmp_path / "some-repo"
    repo.mkdir()
    _git_init(repo)
    stale = tmp_path / "moved-or-deleted-doe-claude"
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(stale))

    rc = main([str(repo)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ADVISORY" in err
    assert "repos.doe_claude" in err
    assert str(stale) in err
    assert "not DoE-claude" not in err
    assert not _hook_path(repo).exists()


def test_unresolved_doe_root_is_a_clean_advisory_skip_not_a_block(tmp_path, monkeypatch, capsys):
    """AC3: `read_doe_root_pointer()` returns "" on an unresolved
    `repos.doe_claude` and never raises. This installer must treat that as a
    clean skip (exit 0) with a named advisory on stderr -- never a
    traceback, never a nonzero exit."""
    repo = tmp_path / "some-repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: "")

    rc = main([str(repo)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ADVISORY" in err
    assert "repos.doe_claude" in err
    assert not _hook_path(repo).exists()


# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------

def test_fresh_install_writes_executable_hook_containing_the_gate(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "fake-doe-claude"
    repo.mkdir()
    _git_init(repo)
    gate_dir = repo / "coordinator" / "hooks" / "scripts"
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(repo))
    _write_stub_gates(gate_dir)

    rc = main([str(repo)])
    assert rc == 0
    hook = _hook_path(repo)
    assert hook.is_file()
    content = hook.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh\n")
    for gate in _GATE_REGISTRY:
        assert gate.marker in content
        # AC4: the emitted gate path is repo-root-relative, under
        # coordinator/hooks/scripts/ -- not coordinator/bin/.
        assert f"coordinator/hooks/scripts/{gate.filename}" in content
    assert content.rstrip("\n").endswith("exit 0")
    if os.name != "nt":
        assert os.access(hook, os.X_OK)
    assert "installed" in capsys.readouterr().err


def test_fresh_install_gate_actually_executes(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 0
    assert any(line.startswith("RAN:") for line in result.stdout.splitlines())


def test_missing_gate_script_advisory_fires_at_install_time(tmp_path, monkeypatch, capsys):
    """`guard-doctrine-surface-ratio.py` is DoE-claude's own deliverable and
    is not expected to exist in claude-klabauter's tree -- when this installer
    is exercised WITHOUT the stub gate present, the install-time ADVISORY
    must fire (Anti-scope: this is correct behavior here, not a bug)."""
    repo = tmp_path / "fake-doe-claude"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(repo))

    rc = main([str(repo)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ADVISORY" in err
    for gate in _GATE_REGISTRY:
        assert gate.filename in err


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
    repo = tmp_path / "fake-doe-claude"
    repo.mkdir()
    _git_init(repo)
    gate_dir = repo / "coordinator" / "hooks" / "scripts"
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(repo))
    # The gate's stub exits 1 -- proves it isn't dead code after the human
    # hook's own trailing `exit 0`.
    _write_stub_gates(gate_dir, exit_map={_GATE_REGISTRY[-1].filename: 1})

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


def test_foreign_hook_ending_in_unconditional_exit_1_swallows_appended_gates(
    tmp_path, monkeypatch, capsys
):
    """Known limitation, documented not fixed here: `_strip_trailing_exit0`
    only strips a bare trailing `exit 0`. A foreign hook whose last line is
    an unconditional `exit 1` (or any other unconditional exit) is NOT
    stripped, so every gate appended after it is textually present but
    unreachable dead code -- the foreign hook's own exit fires first and the
    shell never reaches the appended lines. `_strip_trailing_exit0` is shared
    plumbing with the ratified claude-klabauter installer; changing it here is out of
    scope for this module."""
    repo = tmp_path / "fake-doe-claude"
    repo.mkdir()
    _git_init(repo)
    gate_dir = repo / "coordinator" / "hooks" / "scripts"
    monkeypatch.setattr(_mod, "_resolve_doe_root", lambda: str(repo))
    _write_stub_gates(gate_dir)

    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\necho "custom hook"\nexit 1\n', encoding="utf-8")
    if os.name != "nt":
        os.chmod(hook, 0o755)

    rc = main([str(repo)])
    assert rc == 0
    content = hook.read_text(encoding="utf-8")
    for gate in _GATE_REGISTRY:
        assert gate.marker in content  # textually present ...
    assert "appended gate(s)" in capsys.readouterr().err

    result = _run_hook(hook, repo)
    # ... but unreachable: the foreign hook's own `exit 1` fires first, so
    # the appended gate never actually runs.
    assert result.returncode == 1
    assert "custom hook" in result.stdout
    assert not any(
        line.startswith("RAN:") for line in (result.stdout + result.stderr).splitlines()
    ), "known limitation: appended gate is dead code after a foreign hook's unconditional non-`exit 0` trailing line"


# ---------------------------------------------------------------------------
# Fail loud — missing script / missing interpreter, always exit 1
# ---------------------------------------------------------------------------

def test_missing_gate_script_blocks_loudly_with_exit_1(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    gate_dir = _mod._bin_dir(str(repo))
    gate = _GATE_REGISTRY[0]
    (gate_dir / gate.filename).unlink()

    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert gate.label in result.stderr
    assert gate.marker in result.stderr


def test_missing_python_interpreter_blocks_loudly_with_exit_1(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    result = _run_hook(_hook_path(repo), repo, path_env="/nonexistent-empty-path-dir")
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "no python interpreter found" in result.stderr


def test_missing_gate_script_override_bypasses_the_block(tmp_path, monkeypatch):
    repo = _install(tmp_path, monkeypatch)
    gate_dir = _mod._bin_dir(str(repo))
    gate = _GATE_REGISTRY[0]
    (gate_dir / gate.filename).unlink()

    result = _run_hook(_hook_path(repo), repo, extra_env={gate.override_env: "1"})
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr


# ---------------------------------------------------------------------------
# Exit-code clamping — the highest-priority guard in this file: 1, NEVER 2.
# ---------------------------------------------------------------------------

def test_gate_script_exit_2_is_clamped_to_1(tmp_path, monkeypatch):
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
    """Any surprise nonzero exit code from the gate script -- not just 1 or 2
    -- must still surface as exactly 1, never propagated raw."""
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 47})
    result = _run_hook(_hook_path(repo), repo)
    assert result.returncode == 1


def test_override_bypasses_a_clamped_gate_failure(tmp_path, monkeypatch):
    gate = _GATE_REGISTRY[0]
    repo = _install(tmp_path, monkeypatch, exit_map={gate.filename: 2})
    result = _run_hook(_hook_path(repo), repo, extra_env={gate.override_env: "1"})
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr


def test_hook_body_never_contains_a_bare_exit_dollar_question():
    """Regression guard for the exact defect this clamping fixes: propagating
    a raw gate exit code (including something other than 0/1) straight out
    of the hook via a bare `|| exit $?`. This module's emitted body must
    never contain that shape."""
    content = _mod._hook_body(_GATE_REGISTRY)
    assert "exit $?" not in content
    assert "|| exit" not in content


def test_hook_sh_syntax_is_valid(tmp_path, monkeypatch):
    """`sh -n` (parse-only, no execution) must accept the emitted hook body
    on every code path this module can emit -- a syntax error inside a hook
    is itself a way to leak a non-0/1 exit status (a shell parse error is
    commonly exit 2)."""
    repo = _install(tmp_path, monkeypatch)
    result = subprocess.run(
        [require_sh_interpreter(), "-n", str(_hook_path(repo))],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr


def test_hook_always_exits_0_or_1_across_every_branch(tmp_path, monkeypatch):
    """Sweep every branch this hook can take (clean, missing script, missing
    interpreter, gate rc 1, gate rc 2, each with and without override) and
    assert the observed exit code is always in {0, 1} -- never anything
    else."""
    gate = _GATE_REGISTRY[0]
    seen_codes = set()

    for exit_map, path_env, extra_env in [
        (None, None, None),
        ({gate.filename: 1}, None, None),
        ({gate.filename: 2}, None, None),
        ({gate.filename: 2}, None, {gate.override_env: "1"}),
        (None, "/nonexistent-empty-path-dir", None),
    ]:
        case_root = tmp_path / f"case_{path_env}_{extra_env}_{exit_map}".replace("/", "_")
        case_root.mkdir(parents=True, exist_ok=True)
        repo = _install(case_root, monkeypatch, exit_map=exit_map)
        result = _run_hook(_hook_path(repo), repo, path_env=path_env, extra_env=extra_env)
        seen_codes.add(result.returncode)

    assert seen_codes <= {0, 1}
    assert 1 in seen_codes  # sanity: at least one branch actually blocked


def test_hook_survives_the_repo_being_relocated(tmp_path, monkeypatch):
    """The point of emitting a repo-root-relative gate path.

    An absolute path baked at install time breaks the moment the checkout is
    moved or renamed. Install, move the whole repo, and the hook must still
    resolve its gate and pass on a clean index -- with no re-install."""
    repo = _install(tmp_path, monkeypatch)
    hook_before = _hook_path(repo).read_text()

    moved = tmp_path / "relocated-elsewhere"
    repo.rename(moved)

    result = _run_hook(_hook_path(moved), moved)
    assert result.returncode == 0, (
        f"relocated repo must still resolve its gate: {result.stdout}\n{result.stderr}"
    )
    assert "RAN:" in (result.stdout + result.stderr), "the gate must actually have executed"

    assert _hook_path(moved).read_text() == hook_before
    assert str(repo) not in hook_before


def test_stale_gate_body_is_refreshed_not_reported_as_installed(tmp_path, monkeypatch):
    """Marker presence is not currency. A gate's marker survives any change
    to the body that runs it, so keying idempotency on the marker leaves an
    OUTDATED hook in place while reporting success."""
    repo = _install(tmp_path, monkeypatch)
    hook = _hook_path(repo)

    current = hook.read_text()
    stale = current.replace(
        '_gate_script="coordinator/hooks/scripts/', '_gate_script="/baked/absolute/path/'
    )
    assert stale != current, "fixture must actually differ from the current body"
    hook.write_text(stale)

    assert main([str(repo)]) == 0
    refreshed = hook.read_text()
    assert refreshed == current, "a stale body this installer wrote must be rewritten"
    assert "/baked/absolute/path/" not in refreshed

    assert _run_hook(hook, repo).returncode == 0


def test_custom_hook_with_stale_gate_region_is_surfaced_not_silently_rewritten(
    tmp_path, monkeypatch
):
    """A hand-edited hook is never rewritten around. A stale gate region
    inside one is reported loudly (nonzero) so a human resolves it."""
    repo = _install(tmp_path, monkeypatch)
    hook = _hook_path(repo)

    gate = _GATE_REGISTRY[0]
    custom = (
        "#!/bin/sh\n# someone's own hook\necho custom >&2\n"
        + hook.read_text().replace(
            '_gate_script="coordinator/hooks/scripts/', '_gate_script="/baked/absolute/path/'
        )
    )
    hook.write_text(custom)

    rc = main([str(repo)])
    assert rc == 1, "a stale region in a foreign hook must be surfaced, not silently accepted"
    assert hook.read_text() == custom, "a custom hook must not be rewritten"
    assert gate.marker in hook.read_text()
