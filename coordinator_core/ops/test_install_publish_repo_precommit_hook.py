"""Characterization + regression tests for
coordinator_core.ops.install_publish_repo_precommit_hook.

D2 fix (2026-07-28, break-class): the generated hook's `command -v bash ||
exit 0` (top-of-script) and `if ! command -v node; then exit 0; fi` guards
used to silently no-op the WHOLE hook (both gates) whenever bash/node were
unresolvable on PATH. This file behaviorally exercises the emitted hook via
`sh` with a narrowed PATH to prove each interpreter-missing case now fails
LOUD (named BLOCKED banner, non-zero exit) and is bypassable only via its own
named COORDINATOR_OVERRIDE_PRECOMMIT_{BASH,NODE}_MISSING escape hatch — never
a silent skip. See install_publish_repo_precommit_hook.py's own module
docstring (2026-07-28 D2 fix section) for the full defect writeup.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.install_publish_repo_precommit_hook as _mod
from coordinator_core.ops.install_publish_repo_precommit_hook import main


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _make_oss_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "oss-repo"
    repo.mkdir(parents=True)
    _git_init(repo)
    return repo


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-commit"


def _write_passing_exec_bit_test(repo: Path) -> None:
    test_dir = repo / "coordinator" / "tests" / "plugin-ecosystem"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "exec-bit.test.js").write_text(
        "const { test } = require('node:test');\n"
        "test('always passes', () => {});\n",
        encoding="utf-8",
    )


def _write_passing_illegal_paths_script(repo: Path) -> None:
    bin_dir = repo / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "check-no-illegal-paths.sh"
    script.write_text("#!/bin/sh\necho 'RAN:check-no-illegal-paths.sh'\nexit 0\n", encoding="utf-8")


def _run_hook(
    hook: Path, cwd: Path, path_env: str | None = None, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path_env is not None:
        env["PATH"] = path_env
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["/bin/sh", str(hook)], cwd=str(cwd), capture_output=True, text=True, env=env
    )


def _run_main(repo: Path) -> int:
    """main() resolves the acting repo from os.getcwd(), not from argv[0]
    (argv[0] is EXPECTED_REPO_ROOT, the identity target) — every caller here
    must chdir into the target repo first."""
    prior_cwd = os.getcwd()
    os.chdir(repo)
    try:
        return main([str(repo)])
    finally:
        os.chdir(prior_cwd)


# ---------------------------------------------------------------------------
# Fresh install — both gates present and runnable
# ---------------------------------------------------------------------------

def test_fresh_install_both_gates_run_when_interpreters_present(tmp_path):
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    _write_passing_illegal_paths_script(repo)

    assert _run_main(repo) == 0
    hook = _hook_path(repo)
    assert hook.is_file()

    result = _run_hook(hook, repo)
    assert result.returncode == 0
    assert "RAN:check-no-illegal-paths.sh" in result.stdout


# ---------------------------------------------------------------------------
# D3 — illegal-path gate no longer silently skips a present-but-non-
# executable helper (the `[ -x ]` -> `[ -f ]` fix), and fails loudly on the
# one remaining genuinely-anomalous case: something present at that path
# that isn't a runnable regular file at all.
# ---------------------------------------------------------------------------

def test_illegal_paths_gate_runs_helper_present_but_not_executable(tmp_path):
    """This is the exact defect D3 closes: before the fix, a helper that
    existed but lacked the exec bit made `[ -x ]` fail and the gate vanished
    with zero output -- indistinguishable from the gate running and finding
    nothing. Gating on `-f` instead means a present, non-executable helper
    now actually runs (its own "RAN:" output appears) rather than silently
    exiting 0."""
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    bin_dir = repo / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "check-no-illegal-paths.sh"
    script.write_text("#!/bin/sh\necho 'RAN:check-no-illegal-paths.sh'\nexit 0\n", encoding="utf-8")
    # Deliberately NOT chmod +x -- this is the case that used to vanish silently.

    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    result = _run_hook(hook, repo)
    assert result.returncode == 0
    assert "RAN:check-no-illegal-paths.sh" in result.stdout


def test_illegal_paths_gate_blocks_loudly_when_helper_is_unrunnable(tmp_path):
    """A helper PATH that exists but is not a runnable regular file (here: a
    directory landed where the script is expected) has no defensible silent
    path -- distinct from the genuinely-absent case, which stays a silent
    self-healing skip."""
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    bin_dir = repo / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "check-no-illegal-paths.sh").mkdir()  # present, but not a regular file

    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    result = _run_hook(hook, repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "not a runnable file" in result.stderr


def test_illegal_paths_gate_unrunnable_override_bypasses_the_block(tmp_path):
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    bin_dir = repo / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "check-no-illegal-paths.sh").mkdir()

    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    result = _run_hook(
        hook, repo, extra_env={"COORDINATOR_OVERRIDE_PRECOMMIT_ILLEGAL_PATHS": "1"}
    )
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr
    assert "not a runnable file" in result.stderr


def test_illegal_paths_gate_non_blocking_but_observable_when_helper_genuinely_absent(tmp_path):
    """Deliberately unchanged by D3: a publish repo may legitimately not
    carry the helper at all, so a genuinely absent helper stays NON-BLOCKING
    -- see the module docstring's negative-spec block. Review: code-reviewer
    F4 (2026-07-28) -- the skip is no longer fully silent: it now prints a
    low-noise informational stderr line so an operator can distinguish "by
    design" from "install regression", without changing the non-blocking
    outcome."""
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    # No coordinator/bin/check-no-illegal-paths.sh at all.

    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    result = _run_hook(hook, repo)
    assert result.returncode == 0
    assert "BLOCKED" not in result.stderr
    assert "RAN:check-no-illegal-paths.sh" not in result.stdout
    assert "illegal-path gate skipped -- no helper vendored" in result.stderr
    assert "by design, not a regression" in result.stderr


def test_fresh_install_is_idempotent(tmp_path, capsys):
    repo = _make_oss_repo(tmp_path)
    assert _run_main(repo) == 0
    assert _run_main(repo) == 0
    captured = capsys.readouterr()
    assert "already installed" in captured.err


# ---------------------------------------------------------------------------
# D2 — node-missing (exec-bit gate) fails loudly, never silently
# ---------------------------------------------------------------------------

def test_missing_node_interpreter_blocks_loudly(tmp_path):
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    _write_passing_illegal_paths_script(repo)
    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    # PATH with no node (but bash/git/sh builtins are still reachable via
    # their existing absolute-path invocations / shell builtins).
    result = _run_hook(hook, repo, path_env=_path_without_node(tmp_path))
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "no node interpreter found" in result.stderr
    assert _mod._FRESH_HOOK_TEMPLATE  # sanity: module import ok


def test_missing_node_interpreter_override_bypasses_the_block(tmp_path):
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    _write_passing_illegal_paths_script(repo)
    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    result = _run_hook(
        hook,
        repo,
        path_env=_path_without_node(tmp_path),
        extra_env={"COORDINATOR_OVERRIDE_PRECOMMIT_NODE_MISSING": "1"},
    )
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr
    assert "no node interpreter found" in result.stderr
    # Gate 2 (illegal-path, bash-based) still ran despite node's absence.
    assert "RAN:check-no-illegal-paths.sh" in result.stdout


# ---------------------------------------------------------------------------
# D2 — bash-missing (illegal-path gate) fails loudly, never silently
# ---------------------------------------------------------------------------

def test_missing_bash_interpreter_blocks_loudly(tmp_path):
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    _write_passing_illegal_paths_script(repo)
    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    # PATH with node reachable but no bash.
    result = _run_hook(hook, repo, path_env=_path_without_bash(tmp_path))
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "no bash interpreter found" in result.stderr
    # Gate 1 (exec-bit, node-based) ran fine ahead of gate 2's block.
    assert "exec-bit drift detected" not in result.stderr


def test_missing_bash_interpreter_override_bypasses_the_block(tmp_path):
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    _write_passing_illegal_paths_script(repo)
    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    result = _run_hook(
        hook,
        repo,
        path_env=_path_without_bash(tmp_path),
        extra_env={"COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING": "1"},
    )
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr
    assert "no bash interpreter found" in result.stderr
    assert "RAN:check-no-illegal-paths.sh" not in result.stdout


# ---------------------------------------------------------------------------
# Review: code-reviewer F3 (2026-07-28) — parity with
# test_install_meta_repo_precommit_hook.py's
# test_wrapper_override_never_bypasses_a_real_gate_finding: the
# COORDINATOR_OVERRIDE_PRECOMMIT_{NODE,BASH}_MISSING escape hatches only
# ever excuse "the gate could not run" (interpreter absent) -- they must
# NEVER suppress a real nonzero exit from a gate that DID run. Neither
# override installer test file pinned this for the publish-repo shim before
# this fix; this is the load-bearing invariant of the whole D2/D3 change.
# ---------------------------------------------------------------------------

def test_node_missing_override_never_bypasses_a_real_exec_bit_failure(tmp_path):
    """NODE_MISSING override must be irrelevant when node IS present and the
    exec-bit gate genuinely fails -- the override only excuses a gate that
    could not run, never a real finding from one that did."""
    repo = _make_oss_repo(tmp_path)
    test_dir = repo / "coordinator" / "tests" / "plugin-ecosystem"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "exec-bit.test.js").write_text(
        "const { test } = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('deliberately fails', () => { assert.fail('exec-bit drift'); });\n",
        encoding="utf-8",
    )
    _write_passing_illegal_paths_script(repo)
    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    # node IS on PATH (unnarrowed) and the gate genuinely fails -- the
    # override must not suppress that real failure.
    result = _run_hook(
        hook, repo, extra_env={"COORDINATOR_OVERRIDE_PRECOMMIT_NODE_MISSING": "1"}
    )
    assert result.returncode == 1
    assert "exec-bit drift detected" in result.stderr
    assert "SKIPPED" not in result.stderr


def test_bash_missing_override_never_bypasses_a_real_illegal_paths_failure(tmp_path):
    """BASH_MISSING override must be irrelevant when bash IS present and the
    illegal-path gate genuinely fails -- same invariant as the node case
    above, for the bash-based gate."""
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)
    bin_dir = repo / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "check-no-illegal-paths.sh"
    script.write_text(
        "#!/bin/sh\necho 'illegal path found: foo?.txt' >&2\nexit 1\n", encoding="utf-8"
    )

    assert _run_main(repo) == 0
    hook = _hook_path(repo)

    # bash IS on PATH (unnarrowed) and the gate genuinely fails -- the
    # override must not suppress that real failure.
    result = _run_hook(
        hook, repo, extra_env={"COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING": "1"}
    )
    assert result.returncode == 1
    assert "illegal path found" in result.stderr
    assert "SKIPPED" not in result.stderr


# ---------------------------------------------------------------------------
# Append path — bash-missing case on the illegal-path append template too
# ---------------------------------------------------------------------------

def test_append_path_bash_missing_blocks_loudly(tmp_path):
    """Simulates an exec-bit-only hook from before D4 (illegal-path gate
    added) — re-running main() appends the illegal-path gate, and a
    bash-less box hitting the appended block fails loudly, not silently."""
    repo = _make_oss_repo(tmp_path)
    _write_passing_exec_bit_test(repo)

    canonical = _mod._canon(str(repo))
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    # Synthetic pre-D4 hook: only the exec-bit marker/identity guard, no
    # illegal-path gate at all — this is what main()'s "upgrade" branch
    # (_GATE_MARKER present, _PATHS_MARKER absent) expects to find.
    synthetic_hook = f"""#!/bin/sh
