"""
coordinator_core.install.tests.test_c10a_whoami_steps_retired — C10a-1/2 retirement.

Spec: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (IBMDT-C2, item 1)

`_c10a_steps` (coordinator_core/install/substrate.py) used to run three sub-steps:
C10a-1 (relocate `coordinator-whoami/` from a legacy install-base dir or the plugin's
`whoami/` source into the settings home, then replace the legacy dir with a compat
pointer), C10a-2 (register the `coordinator.whoami_src` machine-local key), and
C10a-3 (venv rebuild, break-glass only). `coordinator_whoami` ships no package source
on any current box (state/cross-repo/archive/2026-09-12-doe-claude-em-installer-
still-registers-retired-whoami-src.md), so C10a-1/2 only ever populated or advertised
a stale/empty seam. This module asserts they no longer run: no relocation copy, no
legacy-dir replacement, no `coordinator.whoami_src` registry write — while C10a-3
(gated by `allow_venv_fallback`) is unaffected, since it already probes
`plugin_root / "whoami"` directly, independent of C10a-1's copy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install.substrate import _c10a_steps


def _make_dirs(tmp_path: Path) -> "tuple[Path, Path, Path, Path]":
    install_base = tmp_path / "home"
    settings_home_path = tmp_path / "settings-home"
    plugin_root = tmp_path / "plugin-root"
    bin_dst = settings_home_path / "bin"
    for d in (install_base, settings_home_path, plugin_root, bin_dst):
        d.mkdir(parents=True)
    return install_base, settings_home_path, plugin_root, bin_dst


def test_c10a_steps_does_not_relocate_legacy_whoami_dir(tmp_path):
    """A real legacy `<install_base>/.claude/coordinator-whoami` dir must
    survive untouched — Step C10a-1's relocation copy and compat-pointer
    replacement are retired."""
    install_base, settings_home_path, plugin_root, bin_dst = _make_dirs(tmp_path)

    legacy_whoami = install_base / ".claude" / "coordinator-whoami"
    legacy_whoami.mkdir(parents=True)
    (legacy_whoami / "marker").write_text("real dir", encoding="utf-8")

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    assert legacy_whoami.is_dir() and not legacy_whoami.is_symlink(), (
        "C10a-1 is retired: the legacy real dir must not be replaced by a compat pointer"
    )
    assert (legacy_whoami / "marker").read_text(encoding="utf-8") == "real dir"
    dst_whoami = settings_home_path / "coordinator-whoami"
    assert not dst_whoami.exists(), (
        "C10a-1 is retired: nothing should be copied into the settings-home destination"
    )


def test_c10a_steps_does_not_relocate_plugin_whoami_source(tmp_path):
    """A plugin-side `whoami/` package source must not be copied into the
    settings home either — the C10a-1 copy leg covered both source
    candidates and both are retired."""
    install_base, settings_home_path, plugin_root, bin_dst = _make_dirs(tmp_path)

    plugin_whoami = plugin_root / "whoami"
    plugin_whoami.mkdir(parents=True)
    (plugin_whoami / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    dst_whoami = settings_home_path / "coordinator-whoami"
    assert not dst_whoami.exists(), (
        "C10a-1 is retired: the plugin-side whoami/ source must not be copied"
    )


def test_c10a_steps_does_not_set_whoami_src_registry_key(tmp_path, monkeypatch):
    """Step C10a-2's `coordinator.whoami_src` registry write is retired: a
    fake `machine-local` CLI that records every argv it is invoked with
    must never see a `set coordinator.whoami_src ...` call."""
    install_base, settings_home_path, plugin_root, bin_dst = _make_dirs(tmp_path)

    calls_log = tmp_path / "ml-calls.log"
    ml_cli = bin_dst / "machine-local"
    ml_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(calls_log)!r}).open('a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "print('')\n",
        encoding="utf-8",
    )
    ml_cli.chmod(0o755)

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    logged = calls_log.read_text(encoding="utf-8") if calls_log.is_file() else ""
    assert "whoami_src" not in logged, (
        f"C10a-2 is retired: machine-local must never be invoked with whoami_src (log: {logged!r})"
    )


def test_c10a_steps_check_only_reports_no_whoami_relocation(tmp_path, capsys):
    """`--check-only` (dry-run) must not print a whoami relocation or
    registry-key intent — the install dry-run's own acceptance criterion
    for this item."""
    install_base, settings_home_path, plugin_root, bin_dst = _make_dirs(tmp_path)

    legacy_whoami = install_base / ".claude" / "coordinator-whoami"
    legacy_whoami.mkdir(parents=True)
    (legacy_whoami / "marker").write_text("real dir", encoding="utf-8")

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "whoami_src" not in out
    assert "relocate coordinator-whoami" not in out


def test_c10a_steps_venv_rebuild_still_reachable_without_relocation(tmp_path, monkeypatch):
    """C10a-3 (venv rebuild) must remain reachable via `allow_venv_fallback`
    even though C10a-1 no longer populates the settings-home whoami
    destination — `has_viable_whoami` still resolves via
    `plugin_root / "whoami"` directly."""
    install_base, settings_home_path, plugin_root, bin_dst = _make_dirs(tmp_path)

    plugin_whoami = plugin_root / "whoami"
    plugin_whoami.mkdir(parents=True)
    (plugin_whoami / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    from coordinator_core.install import substrate as substrate_mod

    def _fake_ensure_coordinator_venv(*_args, **kwargs):
        return "would-rebuild" if kwargs.get("check_only") else "rebuilt"

    monkeypatch.setattr(
        "coordinator_core.install.ensure_venv.ensure_coordinator_venv",
        _fake_ensure_coordinator_venv,
    )
    monkeypatch.setattr(
        "coordinator_core.install.ensure_venv._venv_healthy", lambda _venv_py: True,
    )

    rc = _c10a_steps(
        str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False,
        allow_venv_fallback=True,
    )

    assert rc == 0
