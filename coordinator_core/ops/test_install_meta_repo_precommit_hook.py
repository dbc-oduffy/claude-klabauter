"""Characterization + regression tests for
coordinator_core.ops.install_meta_repo_precommit_hook.

See that module's own docstring (2026-07-28 REWRITE section) for the two
generations of silent-skip defect this test file guards against: a hardcoded
bin-dir literal that stopped existing (every gate vanished, hook still exited
0), and a WARN-and-continue draft that replaced it (same fail-open shape,
different clothes). The tests below are deliberately BEHAVIORAL where
possible — they execute the emitted hook via `sh` with stub gate scripts,
rather than only grepping the hook text for marker substrings, because
substring presence is exactly what the previous "gates are present but dead
after a stray `exit 0`" bug would still pass.

Port of: install-meta-repo-precommit-hook.sh (DoE b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core.ops.install_meta_repo_precommit_hook import (
    _GATE_REGISTRY,
    _bin_dir,
    main,
)
import coordinator_core.ops.install_meta_repo_precommit_hook as _mod

# Declared, not excused: this file spawns real `git` and `sh` processes
# because the tests are deliberately BEHAVIORAL -- they execute the emitted
# pre-commit hook via a real shell against a real `$HOME/.claude` git repo,
# per the module docstring's own rationale (the two prior fail-open defects
# both silently passed a substring-only grep test; only executing the
# actual hook text catches "gates present but dead after a stray `exit 0`").
# No mock stands in for that. Each test builds its own fake HOME/meta repo
# via `_make_meta_repo`, so it is not hoisted to module scope -- per-test
# isolation (HOME env + hook execution side effects would collide). The
# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _make_meta_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake $HOME with a $HOME/.claude git repo and point HOME there."""
    fake_home = tmp_path / "fakehome"
    meta = fake_home / ".claude"
    meta.mkdir(parents=True)
    _git_init(meta)
    monkeypatch.setenv("HOME", str(fake_home))
    # CLAUDE_HOME outranks HOME in meta_repo_identity's precedence, and the
    # suite-root home quarantine does not clear it — leaving a real one set
    # would point the op at the developer's live meta-repo.
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    return meta


def _hook_path(meta_repo: Path) -> Path:
    return meta_repo / ".git" / "hooks" / "pre-commit"


def _write_stub_gates(fake_bin: Path, exit_map: dict | None = None) -> None:
    """Write a stub Python script for every registered gate. Each stub prints
    `RAN:<filename>` and exits 0, unless overridden via `exit_map`, so a hook
    execution can be checked for BOTH reachability (did stdout mention it)
    and ordering (in what sequence) — not just textual presence in the hook.
    """
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
        ["/bin/sh", str(hook)], cwd=str(cwd), capture_output=True, text=True, env=env
    )


# ---------------------------------------------------------------------------
# Negative corpus
# ---------------------------------------------------------------------------

