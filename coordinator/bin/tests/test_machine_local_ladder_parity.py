"""test_machine_local_ladder_parity — cross-implementation settings-home ladder
coverage for the three independently-authored `claude-machine-local.{sh,ps1}`
consumer templates plus the Python wrapper's `_settings_home()`.

Spec backlink: docs/plans/2026-05-20-portable-code-substrate.md §5.2/§5.3
Review: code-reviewer — F4, no automated ladder-parity test existed for the
`.sh`/`.ps1` templates; only the Python wrapper's ladder was regression-tested
(test_claude_machine_local.py's `test_settings_home_*` trio). Finding 1 and
Finding 2 in the same review are concrete instances of the drift-undetected
class this test exists to catch — the `.sh` and `.ps1` ladders are hand-
duplicated (they cannot import coordinator/lib/settings-home.sh per the
standalone-portability constraint), so nothing previously caught them
diverging from the canonical two-rung precedence.

Approach: source/dot-source the real template files under three env
scenarios (COORDINATOR_SETTINGS_HOME set; CLAUDE_HOME set; neither set), with
a fake `python3` shim on PATH that captures the reader path the script
composed (`<settings-home>/bin/_machine_local.py`) instead of actually
invoking the reader. This exercises the real ladder-computation lines in
each file — only the Python interpreter is faked — and derives the resolved
settings-home from the captured reader path for comparison against the
canonical formula.

`.ps1` coverage SKIPS gracefully (not fails) when `pwsh` is not on PATH — CI
machines may lack a PowerShell runtime.
"""
from __future__ import annotations

import os
import posixpath
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_TESTS_DIR = _THIS.parent
_BIN_DIR = _TESTS_DIR.parent

