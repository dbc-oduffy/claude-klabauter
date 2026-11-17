"""
Regression coverage for ``coordinator_core.install.substrate._install_one``'s
overwrite policy, specifically the ``force_overwrite`` caller-forced class
added to repair the percolate-hook-files bug (a stale
``percolate-store.yaml``/README/`.gitkeep`/allowlist destination was silently
preserved as "operator customization" on re-install, because those files
carry non-code suffixes/names and never satisfied the pre-existing
suffix/name classification alone).

Pins three overwrite-policy legs:
  1. A hook-file-shaped destination that differs from source IS overwritten
     when the call site passes ``force_overwrite=True`` — the regression fix.
  2. A non-hook, non-code config file (``.toml``) is STILL preserved on diff
     when ``force_overwrite`` is left at its default — the operator-
     protection path must not regress.
  3. Destination-absent still copies; destination-identical is a no-op,
     regardless of ``force_overwrite``.

Spec backlink: dispatch brief "percolate-hook-files overwrite-policy repair",
2026-07-24 (bug confirmed against
Coordinator-content-repo coordinator/tests/test_percolate_store_copies_parity.py).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core.install import substrate


def test_force_overwrite_repairs_stale_hook_file_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The regression this fixes: a percolate-hook-shaped destination (e.g.
    percolate-store.yaml) that has drifted from source must be repaired on
    re-install when the call site marks it force_overwrite=True — it is a
    doctrine-tracked template, not operator config, even though its .yaml
    suffix/name would otherwise fall into the preserve-on-diff bucket."""
    src = tmp_path / "percolate-store.yaml"
    src.write_text("concerns: [fresh]\n", encoding="utf-8")
    dst = tmp_path / "dst" / "percolate-store.yaml"
    dst.parent.mkdir()
    dst.write_text("concerns: [stale]\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "concerns: [fresh]\n"
    out = capsys.readouterr().out
    assert "updated percolate-store.yaml" in out
    assert "re-install overwrites" in out


def test_non_hook_config_file_still_preserved_on_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Operator-protection path must not regress: a plain .toml (no
    force_overwrite passed, default False) with a differing destination is
    preserved, not overwritten."""
    src = tmp_path / "registry.toml"
    src.write_text("concerns = []\n", encoding="utf-8")
    dst = tmp_path / "dst" / "registry.toml"
    dst.parent.mkdir()
    dst.write_text("concerns = [\"operator-added\"]\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "concerns = [\"operator-added\"]\n"
    out = capsys.readouterr().out
    assert "operator-customized" in out
    assert "preserved" in out


def test_destination_absent_copies_regardless_of_force_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "README.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "dst" / "README.md"
    dst.parent.mkdir()

    substrate._install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "hello\n"


def test_destination_identical_is_noop_with_force_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Byte-identical destination is a no-op even under force_overwrite —
    no spurious "updated" print, no unnecessary copy."""
    src = tmp_path / "README.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "dst" / "README.md"
    dst.parent.mkdir()
    dst.write_text("hello\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "hello\n"
    out = capsys.readouterr().out
    assert "updated" not in out


def test_code_file_force_overwrite_reason_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Pre-existing code-file classification (suffix/name) still reports
    'code file' as the reason, independent of the new caller-forced class."""
    src = tmp_path / "foo.py"
    src.write_text("print('fresh')\n", encoding="utf-8")
    dst = tmp_path / "dst" / "foo.py"
    dst.parent.mkdir()
    dst.write_text("print('stale')\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "print('fresh')\n"
    out = capsys.readouterr().out
    assert "updated foo.py (code file; re-install overwrites)" in out


# ---------------------------------------------------------------- settings-home bin on the Windows user PATH


def _stub_windows_env(monkeypatch: pytest.MonkeyPatch, entries: list[str]) -> list[str]:
    """Stub the registry/cygpath seams of ``_windows_health_steps``; returns the prepend log."""
    prepended: list[str] = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", lambda: (entries, os.pathsep.join(entries), 2))
    monkeypatch.setattr(substrate, "_win_user_path_prepend", lambda e, raw, kind: prepended.append(e) or True)
    monkeypatch.setattr(substrate, "_ensure_interpreter_dir_on_windows_path", lambda check_only: None)
    monkeypatch.setattr(substrate, "_orphan_appx_stub", lambda path: False)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return prepended


def test_windows_health_steps_prepend_settings_home_bin_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepended = _stub_windows_env(monkeypatch, [])
    bin_dst = tmp_path / "settings-home" / "bin"
    substrate._windows_health_steps(bin_dst, check_only=False)
    assert prepended == [str(bin_dst)]


def test_windows_health_steps_leave_an_already_present_bin_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dst = tmp_path / "settings-home" / "bin"
    prepended = _stub_windows_env(monkeypatch, [str(bin_dst)])
    substrate._windows_health_steps(bin_dst, check_only=False)
    assert prepended == []


def test_windows_health_steps_check_only_and_disable_env_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dst = tmp_path / "settings-home" / "bin"
    prepended = _stub_windows_env(monkeypatch, [])
    substrate._windows_health_steps(bin_dst, check_only=True)
    assert prepended == []
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    substrate._windows_health_steps(bin_dst, check_only=False)
    assert prepended == []


def test_install_substrate_trampoline_delegates_without_its_own_path_writer() -> None:
    """One implementation: the trampoline hands off to ``substrate.main`` and carries no registry
    writer or hardcoded drive location of its own."""
    source = (Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "install-substrate.py").read_text(
        encoding="utf-8")
    assert '"coordinator_core.install.substrate", "main"' in source
    for forbidden in ("winreg", "setx", "SetEnvironmentVariable", "HKEY_CURRENT_USER"):
        assert forbidden not in source
    assert not re.search(r"""["'][A-Za-z]:[\/]""", source)
