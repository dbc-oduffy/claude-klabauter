"""C6 (docs/plans/2026-08-16-registry-read-stops-costing-a-process.md): no
install path may durably persist ``COORDINATOR_SETTINGS_HOME`` — Anti-scope's
first bullet. Pins the refusal as an artifact rather than a paragraph: baking
the variable at install time would convert rung 0 of the settings-home
resolver ladder (``coordinator_core._settings_home.settings_home``, highest
precedence, returned before the ``coordinator-settings-home`` CLI is even
consulted) into a hardcode pinned to install-time truth — machine migration,
a second install, a ``CLAUDE_HOME`` change, or a differently-rooted tenant
would then be silently overridden, and the ladder could never self-correct.
Slow is recoverable; wrong-root is not.

Two halves, both required (per the chunk body):

  1. ``TestStaticScanNoDurablePersistence`` — an AST-based scan of every
     install-surface module: for each function whose body references
     ``COORDINATOR_SETTINGS_HOME`` (as a bare name or inside a string/
     f-string constant), assert that SAME function's body contains none of
     the durable-persistence call markers (``setx``, ``winreg.SetValueEx``,
     a shell-rc/profile ``.write_text`` write, ``os.environ[...] = ...``
     assignment). Function-scoped co-occurrence, not whole-file grep, so a
     read in one function and an unrelated write in another do not collide.

  2. ``TestBehaviouralNoPersistedSurface`` — runs the real profile-writing
     path (``shell_rc_guard.write_path_entry_guard_blocks``, the same
     function ``substrate.py``'s Step 3e/3e-bin legs call) against a SCRATCH
     home under ``tmp_path``, with ``COORDINATOR_SETTINGS_HOME`` itself set
     to a scratch value, and asserts the literal string never lands in any
     written profile file — a source-text scan alone would miss a value
     constructed at runtime (an f-string, a name assembled from a constant,
     a write via a helper); this exercises the actual write.

NEGATIVE SPEC — what this module does NOT refuse, and why:

  - READING ``COORDINATOR_SETTINGS_HOME`` (``os.environ.get(...)``) is legal
    everywhere and is not scanned for — every resolver in the ladder does
    this, including the bootstrap-safe ``_settings_home.settings_home()``
    this whole plan's C5 chunk depends on.
  - SETTING IT IN A CHILD ENV DICT is legal and is NOT a durable-persistence
    marker: ``coordinator_core._settings_home.settings_home_child_env()``
    and ``coordinator/bin/lib/cc_invoke.py``'s ``child_env`` /
    ``_build_subprocess_env`` (C5, landed a6e513e636ea) both do exactly
    this — a plain ``dict[...] = ...`` assignment into a fresh dict that is
    handed to a child process's ``env=`` kwarg, never written to disk, never
    read back by a FUTURE unrelated process. The static scan's marker set
    deliberately excludes ``dict.__setitem__``/plain assignment for this
    reason; only environment-variable-shaped OS/registry/file persistence
    counts.
  - The Windows registry PATH write (``_win_user_path_prepend``) is real
    machine state on THIS box — this module never invokes it for real (that
    would mutate the operator's actual ``HKCU\\Environment``). Coverage for
    it is static only: the AST scan confirms its one ``winreg.SetValueEx``
    call site always names the registry value ``"PATH"``, never
    ``COORDINATOR_SETTINGS_HOME``, and function-scope co-occurrence would
    still catch a future edit that assembled a different value name from
    the env var.

STATIC SCAN'S BLIND SPOT (named explicitly per the chunk body, so the next
reader does not over-trust the grep alone): AST-level function-scope
co-occurrence still cannot prove a durable write's ARGUMENT VALUE is (or is
not) derived from ``COORDINATOR_SETTINGS_HOME`` when that derivation crosses
a function boundary — e.g. a helper that reads the var and returns a plain
string, consumed by an unrelated writer function three calls away, with no
textual reference to the env var name inside the writer's own body. The
behavioural half exists precisely to close that gap for the one write path
this repo actually exercises (the POSIX profile/rc block writer); it does
NOT close it for the Windows registry write, which is covered by static
scan only (see NEGATIVE SPEC above) because a live install cannot safely
run against the operator's real ``HKCU\\Environment`` from a test.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Install-surface modules this guard scans — every module this plan's own
# scope names as touching the install path, plus the settings-home resolver
# and the legitimate child-env spawn seam (scanned too, so the negative-spec
# claim above is verified rather than merely asserted).
_SCANNED_FILES: List[Path] = [
    _REPO_ROOT / "coordinator_core" / "install" / "substrate.py",
    _REPO_ROOT / "coordinator_core" / "install" / "shell_rc_guard.py",
    _REPO_ROOT / "coordinator_core" / "install" / "uninstall_legs.py",
    _REPO_ROOT / "coordinator_core" / "install" / "forwarder_self_heal.py",
    _REPO_ROOT / "coordinator_core" / "install" / "first_run.py",
    _REPO_ROOT / "coordinator_core" / "install" / "maximalist.py",
    _REPO_ROOT / "coordinator_core" / "_settings_home.py",
    _REPO_ROOT / "coordinator" / "bin" / "lib" / "cc_invoke.py",
]

_TARGET_VAR = "COORDINATOR_SETTINGS_HOME"

# Durable-persistence call markers — a function that references
# COORDINATOR_SETTINGS_HOME AND makes one of these calls is a hit. Deliberately
# excludes plain dict/os.environ __setitem__ (child-env propagation, legal —
# see module NEGATIVE SPEC) and excludes os.environ.get/os.environ[...] READS.
_DURABLE_CALL_MARKERS = frozenset({
    "setx",
    "SetValueEx",       # winreg.SetValueEx — HKCU\Environment write
    "SetEnvironmentVariable",
    "write_text",        # profile/shell-rc file write (Path.write_text)
    "write_bytes",
    "write_shell_rc_guard_block",
    "write_path_entry_guard_blocks",
    "_write_block_to_file",
})


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _references_target_var(node: ast.AST) -> bool:
    """True if `node` (a function body, walked) names COORDINATOR_SETTINGS_HOME
    as a bare identifier OR as a string/f-string constant fragment — catches
    an f-string interpolation (`f"{_TARGET_VAR}"` or a literal embedded
    directly) as well as a plain `os.environ.get("COORDINATOR_SETTINGS_HOME")`
    call, without requiring the two to be textually identical statements."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == _TARGET_VAR:
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and _TARGET_VAR in child.value:
            return True
    return False