for _p in (str(_BIN_DIR / "lib"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# `claude-machine-local.{sh,ps1}` are example-doctrine-repo-owned templates
# (example-doctrine-repo/coordinator/templates/bin/) -- they are NOT vendored into
# claude-klabauter's own tree (same D5-shaped split as canonical-structure.yaml; see
# coordinator_core/install/scaffold_structure.py module docstring). A prior
# `Path(__file__).../ "templates" / "bin"` resolution here assumed a
# monorepo layout that never held on this checkout -- resolve via
# `coordinator_registry.doe_root()`, the same sibling-repo resolution ladder
# `test_agent_helper_shim_doe_corpus_coverage.py` uses, and skip (not fail)
# when the example-doctrine-repo checkout is unresolvable on this machine/persona.
try:
    import coordinator_registry  # noqa: E402
except Exception as _coordinator_registry_import_exc:  # noqa: E402
    pytest.skip(
        "coordinator_registry unimportable (example-doctrine-repo root unresolvable at "
        f"import time): {_coordinator_registry_import_exc}",
        allow_module_level=True,
    )


def _resolve_doe_templates_bin_dir() -> Path:
    try:
        root = coordinator_registry.doe_root()
    except Exception as exc:  # coordinator_registry._DoeUnresolvable, etc.
        pytest.skip(f"example-doctrine-repo root unresolvable via coordinator_registry.doe_root(): {exc}")
    p = Path(root) / "coordinator" / "templates" / "bin"
    if not p.is_dir():
        pytest.skip(f"resolved example-doctrine-repo root has no coordinator/templates/bin/ at {p}; skipping ladder-parity check")
    return p


_TEMPLATES_BIN_DIR = _resolve_doe_templates_bin_dir()
_SH_SCRIPT = _TEMPLATES_BIN_DIR / "claude-machine-local.sh"
_PS1_SCRIPT = _TEMPLATES_BIN_DIR / "claude-machine-local.ps1"

_PWSH = shutil.which("pwsh")

# Three env scenarios, matching test_claude_machine_local.py's Python-side
# ladder coverage (test_settings_home_override_wins /
# _honors_claude_home / _default_falls_back_to_home).
_SCENARIOS = [
    pytest.param(
        {"COORDINATOR_SETTINGS_HOME": "/tmp/ladder-override-home", "CLAUDE_HOME": "/tmp/ladder-claude-home-ignored"},
        "/tmp/ladder-override-home",
        id="coordinator-settings-home-wins",
    ),
    pytest.param(
        {"COORDINATOR_SETTINGS_HOME": "", "CLAUDE_HOME": "/tmp/ladder-claude-home"},
        # .sh composes this via plain bash "/"-string-concat (POSIX-literal
        # env value, no real filesystem lookup involved) -- posixpath.join
        # matches that, unlike os.path.join which on Windows leaves the "/"
        # prefix untouched but joins the suffix with "\".
        posixpath.join("/tmp/ladder-claude-home", ".coordinator-claude-settings"),
        id="claude-home-honored",
    ),
    pytest.param(
        {"COORDINATOR_SETTINGS_HOME": "", "CLAUDE_HOME": ""},
        os.path.join(os.path.expanduser("~"), ".coordinator-claude-settings"),
        id="default-falls-back-to-home",
    ),
]


def _make_fake_python3(tmp_path: Path, capture_file: Path) -> Path:
    """Write a fake `python3` shim that captures argv[1] (the reader path)
    instead of actually invoking the reader, then exits 1 (clean absence —
    harmless: the caller's `keys` loop just finds no keys and exports
    nothing)."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    shim = fake_bin / "python3"
    shim.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf '%s' "$1" > "{capture_file}"
            exit 1
            """),
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _derive_settings_home(reader_path: str) -> str:
    """<settings-home>/bin/_machine_local.py -> <settings-home>.

    The captured reader_path came straight out of bash's own plain "/"
    string-concat (claude-machine-local.sh composes
    ``"$_ml_settings_home/bin/_machine_local.py"`` with no path-arithmetic
    library involved) -- always POSIX-separated regardless of host OS. Parse
    it with ``posixpath``, not ``pathlib.Path`` -- on Windows, ``Path()``
    silently re-renders a POSIX-separated string with backslashes on
    ``str()``, which broke this comparison against the (also POSIX) expected
    literals in ``_SCENARIOS`` for every non-Windows-native input.
    """
    return posixpath.dirname(posixpath.dirname(reader_path))


@pytest.mark.parametrize("env_overrides,expected_settings_home", _SCENARIOS)
def test_sh_ladder_matches_canonical_precedence(tmp_path, env_overrides, expected_settings_home):
    capture_file = tmp_path / "captured-reader-path.txt"
    fake_bin = _make_fake_python3(tmp_path, capture_file)

    env = dict(os.environ)
    env.pop("CLAUDE_MACHINE_LOCAL_SOURCED", None)
    for key, value in env_overrides.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", "-c", f"source '{_SH_SCRIPT}'"],
        env=env, capture_output=True, text=True,
    )
    assert capture_file.exists(), (
        f"Fake python3 shim was never invoked — .sh ladder did not reach the "
        f"reader-invocation line. stderr: {result.stderr!r}"
    )
    reader_path = capture_file.read_text(encoding="utf-8")
    resolved = _derive_settings_home(reader_path)
    assert resolved == expected_settings_home, (
        f".sh resolved settings-home {resolved!r}, expected {expected_settings_home!r} "
        f"(env: {env_overrides!r}, stderr: {result.stderr!r})"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh not on PATH — cannot exercise .ps1 ladder")
@pytest.mark.parametrize("env_overrides,expected_settings_home", _SCENARIOS)
def test_ps1_ladder_matches_canonical_precedence(tmp_path, env_overrides, expected_settings_home):
    capture_file = tmp_path / "captured-reader-path.txt"
    fake_bin = _make_fake_python3(tmp_path, capture_file)
    # pwsh's Get-Command PATH lookup on POSIX honors the executable bit
    # regardless of extension, so the bash shim above is invocable directly.

    env = dict(os.environ)
    env.pop("CLAUDE_MACHINE_LOCAL_SOURCED", None)
    for key, value in env_overrides.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command", f". '{_PS1_SCRIPT}'"],
        env=env, capture_output=True, text=True,
    )
    assert capture_file.exists(), (
        f"Fake python3 shim was never invoked — .ps1 ladder did not reach the "
        f"reader-invocation line. stderr: {result.stderr!r}"
    )
    reader_path = capture_file.read_text(encoding="utf-8")
    resolved = _derive_settings_home(reader_path)
    assert resolved == expected_settings_home, (
        f".ps1 resolved settings-home {resolved!r}, expected {expected_settings_home!r} "
        f"(env: {env_overrides!r}, stderr: {result.stderr!r})"
    )