# {_mod._GATE_MARKER}
OSS_REPO_ROOT="{canonical}"
_canon() {{
  [ -n "$1" ] || {{ echo ""; return; }}
  (cd "$1" 2>/dev/null && pwd -P) || {{ echo ""; }}
}}
_cur="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$_cur" ] || exit 0
if [ "$(_canon "$_cur")" != "$(_canon "$OSS_REPO_ROOT")" ]; then
  exit 0
fi
"""
    hook.write_text(synthetic_hook, encoding="utf-8")
    assert _mod._GATE_MARKER in hook.read_text(encoding="utf-8")
    assert _mod._PATHS_MARKER not in hook.read_text(encoding="utf-8")

    assert _run_main(repo) == 0
    content = hook.read_text(encoding="utf-8")
    assert _mod._PATHS_MARKER in content
    assert "COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING" in content

    _write_passing_illegal_paths_script(repo)
    result = _run_hook(hook, repo, path_env=_path_without_bash(tmp_path))
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "no bash interpreter found" in result.stderr


# ---------------------------------------------------------------------------
# helpers to locate real interpreters for narrowed-PATH tests
# ---------------------------------------------------------------------------

def _bash_path() -> str:
    # `command` is a shell builtin; invoke via sh -c to resolve it.
    result = subprocess.run(["/bin/sh", "-c", "command -v bash"], capture_output=True, text=True)
    path = result.stdout.strip()
    if not path:
        pytest.skip("bash not found on PATH in this environment")
    return path


def _node_path() -> str:
    result = subprocess.run(["/bin/sh", "-c", "command -v node"], capture_output=True, text=True)
    path = result.stdout.strip()
    if not path:
        pytest.skip("node not found on PATH in this environment")
    return path


def _git_path() -> str:
    result = subprocess.run(["/bin/sh", "-c", "command -v git"], capture_output=True, text=True)
    path = result.stdout.strip()
    if not path:
        pytest.skip("git not found on PATH in this environment")
    return path


def _make_tool_bindir(tmp_path: Path, tools: dict) -> str:
    """Build a directory containing ONLY symlinks to `tools` (name -> real
    absolute path), so a PATH pointed at it exposes exactly those binaries.

    Picking real tools' *directories* is not safe here: on this box
    `/opt/homebrew/bin` holds both `git` and `bash`, so reusing git's parent
    directory to "keep git reachable while excluding bash" silently smuggles
    bash back in. A dedicated symlink dir is the only way to control
    resolvable-vs-not per binary independent of which real directory each
    one happens to live in.
    """
    bindir = tmp_path / "tool-bindir"
    bindir.mkdir(exist_ok=True)
    for name, real_path in tools.items():
        link = bindir / name
        if not link.exists():
            link.symlink_to(real_path)
    return str(bindir)


def _path_without_node(tmp_path: Path) -> str:
    """A PATH with bash and git reachable (the hook needs git for its
    identity check regardless of which interpreter-missing case is under
    test) but no node binary resolvable on it."""
    return _make_tool_bindir(
        tmp_path, {"bash": _bash_path(), "git": _git_path(), "sh": "/bin/sh"}
    )


def _path_without_bash(tmp_path: Path) -> str:
    """A PATH with node and git reachable but no bash binary resolvable on it."""
    return _make_tool_bindir(
        tmp_path, {"node": _node_path(), "git": _git_path(), "sh": "/bin/sh"}
    )