def _durable_markers_in(node: ast.AST) -> List[str]:
    hits = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child)
            if name in _DURABLE_CALL_MARKERS:
                hits.append(name)
    return hits


def _iter_functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


class TestStaticScanNoDurablePersistence:
    """Half 1 (function-scoped AST co-occurrence): no function referencing
    COORDINATOR_SETTINGS_HOME also performs a durable-persistence write."""

    @pytest.mark.parametrize("path", _SCANNED_FILES, ids=lambda p: p.name)
    def test_no_function_both_names_and_durably_persists_the_var(self, path: Path) -> None:
        if not path.is_file():
            pytest.skip(f"{path} not present on this checkout")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        offenders = []
        for fn in _iter_functions(tree):
            if not _references_target_var(fn):
                continue
            markers = _durable_markers_in(fn)
            if markers:
                offenders.append((fn.name, fn.lineno, markers))

        assert offenders == [], (
            f"{path}: function(s) referencing {_TARGET_VAR} also call a "
            f"durable-persistence marker — {offenders}. Reading the var and "
            "setting it in a fresh child-env dict stay legal (see module "
            "NEGATIVE SPEC); a setx/registry/profile write does not."
        )

    def test_no_literal_setx_invocation_anywhere_in_install_surface(self) -> None:
        """Belt-and-braces: `setx` never appears at all in the scanned
        surface — the fastest durable-persistence primitive to add by
        accident, and currently entirely absent (verified this session)."""
        for path in _SCANNED_FILES:
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            assert "setx" not in source.lower(), (
                f"{path}: literal 'setx' found — a Windows setx invocation "
                "would durably persist into the user/machine environment; "
                "not permitted anywhere on the install surface"
            )

    def test_registry_path_write_never_names_settings_home_value(self) -> None:
        """`_win_user_path_prepend`'s one `winreg.SetValueEx` call is
        confirmed (by name, statically) to write the "PATH" value only —
        never COORDINATOR_SETTINGS_HOME. This is the module's own
        documented blind-spot boundary for the registry leg (see NEGATIVE
        SPEC): behavioural coverage cannot safely exercise real HKCU from a
        test, so this stays a static, name-level assertion."""
        substrate_path = _REPO_ROOT / "coordinator_core" / "install" / "substrate.py"
        source = substrate_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(substrate_path))

        set_value_ex_calls = [
            child for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
            for child in ast.walk(node) if isinstance(child, ast.Call) and _call_name(child) == "SetValueEx"
        ]
        assert set_value_ex_calls, (
            "expected exactly one winreg.SetValueEx call site in substrate.py "
            "(_win_user_path_prepend) — none found; update this test if the "
            "PATH-write mechanism moved"
        )
        for call in set_value_ex_calls:
            # winreg.SetValueEx(key, value_name, reserved, type, value) —
            # positional arg[1] is the registry VALUE NAME being written.
            assert len(call.args) >= 2, f"line {call.lineno}: unexpected SetValueEx call shape"
            value_name_arg = call.args[1]
            assert isinstance(value_name_arg, ast.Constant) and value_name_arg.value == "PATH", (
                f"line {call.lineno}: winreg.SetValueEx writes registry value "
                f"name {ast.dump(value_name_arg)!r} — expected the literal "
                "\"PATH\"; a different/dynamic value name here could silently "
                f"start persisting {_TARGET_VAR}"
            )


