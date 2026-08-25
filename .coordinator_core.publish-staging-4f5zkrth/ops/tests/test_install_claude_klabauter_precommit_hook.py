"""Regression tests for `coordinator_core.ops.install_claude_klabauter_precommit_hook`'s
C17 conversion off `coordinator_core.py_probe_sh`'s `$PATH`-walking interpreter
probe onto a baked `sys.executable` assignment.

Spec: `state/dispatch-briefs/2026-08-21-the-cli-bootstrap-tax-dies-at-the-
interpreter-floor/C17.md`. What this file pins:
  (a) the emitted hook body never contains a `$PATH` walk (no `_py_resolve`
      function, no `for _py_dir in $PATH` loop, no `python_probe_lines` text)
      -- the regression this whole chunk exists to prevent reintroducing;
  (b) the emitted `_py="..."` line is exactly `sys.executable`
      forward-slash-normalized, double-quote-escaped correctly for embedded
      special characters;
  (c) BEHAVIORAL execution: the emitted hook, run through a real `sh`, resolves
      `$_py` to a working interpreter and a stub gate actually runs -- not just
      a textual assertion (same discipline `test_install_meta_repo_precommit_
      hook.py`'s own docstring names: substring presence is exactly what a
      "looks wired but is dead" defect would still pass);
  (d) the self-heal path: a baked `_py` that fails `[ -x "$_py" ]` at hook-run
      time clears to empty and falls into the SAME missing-interpreter
      CANNOT-PROCEED branch the module already had (BLOCKED banner, exit 1,
      never a raw code), with the C17-updated re-run-the-installer remediation
      text -- never a silent skip;
  (e) `AC12`'s exclusion is honored implicitly: nothing here touches
      `install_doe_claude_precommit_hook` (another repo's surface) or
      `git_hook_install`/`install_meta_repo_precommit_hook` (not this repo's
      commit hot path) -- this file is scoped to `install_claude_klabauter_precommit_
      hook` alone, matching the module's own `writes:` scope.

Negative-spec this file enforces:
  - `python_probe_sh`/`python_probe_lines` must never appear as an import or
    a call anywhere in this module's source -- a regression that re-wires the
    shared PATH-walk primitive back in must fail here, not just visually.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import install_claude_klabauter_precommit_hook as _mod
from coordinator_core.ops.install_claude_klabauter_precommit_hook import (
    _GATE_REGISTRY,
    _baked_interpreter_path,
    _hook_body,
    _py_resolve_line,
    _sh_double_quote_escape,
    main,
)
from coordinator_core.testing.sh_interpreter import require_sh_interpreter

# Behavioral tests spawn real `git`/`sh` processes -- cadence-tier, not the
# per-commit fast path. Spawn ratchet: coordinator_core/tests/
# test_no_new_spawning_tests.py.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_claude_klabauter_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo this module's `_self_repo_root()` is monkeypatched to
    treat as claude-klabauter itself -- mirrors this module's own docstring
    ("Does not install into THIS session's own live repo")."""
    repo = tmp_path / "claude-klabauter-fake"
    repo.mkdir()
    _git(repo, "init", "-q")
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(repo))
    monkeypatch.setattr(_mod, "_bin_dir", lambda: tmp_path / "bin-unused")
    return repo


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-commit"


def _write_stub_gates(fake_bin: Path, exit_map: dict | None = None) -> None:
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


def _run_hook(hook: Path, cwd: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [require_sh_interpreter(), str(hook)], cwd=str(cwd), capture_output=True, text=True, env=env
    )


# ---------------------------------------------------------------------------
# (a) No PATH walk left in the emitted body or the module source
# ---------------------------------------------------------------------------


def test_emitted_body_contains_no_path_walk():
    body = _hook_body(_GATE_REGISTRY)
    assert "_py_resolve" not in body
    assert "for _py_dir in $PATH" not in body
    assert "windowsapps" not in body.lower()
    assert "command -v" not in body