def test_target_not_a_git_repo(tmp_path, capsys):
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    rc = main([str(notgit)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not in a git repo — NO gate installed." in err


def test_target_is_git_repo_but_not_meta_repo(tmp_path, monkeypatch, capsys):
    somerepo = tmp_path / "somerepo"
    somerepo.mkdir()
    _git_init(somerepo)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    rc = main([str(somerepo)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not the meta-repo" in err
    assert not (somerepo / ".git" / "hooks" / "pre-commit").exists()


def test_default_target_is_the_meta_repo_not_cwd(tmp_path, monkeypatch, capsys):
    """Regression (2026-07-30): a bare invocation resolves the meta-repo from
    the environment, NOT cwd.

    The previous default was `"."`, so running from any working repo — which is
    where a session actually runs — skipped with exit 0 and installed nothing
    while reading as success to `/coordinator:install` and `/repo-setup`. This
    test stands in the failing position: cwd is a DIFFERENT git repo than the
    meta-repo, and the meta-repo must still get the gate.
    """
    meta = _make_meta_repo(tmp_path, monkeypatch)
    cwdrepo = tmp_path / "cwdrepo"
    cwdrepo.mkdir()
    _git_init(cwdrepo)
    monkeypatch.chdir(cwdrepo)

    rc = main([])

    assert rc == 0
    assert _hook_path(meta).exists(), "bare invocation installed nothing into the meta-repo"
    assert not (cwdrepo / ".git" / "hooks" / "pre-commit").exists(), "installed into cwd's repo"


def test_explicit_non_meta_target_names_no_gate_installed(tmp_path, monkeypatch, capsys):
    """The identity guard survives for an EXPLICIT target, and its banner says
    plainly that nothing was installed — the skip-reads-as-success half of the
    same defect. Exit stays 0 by the op's pinned no-op contract (see
    `coordinator/bin/install-meta-repo-precommit-hook.py` § Exit convention)."""
    _make_meta_repo(tmp_path, monkeypatch)
    somerepo = tmp_path / "someotherrepo"
    somerepo.mkdir()
    _git_init(somerepo)

    rc = main([str(somerepo)])

    assert rc == 0
    err = capsys.readouterr().err
    assert "is not the meta-repo" in err
    assert "NO gate installed" in err


# ---------------------------------------------------------------------------
# Bin-dir resolution — self-relative, no hardcoded literal
# ---------------------------------------------------------------------------

def test_bin_dir_is_module_sibling_not_a_home_literal():
    resolved = _bin_dir()
    assert resolved.name == "bin"
    assert resolved.parent.name == "coordinator"
    # The resolved value must come from this module's own location, not $HOME.
    assert str(resolved).startswith(str(Path(_mod.__file__).resolve().parents[2]))


def test_bin_dir_tracks_module_relocation(tmp_path, monkeypatch):
    """Behavioral proof of self-relative resolution: move the "module" to a
    different fake repo root and confirm the computed bin dir moves with it,
    rather than being pinned to any fixed string."""
    fake_file = tmp_path / "otherrepo" / "coordinator_core" / "ops" / "install_meta_repo_precommit_hook.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "__file__", str(fake_file))
    resolved = _mod._bin_dir()
    assert resolved == (tmp_path / "otherrepo" / "coordinator" / "bin")


def test_no_hardcoded_absolute_literal_outside_the_docstring():
    """The module docstring is allowed to MENTION the historical broken
    literal in prose (it explains the defect this file replaces). The actual
    code must not contain it — this is a regression test for defect #1
    described in that docstring, not a ban on discussing it."""
    source = Path(_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    doc_node = tree.body[0]
    assert isinstance(doc_node, ast.Expr) and isinstance(doc_node.value, ast.Constant)
    # Drop the docstring by LINE RANGE (ast.get_docstring() normalizes
    # whitespace, so a string-replace against the raw source can silently
    # fail to match and leave the docstring's prose in `code_only` uncut —
    # slicing by lineno/end_lineno is exact regardless of formatting).
    lines = source.splitlines(keepends=True)
    code_only = "".join(lines[: doc_node.lineno - 1] + lines[doc_node.end_lineno :])
    assert "plugins/coordinator-claude" not in code_only
    assert "X:/" not in code_only
    assert "X:\\" not in code_only


# ---------------------------------------------------------------------------
# Fresh install — all four gates present, reachable, in registry order
# ---------------------------------------------------------------------------

def test_fresh_install_writes_all_four_gates_and_sets_exec_bit(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    rc = main([str(meta)])
    assert rc == 0
    hook = _hook_path(meta)
    assert hook.is_file()
    content = hook.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh\n")
    for gate in _GATE_REGISTRY:
        assert gate.marker in content
    assert content.rstrip("\n").endswith("exit 0")
    # POSIX-only: git execs this `#!/bin/sh` hook via its own bundled `sh`
    # on every platform, so os.access(X_OK)'s Windows degrade-to-F_OK is a
    # genuine no-op there, not a defect.
    if os.name != "nt":
        assert os.access(hook, os.X_OK)
    assert "installed" in capsys.readouterr().err


def test_fresh_install_all_four_gates_actually_execute_in_order(tmp_path, monkeypatch):
    """Textual presence is not enough (that's exactly what the dead-code-
    after-exit-0 bug would still pass) — run the emitted hook and check
    every gate actually ran, in registry order."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    assert main([str(meta)]) == 0
    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 0
    ran_order = [line.split(":", 1)[1] for line in result.stdout.splitlines() if line.startswith("RAN:")]
    assert ran_order == [g.filename for g in _GATE_REGISTRY]


def test_idempotent_when_all_four_markers_present(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    main([str(meta)])
    before = _hook_path(meta).read_text(encoding="utf-8")
    rc = main([str(meta)])
    assert rc == 0
    after = _hook_path(meta).read_text(encoding="utf-8")
    assert before == after
    assert "already installed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fail-loud — missing script / missing interpreter BLOCK, never a silent skip
# ---------------------------------------------------------------------------

def test_missing_gate_script_blocks_loudly_and_stops_the_chain(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main([str(meta)]) == 0

    # Gate 2 (illegal-path) is second in registry order; delete its script so
    # gates 3-4 (which would otherwise run after it) must NOT execute either.
    missing_gate = _GATE_REGISTRY[1]
    (fake_bin / missing_gate.filename).unlink()

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert missing_gate.label in result.stderr
    assert missing_gate.marker in result.stderr
    ran = [line for line in result.stdout.splitlines() if line.startswith("RAN:")]
    # Only the gate(s) before the missing one ran; nothing after it did.
    assert ran == [f"RAN:{_GATE_REGISTRY[0].filename}"]


def test_missing_python_interpreter_blocks_loudly(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main([str(meta)]) == 0

    # A PATH with no python3/python/py on it at all (still has /bin, /usr/bin
    # for sh/command/echo themselves — command -v is a shell builtin so it
    # doesn't need an external binary, but there must be no python to find).
    result = _run_hook(_hook_path(meta), meta, path_env="/nonexistent-empty-path-dir")
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "no python interpreter found" in result.stderr


# ---------------------------------------------------------------------------
# Escape hatch — per-gate override env var bypasses a CANNOT-RUN block
# ---------------------------------------------------------------------------

def test_missing_gate_script_blocked_message_names_its_override():
    """The gate's own shell `[ "$VAR" = "1" ]` test still reads the exact
    override spelling (load-bearing for the bypass mechanism itself), but
    the BLOCKED banner's rendered remediation text no longer names the key
    inline — it points at the override-key doc instead (B6/B8, see
    docs/wiki/guard-messaging.md § Register)."""
    for gate in _GATE_REGISTRY:
        block = "\n".join(_mod._gate_block(gate, Path("/fake/bin")))
        assert gate.override_env in block
        assert f"{gate.override_env}=1" not in block
        assert _mod.OVERRIDE_KEYS_DOC_DISPLAY in block


def test_missing_gate_script_override_bypasses_the_block(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main([str(meta)]) == 0

    missing_gate = _GATE_REGISTRY[1]
    (fake_bin / missing_gate.filename).unlink()

    # Without the override: BLOCKED (already covered by the sibling test
    # above, re-asserted here as the control for comparison).
    blocked = _run_hook(_hook_path(meta), meta)
    assert blocked.returncode == 1

    # With the override set: the missing gate is SKIPPED (not silently, and
    # not run), and every OTHER gate still executes normally.
    overridden = _run_hook(
        _hook_path(meta), meta, extra_env={missing_gate.override_env: "1"}
    )
    assert overridden.returncode == 0
    assert "SKIPPED" in overridden.stderr
    assert missing_gate.marker in overridden.stderr
    ran = [line.split(":", 1)[1] for line in overridden.stdout.splitlines() if line.startswith("RAN:")]
    assert ran == [g.filename for g in _GATE_REGISTRY if g.filename != missing_gate.filename]


def test_missing_python_interpreter_override_bypasses_the_block(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main([str(meta)]) == 0

    # No python on PATH at all -> every gate's interpreter check fires; set
    # every gate's override so all of them SKIP cleanly instead of blocking.
    overrides = {g.override_env: "1" for g in _GATE_REGISTRY}
    result = _run_hook(
        _hook_path(meta), meta, path_env="/nonexistent-empty-path-dir", extra_env=overrides
    )
    assert result.returncode == 0
    assert result.stdout.count("RAN:") == 0
    assert result.stderr.count("SKIPPED") == len(_GATE_REGISTRY)


def test_wrapper_override_never_bypasses_a_real_gate_finding(tmp_path, monkeypatch):
    """The wrapper-level override only ever bypasses "the gate could not
    run" — it must NOT suppress a real nonzero exit from a gate script that
    DID run successfully (that would be a policy bypass, not an escape
    hatch for a broken install)."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    failing_gate = _GATE_REGISTRY[2]
    _write_stub_gates(fake_bin, exit_map={failing_gate.filename: 1})
    assert main([str(meta)]) == 0

    # The script exists and python is present -- the override should be
    # irrelevant here, and the gate's own (real) failure must still block.
    result = _run_hook(
        _hook_path(meta), meta, extra_env={failing_gate.override_env: "1"}
    )
    assert result.returncode == 1
    assert "SKIPPED" not in result.stderr


# ---------------------------------------------------------------------------
# Bash-presence carve-out — distinct from a missing-script BLOCKED failure
# ---------------------------------------------------------------------------

def test_bash_carveout_is_a_single_shared_guard_distinct_from_blocked(tmp_path, monkeypatch):
    """A bash-kind gate's absence-of-bash case must be ONE shared guard
    ahead of the whole bash-kind group, and must read differently on stderr
    than an individual gate's own BLOCKED banner -- collapsing the two into
    one undifferentiated block (as the collision draft this rewrite replaced
    did) loses exactly the distinction that matters: which gate failed and
    why.

    D2 fix (2026-07-28): "no bash on this box" is no longer a SILENT skip
    (the pre-D2 `command -v bash || exit 0` shape) -- it is now a LOUD
    CANNOT-RUN case, fails the commit by default, and is bypassable only via
    the named COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING=1 escape hatch,
    mirroring the per-gate override_env pattern the python gates already use.
    """
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)  # the 4 real python gates must pass cleanly

    bash_gate = _mod._Gate(
        marker="fake-bash-gate",
        filename="fake-bash-gate.sh",
        kind="bash",
        label="fake bash gate",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_FAKE_BASH_GATE",
    )
    fake_bin.mkdir(parents=True, exist_ok=True)
    (fake_bin / bash_gate.filename).write_text(
        "#!/bin/sh\necho 'RAN:fake-bash-gate.sh'\nexit 0\n", encoding="utf-8"
    )
    os.chmod(fake_bin / bash_gate.filename, 0o755)

    registry = list(_GATE_REGISTRY) + [bash_gate]
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", registry)

    content = _mod._hook_body(fake_bin, registry)
    guard_line = "if ! command -v bash >/dev/null 2>&1; then"
    assert guard_line in content
    # Exactly one shared guard, appearing ONCE, ahead of the bash-kind
    # gate's own block -- not duplicated per gate, not merged into it.
    assert content.count(guard_line) == 1
    guard_idx = content.index(guard_line)
    gate_idx = content.index(f"# --- Gate: {bash_gate.label}")
    assert guard_idx < gate_idx
    assert _mod._BASH_MISSING_OVERRIDE_ENV in content

    # Behaviorally: on a box WITH bash, the bash-kind gate still runs and
    # still gets its own BLOCKED treatment if ITS script goes missing --
    # that failure mode must read as BLOCKED, never merge with the group
    # guard's own CANNOT-RUN case.
    hook = tmp_path / "standalone-hook"
    hook.write_text(content, encoding="utf-8")
    os.chmod(hook, 0o755)
    (fake_bin / bash_gate.filename).unlink()
    result = _run_hook(hook, tmp_path)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert bash_gate.marker in result.stderr


def _bash_only_hook(tmp_path) -> tuple[Path, "_mod._Gate"]:
    """Build a standalone hook body containing ONLY one bash-kind gate (no
    python gates), so bash-presence tests don't also need to resolve a real
    python interpreter off a deliberately-narrowed PATH."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    bash_gate = _mod._Gate(
        marker="fake-bash-gate",
        filename="fake-bash-gate.sh",
        kind="bash",
        label="fake bash gate",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_FAKE_BASH_GATE",
    )
    (fake_bin / bash_gate.filename).write_text(
        "#!/bin/sh\necho 'RAN:fake-bash-gate.sh'\nexit 0\n", encoding="utf-8"
    )
    os.chmod(fake_bin / bash_gate.filename, 0o755)

    content = _mod._hook_body(fake_bin, [bash_gate])
    hook = tmp_path / "standalone-hook"
    hook.write_text(content, encoding="utf-8")
    os.chmod(hook, 0o755)
    return hook, bash_gate


def test_missing_bash_interpreter_blocks_loudly(tmp_path):
    """D2 (2026-07-28): a bash-kind gate group on a bash-less box (MinGit)
    fails LOUD -- named banner, non-zero exit -- rather than the pre-D2
    silent `exit 0` that made the whole hook a no-op with zero output.
    """
    hook, bash_gate = _bash_only_hook(tmp_path)

    # A PATH with no bash on it at all -- /bin/sh (the hook interpreter
    # itself) is invoked by absolute path so it doesn't need PATH, but
    # `command -v bash` inside the hook body will fail to resolve anything.
    result = _run_hook(hook, tmp_path, path_env="/nonexistent-empty-path-dir")
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "no bash interpreter found" in result.stderr
    assert "RAN:fake-bash-gate.sh" not in result.stdout


def test_missing_bash_interpreter_override_bypasses_the_block(tmp_path):
    """D2 escape hatch: COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING=1 SKIPs
    the bash-kind group cleanly instead of blocking."""
    hook, bash_gate = _bash_only_hook(tmp_path)

    result = _run_hook(
        hook,
        tmp_path,
        path_env="/nonexistent-empty-path-dir",
        extra_env={_mod._BASH_MISSING_OVERRIDE_ENV: "1"},
    )
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr
    assert "RAN:fake-bash-gate.sh" not in result.stdout


# ---------------------------------------------------------------------------
# Append paths — partial coordinator hook, and a human's custom hook
# ---------------------------------------------------------------------------

def test_append_missing_gates_to_partial_coordinator_hook_and_they_run(tmp_path, monkeypatch, capsys):
    """Simulates a pre-gate-3/4 install (only gates 1-2 markers present) —
    re-running main() appends the missing gates AND they must actually
    execute, not just appear in the text."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    g1, g2 = _GATE_REGISTRY[0], _GATE_REGISTRY[1]
    hook.write_text(
        "#!/bin/sh\n"
        f'_py="$(command -v python3 || command -v python || command -v py)"\n'
        f'_gate_script="{(fake_bin / g1.filename).as_posix()}"\n'
        f'"$_py" "$_gate_script" || exit $?\n'
        f'_gate_script="{(fake_bin / g2.filename).as_posix()}"\n'
        f'"$_py" "$_gate_script" || exit $?\n'
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(hook, 0o755)

    rc = main([str(meta)])
    assert rc == 0
    content = hook.read_text(encoding="utf-8")
    for gate in _GATE_REGISTRY:
        assert gate.marker in content
    err = capsys.readouterr().err
    assert "appended gate(s)" in err
    assert g1.marker not in err.split("appended gate(s)")[1]  # only the missing two named

    result = _run_hook(hook, meta)
    assert result.returncode == 0
    ran_order = [line.split(":", 1)[1] for line in result.stdout.splitlines() if line.startswith("RAN:")]
    assert ran_order == [g.filename for g in _GATE_REGISTRY]


def test_appends_all_gates_to_existing_custom_hook_and_strips_trailing_exit0(tmp_path, monkeypatch, capsys):
    """A custom hook ending in a bare `exit 0` must not swallow the appended
    gates as dead code — this is the regression test for the
    concatenate-onto-the-end bug named in the module docstring."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin, exit_map={_GATE_REGISTRY[-1].filename: 1})

    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\necho "custom hook"\nexit 0\n', encoding="utf-8")
    os.chmod(hook, 0o755)

    rc = main([str(meta)])
    assert rc == 0
    content = hook.read_text(encoding="utf-8")
    assert 'echo "custom hook"' in content
    for gate in _GATE_REGISTRY:
        assert gate.marker in content
    # POSIX-only: see the earlier hook-executable assertion in this file.
    if os.name != "nt":
        assert os.access(hook, os.X_OK)

    # The last stub gate exits 1 -- if it were dead code after a stray
    # `exit 0`, this run would return 0 instead.
    result = _run_hook(hook, meta)
    assert result.returncode == 1
    ran_order = [line.split(":", 1)[1] for line in result.stdout.splitlines() if line.startswith("RAN:")]
    assert ran_order == [g.filename for g in _GATE_REGISTRY]
    assert "custom hook" in result.stdout


def test_append_path_is_idempotent_and_does_not_duplicate_blocks(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\necho "custom hook"\nexit 0\n', encoding="utf-8")
    os.chmod(hook, 0o755)

    def _block_count(text: str, gate) -> int:
        # A gate's marker legitimately appears up to 4x within its OWN block
        # (the "# --- Gate:" comment, the script path, and both BLOCKED-
        # banner echo lines) — count BLOCKS (the comment line), not raw
        # marker substring occurrences, to detect actual duplication.
        return text.count(f"# --- Gate: {gate.label} ({gate.marker}) ---")

    assert main([str(meta)]) == 0
    first_pass = hook.read_text(encoding="utf-8")
    for gate in _GATE_REGISTRY:
        assert _block_count(first_pass, gate) == 1

    rc = main([str(meta)])
    assert rc == 0
    second_pass = hook.read_text(encoding="utf-8")
    assert first_pass == second_pass
    assert "already installed" in capsys.readouterr().err
    for gate in _GATE_REGISTRY:
        assert _block_count(second_pass, gate) == 1


# ---------------------------------------------------------------------------
# Versioned regions — upgrade awareness (marker presence alone is no longer
# "installed and current"; see module docstring's "Versioned regions"
# section). The four scenarios below are the ones the dispatch brief named
# explicitly: current -> no-op unchanged bytes, stale -> replaced in place,
# stale-plus-human-content -> human content untouched, absent -> appended.
# ---------------------------------------------------------------------------

def _one_gate_registry(fake_bin: Path, version: int) -> List["_mod._Gate"]:
    gate = _mod._Gate(
        marker="fake-versioned-gate",
        filename="fake-versioned-gate.py",
        kind="python",
        label="fake versioned gate",
        override_env="COORDINATOR_OVERRIDE_FAKE_VERSIONED_GATE",
        version=version,
    )
    fake_bin.mkdir(parents=True, exist_ok=True)
    script = fake_bin / gate.filename
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('RAN:fake-versioned-gate.py')\nsys.exit(0)\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    return [gate]


def _two_gate_registry(fake_bin: Path, version_a: int, version_b: int) -> List["_mod._Gate"]:
    """Two independently-versioned gates, distinct markers -- for exercising
    `_replace_stale_gate_regions`'s multi-region splice path (descending-
    offset application), which every single-gate test above cannot reach."""
    gate_a = _mod._Gate(
        marker="fake-gate-alpha",
        filename="fake-gate-alpha.py",
        kind="python",
        label="fake gate alpha",
        override_env="COORDINATOR_OVERRIDE_FAKE_GATE_ALPHA",
        version=version_a,
    )
    gate_b = _mod._Gate(
        marker="fake-gate-beta",
        filename="fake-gate-beta.py",
        kind="python",
        label="fake gate beta",
        override_env="COORDINATOR_OVERRIDE_FAKE_GATE_BETA",
        version=version_b,
    )
    fake_bin.mkdir(parents=True, exist_ok=True)
    for gate in (gate_a, gate_b):
        script = fake_bin / gate.filename
        script.write_text(
            f"#!/usr/bin/env python3\nimport sys\nprint('RAN:{gate.filename}')\nsys.exit(0)\n",
            encoding="utf-8",
        )
        os.chmod(script, 0o755)
    return [gate_a, gate_b]


def test_multi_gate_splice_replaces_only_the_stale_region(tmp_path, monkeypatch, capsys):
    """`_replace_stale_gate_regions` exists specifically for multiple
    simultaneous stale regions, resolved-then-applied in DESCENDING
    start-offset order so an earlier splice never shifts a later region's
    already-computed offsets (see that function's own docstring). Every
    other test in this file uses a one-gate registry, so that path -- and
    its interaction with a genuinely current sibling gate -- was never
    exercised. Here gate A goes stale and gate B does not; assert gate B's
    region is byte-identical afterward, order is preserved, and nothing
    between the two regions is lost."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    v1_registry = _two_gate_registry(fake_bin, version_a=1, version_b=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v1_registry)
    assert main([str(meta)]) == 0
    gate_a, gate_b = v1_registry
    v1_text = _hook_path(meta).read_text(encoding="utf-8")
    start_b, end_b, _ = _mod._find_gate_region(v1_text, gate_b.marker)
    b_block_before = v1_text[start_b:end_b]
    assert v1_text.index(gate_a.marker) < v1_text.index(gate_b.marker)

    # Bump only gate A -- gate B stays current.
    v2_registry = _two_gate_registry(fake_bin, version_a=2, version_b=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v2_registry)
    rc = main([str(meta)])
    assert rc == 0
    after = _hook_path(meta).read_text(encoding="utf-8")

    # Exactly one block per gate -- replaced, not duplicated.
    assert after.count("# --- Gate: fake gate alpha (fake-gate-alpha) ---") == 1
    assert after.count("# --- Gate: fake gate beta (fake-gate-beta) ---") == 1
    # Order preserved.
    assert after.index(gate_a.marker) < after.index(gate_b.marker)

    start_a, end_a, _ = _mod._find_gate_region(after, gate_a.marker)
    assert "# gate-version: 2" in after[start_a:end_a]
    start_b2, end_b2, _ = _mod._find_gate_region(after, gate_b.marker)
    assert after[start_b2:end_b2] == b_block_before, "current gate's region must be byte-identical"

    err = capsys.readouterr().err
    assert "replaced stale gate(s)" in err
    assert gate_a.marker in err

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 0
    assert "RAN:fake-gate-alpha.py" in result.stdout
    assert "RAN:fake-gate-beta.py" in result.stdout


def test_multi_gate_splice_both_stale_replaces_both_and_loses_nothing_between(
    tmp_path, monkeypatch, capsys
):
    """Companion to the mixed-staleness case above: BOTH gates stale in the
    same run. The real `_GATE_REGISTRY` had five entries when a same-day
    commit bumped four of them at once (2026-07-29, before
    `coordinator-precommit-exec-bit-check` was retired out of the registry
    the same day) -- this is that scenario, minimized to two gates."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    v1_registry = _two_gate_registry(fake_bin, version_a=1, version_b=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v1_registry)
    assert main([str(meta)]) == 0
    gate_a, gate_b = v1_registry

    v2_registry = _two_gate_registry(fake_bin, version_a=2, version_b=2)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v2_registry)
    rc = main([str(meta)])
    assert rc == 0
    after = _hook_path(meta).read_text(encoding="utf-8")

    assert after.count("# --- Gate: fake gate alpha (fake-gate-alpha) ---") == 1
    assert after.count("# --- Gate: fake gate beta (fake-gate-beta) ---") == 1
    assert after.index(gate_a.marker) < after.index(gate_b.marker)
    assert "# gate-version: 1" not in after

    start_a, end_a, _ = _mod._find_gate_region(after, gate_a.marker)
    start_b, end_b, _ = _mod._find_gate_region(after, gate_b.marker)
    assert "# gate-version: 2" in after[start_a:end_a]
    assert "# gate-version: 2" in after[start_b:end_b]
    # No overlap and nothing lost between the two regions -- between the end
    # of A's region and the start of B's region there is at most whitespace.
    assert end_a <= start_b
    assert after[end_a:start_b].strip() == ""

    err = capsys.readouterr().err
    assert "replaced stale gate(s)" in err
    assert gate_a.marker in err and gate_b.marker in err

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 0
    assert "RAN:fake-gate-alpha.py" in result.stdout
    assert "RAN:fake-gate-beta.py" in result.stdout


# ---------------------------------------------------------------------------
# Region-boundary hardening (2026-07-29 review finding) -- `_find_gate_region`
# must never guess a boundary it cannot confirm. If a hand-edited hook lost
# the blank-line separator between one gate's block and the next content
# (another gate's header, or trailing human content), the old code walked
# forward to whatever blank line happened to occur later (or to end-of-file)
# and a caller replacing that over-extended region silently deleted
# everything it swallowed. The fix raises loud instead. These tests
# reconstruct that hand-edited shape directly (bypassing main()'s own
# writer, which never emits this shape) and assert the installer either
# raises or leaves the file untouched -- never silently deletes content.
# ---------------------------------------------------------------------------

def test_missing_separator_between_two_gates_raises_rather_than_swallowing_sibling(
    tmp_path, monkeypatch
):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    gate_a = _mod._Gate(
        marker="fake-gate-alpha", filename="fake-gate-alpha.py", kind="python",
        label="fake gate alpha", override_env="COORDINATOR_OVERRIDE_FAKE_GATE_ALPHA", version=1,
    )
    gate_b = _mod._Gate(
        marker="fake-gate-beta", filename="fake-gate-beta.py", kind="python",
        label="fake gate beta", override_env="COORDINATOR_OVERRIDE_FAKE_GATE_BETA", version=1,
    )
    fake_bin.mkdir(parents=True, exist_ok=True)
    for gate in (gate_a, gate_b):
        script = fake_bin / gate.filename
        script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
        os.chmod(script, 0o755)

    block_a = "\n".join(_mod._gate_block(gate_a, fake_bin))
    block_b = "\n".join(_mod._gate_block(gate_b, fake_bin))
    # Hand-spliced: gate A's block runs directly into gate B's header, no
    # separator blank line -- the exact shape a human edit (or a bad merge
    # resolution) could produce.
    hand_edited = (
        "#!/bin/sh\n" + _mod._py_resolve_line() + "\n\n"
        + block_a + "\n" + block_b + "\n\nexit 0\n"
    )
    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(hand_edited, encoding="utf-8")
    os.chmod(hook, 0o755)

    # Gate A goes stale -- the installer must try to locate A's region to
    # replace it, and MUST NOT silently swallow gate B's block while doing so.
    stale_registry = [
        _mod._Gate(
            marker="fake-gate-alpha", filename="fake-gate-alpha.py", kind="python",
            label="fake gate alpha", override_env="COORDINATOR_OVERRIDE_FAKE_GATE_ALPHA", version=2,
        ),
        gate_b,
    ]
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", stale_registry)

    with pytest.raises(RuntimeError, match="blank-line"):
        main([str(meta)])

    # Atomicity: the raise happens before any write -- the hand-edited file
    # on disk must be completely untouched, never partially rewritten.
    assert hook.read_text(encoding="utf-8") == hand_edited


def test_missing_separator_before_trailing_human_content_raises_rather_than_deleting_it(
    tmp_path, monkeypatch
):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    gate = _mod._Gate(
        marker="fake-versioned-gate", filename="fake-versioned-gate.py", kind="python",
        label="fake versioned gate", override_env="COORDINATOR_OVERRIDE_FAKE_VERSIONED_GATE", version=1,
    )
    fake_bin.mkdir(parents=True, exist_ok=True)
    script = fake_bin / gate.filename
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    os.chmod(script, 0o755)

    block = "\n".join(_mod._gate_block(gate, fake_bin))
    human_footer = "echo 'human footer, no blank line before it'\n"
    # No blank line anywhere between the gate header and end-of-file.
    hand_edited = (
        "#!/bin/sh\n" + _mod._py_resolve_line() + "\n\n"
        + block + "\n" + human_footer + "exit 0\n"
    )
    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(hand_edited, encoding="utf-8")
    os.chmod(hook, 0o755)

    stale_registry = [
        _mod._Gate(
            marker="fake-versioned-gate", filename="fake-versioned-gate.py", kind="python",
            label="fake versioned gate", override_env="COORDINATOR_OVERRIDE_FAKE_VERSIONED_GATE",
            version=2,
        )
    ]
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", stale_registry)

    with pytest.raises(RuntimeError, match="blank-line"):
        main([str(meta)])

    assert hook.read_text(encoding="utf-8") == hand_edited
    assert human_footer in hook.read_text(encoding="utf-8")


def test_current_gate_region_is_a_byte_identical_no_op(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    registry = _one_gate_registry(fake_bin, version=2)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", registry)

    assert main([str(meta)]) == 0
    before = _hook_path(meta).read_text(encoding="utf-8")
    assert "# gate-version: 2" in before

    rc = main([str(meta)])
    assert rc == 0
    after = _hook_path(meta).read_text(encoding="utf-8")
    assert before == after
    assert "already installed and current" in capsys.readouterr().err


def test_stale_gate_region_is_replaced_in_place(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    v1_registry = _one_gate_registry(fake_bin, version=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v1_registry)
    assert main([str(meta)]) == 0
    v1_text = _hook_path(meta).read_text(encoding="utf-8")
    assert "# gate-version: 1" in v1_text

    v2_registry = _one_gate_registry(fake_bin, version=2)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v2_registry)
    rc = main([str(meta)])
    assert rc == 0
    v2_text = _hook_path(meta).read_text(encoding="utf-8")
    assert "# gate-version: 1" not in v2_text
    assert "# gate-version: 2" in v2_text
    # Exactly one block for the gate -- replaced, not duplicated.
    assert v2_text.count("# --- Gate: fake versioned gate (fake-versioned-gate) ---") == 1
    err = capsys.readouterr().err
    assert "replaced stale gate(s)" in err
    assert "fake-versioned-gate" in err

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 0
    assert "RAN:fake-versioned-gate.py" in result.stdout


def test_stale_gate_region_replacement_leaves_human_content_untouched(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    v1_registry = _one_gate_registry(fake_bin, version=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v1_registry)
    assert main([str(meta)]) == 0
    hook = _hook_path(meta)
    v1_text = hook.read_text(encoding="utf-8")

    # Hand-splice human content around the coordinator-managed block, as a
    # human editing an existing hook would: a preamble comment before it and
    # a footer echo after it, both outside the gate's own region.
    human_preamble = "# my custom pre-commit preamble\necho 'hello from human'\n"
    human_footer = "# my custom footer\necho 'human footer'\n"
    lines = v1_text.split("\n", 1)
    spliced = lines[0] + "\n" + human_preamble + lines[1]
    spliced = spliced.replace("\nexit 0\n", "\n" + human_footer + "exit 0\n", 1)
    hook.write_text(spliced, encoding="utf-8")
    os.chmod(hook, 0o755)

    v2_registry = _one_gate_registry(fake_bin, version=2)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", v2_registry)
    rc = main([str(meta)])
    assert rc == 0
    after = hook.read_text(encoding="utf-8")

    assert human_preamble in after
    assert human_footer in after
    assert "# gate-version: 1" not in after
    assert "# gate-version: 2" in after


# ---------------------------------------------------------------------------
# Orphaned regions — a gate retired OUT of the registry entirely (not merely
# bumped to a new version). Regression coverage for the
# `coordinator-precommit-exec-bit-check` retirement (2026-07-29): an
# already-installed hook's dead region used to survive forever (invisible to
# missing/stale classification, both scoped to the CURRENT registry) and
# would BLOCK every future commit once its script was deleted, rather than
# converge back to a clean install. See `_remove_orphaned_gate_regions`.
# ---------------------------------------------------------------------------

def test_orphaned_gate_region_is_removed_on_retirement(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    # Install with a two-gate registry (alpha retiring, beta staying).
    two_gate_registry = _two_gate_registry(fake_bin, version_a=1, version_b=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", two_gate_registry)
    assert main([str(meta)]) == 0
    gate_alpha, gate_beta = two_gate_registry
    before = _hook_path(meta).read_text(encoding="utf-8")
    assert gate_alpha.marker in before
    assert gate_beta.marker in before

    # Retire alpha out of the registry entirely (its script is gone too —
    # mirrors deleting coordinator-precommit-exec-bit-check from disk).
    (fake_bin / gate_alpha.filename).unlink()
    retired_registry = [gate_beta]
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", retired_registry)
    rc = main([str(meta)])
    assert rc == 0
    err = capsys.readouterr().err
    assert f"removed retired gate(s) [{gate_alpha.marker}]" in err

    after = _hook_path(meta).read_text(encoding="utf-8")
    assert gate_alpha.marker not in after
    assert gate_beta.marker in after

    # Behavioral proof, not just textual absence: the retired gate's block
    # is gone from the executed hook too, and beta still runs.
    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 0
    assert f"RAN:{gate_alpha.filename}" not in result.stdout
    assert f"RAN:{gate_beta.filename}" in result.stdout


def test_orphaned_gate_removal_is_idempotent(tmp_path, monkeypatch, capsys):
    """A second install run after the retired gate's region is already gone
    is a clean no-op — same idempotency property fresh-install/upgrade have."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    two_gate_registry = _two_gate_registry(fake_bin, version_a=1, version_b=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", two_gate_registry)
    assert main([str(meta)]) == 0
    gate_alpha, gate_beta = two_gate_registry

    (fake_bin / gate_alpha.filename).unlink()
    retired_registry = [gate_beta]
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", retired_registry)
    assert main([str(meta)]) == 0
    first_pass = _hook_path(meta).read_text(encoding="utf-8")
    capsys.readouterr()

    rc = main([str(meta)])
    assert rc == 0
    second_pass = _hook_path(meta).read_text(encoding="utf-8")
    assert first_pass == second_pass
    assert "already installed and current" in capsys.readouterr().err


def test_orphaned_gate_removal_leaves_human_content_untouched(tmp_path, monkeypatch):
    """The splice must not swallow anything outside the orphan's own bounded
    region -- same guarantee `_replace_stale_gate_regions` gives stale gates,
    exercised here for the removal path instead."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    two_gate_registry = _two_gate_registry(fake_bin, version_a=1, version_b=1)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", two_gate_registry)
    assert main([str(meta)]) == 0
    gate_alpha, gate_beta = two_gate_registry

    hook = _hook_path(meta)
    v1_text = hook.read_text(encoding="utf-8")
    human_preamble = "echo 'human preamble'\n"
    human_footer = "# my custom footer\necho 'human footer'\n"
    lines = v1_text.split("\n", 1)
    spliced = lines[0] + "\n" + human_preamble + lines[1]
    spliced = spliced.replace("\nexit 0\n", "\n" + human_footer + "exit 0\n", 1)
    hook.write_text(spliced, encoding="utf-8")
    os.chmod(hook, 0o755)

    (fake_bin / gate_alpha.filename).unlink()
    retired_registry = [gate_beta]
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", retired_registry)
    rc = main([str(meta)])
    assert rc == 0
    after = hook.read_text(encoding="utf-8")

    assert human_preamble in after
    assert human_footer in after
    assert gate_alpha.marker not in after
    assert gate_beta.marker in after


# ---------------------------------------------------------------------------
# Exit-code clamping (2026-07-29) — the highest-priority guard added to this
# file: a gate's raw exit code must NEVER escape this hook. A bare
# `|| exit $?` was safe only because all four scripts already registered
# here happened to exit 0 or 1; the fifth registry entry
# (`detect-staged-rollback`) has an own documented exit-2 transport-failure
# code, so this is no longer an accident of which scripts happen to be
# registered. See the module docstring's "Exit-code clamping" section.
# ---------------------------------------------------------------------------

def test_gate_script_exit_2_is_clamped_to_1(tmp_path, monkeypatch):
    """A gate exiting 2 (`detect-staged-rollback`'s own documented transport-
    failure code) must never escape this hook as a raw 2 — a pre-commit hook
    exiting 2 is read by the Claude Code harness as a blocking DENY that
    kills Bash/Write/Edit together, including the tools needed to repair the
    hook itself."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    gate = _GATE_REGISTRY[0]
    _write_stub_gates(fake_bin, exit_map={gate.filename: 2})
    assert main([str(meta)]) == 0

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr


def test_gate_script_exit_1_also_clamped_to_1(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    gate = _GATE_REGISTRY[0]
    _write_stub_gates(fake_bin, exit_map={gate.filename: 1})
    assert main([str(meta)]) == 0

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 1


def test_gate_script_arbitrary_nonzero_exit_clamped_to_1(tmp_path, monkeypatch):
    """Any surprise nonzero exit code — not just 1 or 2 — must still surface
    as exactly 1, never propagated raw."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    gate = _GATE_REGISTRY[0]
    _write_stub_gates(fake_bin, exit_map={gate.filename: 47})
    assert main([str(meta)]) == 0

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 1


def test_override_does_not_bypass_a_clamped_real_finding(tmp_path, monkeypatch):
    """Unlike the CANNOT-RUN cases (missing script/interpreter), a gate that
    actually RAN and returned nonzero — even a clamped exit 2 — is a real
    finding, not a "could not run" case. The wrapper-level override is
    PM-ruled to never bypass that (see `_gate_block`'s `_finding_branch` and
    the sibling `test_wrapper_override_never_bypasses_a_real_gate_finding`
    for the pre-existing rc=1 version of this same guarantee); this is the
    rc=2 variant that clamping specifically had to preserve."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    gate = _GATE_REGISTRY[0]
    _write_stub_gates(fake_bin, exit_map={gate.filename: 2})
    assert main([str(meta)]) == 0

    result = _run_hook(_hook_path(meta), meta, extra_env={gate.override_env: "1"})
    assert result.returncode == 1
    assert "SKIPPED" not in result.stderr
    assert "BLOCKED" in result.stderr


def test_hook_body_never_contains_a_bare_exit_dollar_question():
    """Regression guard for the exact defect this clamping fixes: the
    pre-clamp `_gate_block` emitted `|| exit $?` for both the python and bash
    exec shapes, propagating a raw gate exit code (including a 2) straight
    out of the hook. The emitted body must never contain that shape again."""
    content = _mod._hook_body(Path("/fake/bin"), _GATE_REGISTRY)
    assert "exit $?" not in content
    assert "|| exit" not in content


def test_hook_sh_syntax_is_valid(tmp_path, monkeypatch):
    """`sh -n` (parse-only) must accept the emitted hook body — a shell parse
    error is itself a way to leak a non-0/1 exit status (commonly exit 2)."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main([str(meta)]) == 0

    result = subprocess.run(
        ["/bin/sh", "-n", str(_hook_path(meta))], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_hook_always_exits_0_or_1_across_every_branch(tmp_path, monkeypatch):
    """Sweep every branch this hook can take (clean, missing script, missing
    interpreter, gate rc 1, gate rc 2, override) and assert the observed exit
    code is always in {0, 1} — never anything else."""
    gate = _GATE_REGISTRY[0]
    seen_codes = set()

    scenarios = [
        ("clean", None, None, None, False),
        ("rc1", {gate.filename: 1}, None, None, False),
        ("rc2", {gate.filename: 2}, None, None, False),
        ("rc2-override", {gate.filename: 2}, None, {gate.override_env: "1"}, False),
        ("missing-interpreter", None, "/nonexistent-empty-path-dir", None, False),
        ("missing-script", None, None, None, True),
    ]
    for label, exit_map, path_env, extra_env, delete_script in scenarios:
        case_home = tmp_path / f"case-{label}"
        meta = _make_meta_repo(case_home, monkeypatch)
        fake_bin = case_home / "fakebin"
        monkeypatch.setattr(_mod, "_bin_dir", lambda fake_bin=fake_bin: fake_bin)
        _write_stub_gates(fake_bin, exit_map=exit_map)
        assert main([str(meta)]) == 0
        if delete_script:
            (fake_bin / gate.filename).unlink()

        result = _run_hook(_hook_path(meta), meta, path_env=path_env, extra_env=extra_env)
        seen_codes.add(result.returncode)

    assert seen_codes <= {0, 1}
    assert 1 in seen_codes  # sanity: at least one branch actually blocked


# ---------------------------------------------------------------------------
# Fifth gate — detect-staged-rollback, registered 2026-07-29
# ---------------------------------------------------------------------------

def test_preexisting_v1_hook_with_raw_exit_dollar_question_is_refreshed(tmp_path, monkeypatch):
    """Simulates a LIVE hook already installed by the pre-clamp code (version
    1, `|| exit $?` shape) — a real box in this state exists as of this
    writing. Version-stamp currency (not byte comparison) is what
    `_install_or_append_hook` uses to decide "stale"; if this file's four
    original gates hadn't had their `version` bumped alongside the clamp
    fix, an already-installed box would report "already installed and
    current" forever and never receive the fix. Regression guard for that
    interaction, not for the clamp itself (see the dedicated clamping tests
    above)."""
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    gate = _GATE_REGISTRY[0]
    assert gate.version >= 2, "this test assumes the clamp fix bumped the gate's version"
    old_gate = _mod._Gate(
        marker=gate.marker,
        filename=gate.filename,
        kind=gate.kind,
        label=gate.label,
        override_env=gate.override_env,
        version=1,
    )
    old_body_lines = [
        "#!/bin/sh",
        "# Meta-repo pre-commit gates — fire before drift can land.",
        "",
        _mod._py_resolve_line(),
        "",
        f"# --- Gate: {old_gate.label} ({old_gate.marker}) ---",
        "# gate-version: 1",
        f'_gate_script="{(fake_bin / old_gate.filename).as_posix()}"',
        'if [ ! -f "$_gate_script" ]; then',
        "  exit 1",
        "else",
        '  "$_py" "$_gate_script" || exit $?',
        "fi",
        "",
        "exit 0",
        "",
    ]
    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("\n".join(old_body_lines), encoding="utf-8")
    os.chmod(hook, 0o755)
    assert "exit $?" in hook.read_text(encoding="utf-8")

    rc = main([str(meta)])
    assert rc == 0
    refreshed = hook.read_text(encoding="utf-8")
    assert "exit $?" not in refreshed
    assert f"# gate-version: {gate.version}" in refreshed


def test_staged_rollback_gate_is_registered_with_its_own_override():
    gate = next((g for g in _GATE_REGISTRY if g.marker == "detect-staged-rollback"), None)
    assert gate is not None, "detect-staged-rollback must be registered in _GATE_REGISTRY"
    assert gate.filename == "detect-staged-rollback.py"
    assert gate.kind == "python"
    assert gate.override_env == "COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK"


def test_staged_rollback_gate_runs_as_part_of_a_fresh_install(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main([str(meta)]) == 0

    result = _run_hook(_hook_path(meta), meta)
    assert result.returncode == 0
    assert "RAN:detect-staged-rollback.py" in result.stdout


def test_absent_gate_marker_is_appended_at_current_version(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    registry = _one_gate_registry(fake_bin, version=3)
    monkeypatch.setattr(_mod, "_GATE_REGISTRY", registry)

    hook = _hook_path(meta)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\necho "custom hook, no coordinator gates yet"\nexit 0\n', encoding="utf-8")
    os.chmod(hook, 0o755)

    rc = main([str(meta)])
    assert rc == 0
    content = hook.read_text(encoding="utf-8")
    assert "custom hook, no coordinator gates yet" in content
    assert "# gate-version: 3" in content
    err = capsys.readouterr().err
    assert "appended gate(s)" in err
    assert "replaced stale gate(s)" not in err

    result = _run_hook(hook, meta)
    assert result.returncode == 0
    assert "RAN:fake-versioned-gate.py" in result.stdout
    assert "custom hook, no coordinator gates yet" in result.stdout