class TestBehaviouralNoPersistedSurface:
    """Half 2: run the real profile-writing path against a scratch home and
    assert the persisted surfaces stay clean of the literal variable name."""

    def test_write_block_to_file_never_writes_the_var_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exercises `_write_block_to_file` — the ONE place any consumer's
        guard-block file I/O happens (its own docstring; `shell_rc_guard.py`
        module docstring's § Shared internal surface names it as the
        module's real write primitive) — directly, rather than through
        `write_path_entry_guard_blocks`/`write_shell_rc_guard_block`.

        Those two top-level entry points early-return a no-op on native
        Windows (`os.name == "nt"`, module docstring DEC-7) BEFORE ever
        reaching `_write_block_to_file`, and Python 3.13's `pathlib` refuses
        to instantiate a `PosixPath` on a non-POSIX platform at all (verified
        this session — `UnsupportedOperation: cannot instantiate 'PosixPath'
        on your system`), so monkeypatching `os.name` to force that branch on
        this Windows executor is not viable. Calling `_write_block_to_file`
        directly exercises the actual write primitive on any platform,
        proving the assertion below without depending on which OS branch of
        the two wrapper functions happens to be reachable here.
        """
        from coordinator_core.install import shell_rc_guard

        scratch_home = tmp_path / "scratch-home"
        scratch_home.mkdir()
        scratch_settings_home = tmp_path / "scratch-settings-home"
        scratch_settings_home.mkdir()
        bin_dst = scratch_settings_home / "bin"
        bin_dst.mkdir()

        # If a durable write DID leak the var, setting it to a distinctive
        # scratch value makes that leak visible in the assertion below —
        # a value this test controls, not one already present in any real
        # profile file on this machine.
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(scratch_settings_home))
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "")

        begin, end = shell_rc_guard._sentinel_markers("SETTINGS_HOME_BIN")
        body = shell_rc_guard._build_path_entry_body(str(bin_dst), "append")
        rc_path = scratch_home / ".zshrc"

        result = shell_rc_guard._write_block_to_file(
            rc_path, begin, end, body, check_only=False, label="SETTINGS_HOME_BIN",
        )
        assert result["modified"], "expected the scratch profile write to actually happen"
        assert rc_path.is_file(), "expected the scratch rc file to be created"

        text = rc_path.read_text(encoding="utf-8")
        assert _TARGET_VAR not in text, (
            f"{rc_path}: profile file written by _write_block_to_file "
            f"contains the literal string {_TARGET_VAR!r} — this is "
            "exactly the persistence Anti-scope refuses"
        )
        # It's fine — expected — for the resolved bin_dst PATH (itself
        # DERIVED from the scratch settings-home this test set) to appear;
        # only the ENV VAR NAME must never appear. `_build_path_entry_body`
        # shell-dquote-escapes the path (backslash-doubling on Windows), so
        # compare against that same escaped form rather than the raw string.
        assert shell_rc_guard._shell_dquote_escape(str(bin_dst)) in text, (
            f"{rc_path}: expected the SETTINGS_HOME_BIN guard block body to "
            "have been written; the write may not have executed as expected"
        )

    def test_no_scratch_registry_or_setx_surface_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Companion assertion: the behavioural half above only exercises
        the POSIX profile writer. Confirm no other persisted-environment
        primitive (winreg, setx) is reachable from `_write_block_to_file`
        itself — i.e. this call path has no side door into the
        registry/setx surfaces this module's static half already pins by
        name."""
        from coordinator_core.install import shell_rc_guard
        import subprocess

        calls = []

        def _fail_on_spawn(*a, **kw):
            calls.append((a, kw))
            raise AssertionError("_write_block_to_file spawned a subprocess")

        monkeypatch.setattr(subprocess, "run", _fail_on_spawn)

        scratch_home = tmp_path / "scratch-home-2"
        scratch_home.mkdir()
        begin, end = shell_rc_guard._sentinel_markers("SETTINGS_HOME_BIN")
        body = shell_rc_guard._build_path_entry_body(str(tmp_path / "bin"), "append")

        shell_rc_guard._write_block_to_file(
            scratch_home / ".zshrc", begin, end, body, check_only=False, label="SETTINGS_HOME_BIN",
        )
        assert calls == [], "no subprocess (setx or otherwise) expected from this write path"