def test_module_source_never_imports_or_calls_python_probe_sh():
    """The docstring is allowed to MENTION `py_probe_sh`/`python_probe_lines`
    in prose (it explains the primitive this module used to call and no
    longer does) -- mirrors `test_install_meta_repo_precommit_hook.py::
    test_no_hardcoded_absolute_literal_outside_the_docstring`'s own
    docstring-vs-code distinction. What must never exist is an actual
    import or call."""
    src = inspect.getsource(_mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "coordinator_core.py_probe_sh":
            pytest.fail("install_claude_klabauter_precommit_hook still imports py_probe_sh")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("coordinator_core.py_probe_sh", "py_probe_sh"):
                    pytest.fail("install_claude_klabauter_precommit_hook still imports py_probe_sh")
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name != "python_probe_lines", "a call to python_probe_lines survives in code"


# ---------------------------------------------------------------------------
# (b) The baked line's shape
# ---------------------------------------------------------------------------


def test_py_resolve_line_bakes_sys_executable_forward_slash_normalized():
    line = _py_resolve_line()
    expected_path = "/".join(sys.executable.replace("\\", "/").split("/"))
    assert f'_py="{expected_path}"' in line
    assert "\\" not in line.split("\n")[0].split("_py=")[1]


def test_py_resolve_line_self_heal_clears_on_missing():
    line = _py_resolve_line()
    assert '[ -x "$_py" ] || _py=""' in line


def test_baked_interpreter_path_matches_sys_executable_content():
    baked = _baked_interpreter_path()
    assert baked.replace("/", os.sep) == sys.executable.replace("\\", os.sep) or (
        baked == sys.executable.replace("\\", "/")
    )
    assert "\\" not in baked


def test_double_quote_escape_orders_backslash_first():
    # Backslash must be escaped before the other three, or a later
    # substitution's own inserted backslash would be re-escaped.
    assert _sh_double_quote_escape('a"b$c`d\\e') == 'a\\"b\\$c\\`d\\\\e'


def test_double_quote_escape_handles_plain_path_unchanged():
    synthetic_path = "C:/Users/x/python.exe"  # abs-path-ok: synthetic fixture string, not a real host path
    assert _sh_double_quote_escape(synthetic_path) == synthetic_path


# ---------------------------------------------------------------------------
# (c) Behavioral: the emitted hook actually resolves and runs
# ---------------------------------------------------------------------------


def test_installed_hook_resolves_baked_interpreter_and_runs_gate(tmp_path, monkeypatch):
    repo = _make_claude_klabauter_repo(tmp_path, monkeypatch)
    # The emitted gate path is repo-root-relative (`_gate_block`'s own
    # docstring), so the stub scripts must live under the FAKE REPO's own
    # `coordinator/bin/`, not `_bin_dir()` (which is only the install-time
    # existence-check path and is separately monkeypatched below).
    fake_bin = repo / "coordinator" / "bin"
    _write_stub_gates(fake_bin)
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    rc = main([str(repo)])
    assert rc == 0

    hook = _hook_path(repo)
    assert hook.exists()

    result = _run_hook(hook, cwd=repo)
    assert result.returncode == 0, result.stderr
    for gate in _GATE_REGISTRY:
        assert f"RAN:{gate.filename}" in result.stdout


def test_installed_hook_blocks_loudly_when_baked_interpreter_missing(tmp_path, monkeypatch):
    """Self-heal path: a baked `_py` that does not resolve at hook-run time
    (simulated here by installing, then rewriting the hook's `_py=` line to
    point at a nonexistent path) BLOCKS with the re-run-the-installer
    remediation -- never a silent skip, never a raw exit code."""
    repo = _make_claude_klabauter_repo(tmp_path, monkeypatch)
    # The emitted gate path is repo-root-relative (`_gate_block`'s own
    # docstring), so the stub scripts must live under the FAKE REPO's own
    # `coordinator/bin/`, not `_bin_dir()` (which is only the install-time
    # existence-check path and is separately monkeypatched below).
    fake_bin = repo / "coordinator" / "bin"
    _write_stub_gates(fake_bin)
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    rc = main([str(repo)])
    assert rc == 0
    hook = _hook_path(repo)

    text = hook.read_text(encoding="utf-8")
    missing_path = str(tmp_path / "no-such-interpreter-here")
    lines = text.split("\n")
    new_lines = []
    for line in lines:
        if line.startswith('_py="'):
            new_lines.append(f'_py="{missing_path}"')
        else:
            new_lines.append(line)
    hook.write_text("\n".join(new_lines), encoding="utf-8")

    result = _run_hook(hook, cwd=repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "re-run the coordinator installer" in result.stderr
    assert "install-claude-klabauter-precommit-hook" in result.stderr


# ---------------------------------------------------------------------------
# (d) Refresh mechanism still catches this line changing
# ---------------------------------------------------------------------------


def test_stale_pre_c17_body_is_refreshed_to_baked_line(tmp_path, monkeypatch):
    """A hook this installer wrote under the pre-C17 PATH-walk shape (its own
    header intact) is treated as stale and rewritten wholesale -- the SAME
    byte-for-byte compare mechanism the module docstring already documents
    for this class of change, exercised here for this specific edit."""
    repo = _make_claude_klabauter_repo(tmp_path, monkeypatch)
    # The emitted gate path is repo-root-relative (`_gate_block`'s own
    # docstring), so the stub scripts must live under the FAKE REPO's own
    # `coordinator/bin/`, not `_bin_dir()` (which is only the install-time
    # existence-check path and is separately monkeypatched below).
    fake_bin = repo / "coordinator" / "bin"
    _write_stub_gates(fake_bin)
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)

    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    stale_body = _hook_body(_GATE_REGISTRY).replace(
        _py_resolve_line(), '_py_resolve() {\n  :\n}\n_py="stale"'
    )
    hook.write_text(stale_body, encoding="utf-8")

    rc = main([str(repo)])
    assert rc == 0

    refreshed = hook.read_text(encoding="utf-8")
    assert "_py_resolve()" not in refreshed
    assert _py_resolve_line() in refreshed
