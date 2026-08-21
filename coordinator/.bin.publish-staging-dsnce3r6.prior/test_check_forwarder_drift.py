"""
test_check_forwarder_drift.py — coverage for check-forwarder-drift.py's
CONTENT axis (AC5-AC7 of docs/plans/2026-08-12-auto-arm-the-dual-boot-for-
Claude-klabauter-instal.md § C2).

The NAME axis (forwarder derived-count vs installed-count) already has full
coverage in coordinator_core/plugin_health/tests/test_forwarder_drift.py —
this file covers only the axis that module does NOT have: whether the BYTES
of a path-loaded lib installed into settings-home/bin (`_resolve_claude_klabauter.py`)
match the source-of-truth copy under the engine checkout root's
`coordinator/lib/` source tree.

AC6 (load-bearing): `test_name_axis_ok_content_axis_drifts_on_stale_installed_lib`
reproduces the exact live-incident shape this plan's Problem section
describes — a stale installed `_resolve_claude_klabauter.py` that the NAME axis
reports as `[ok]` (it only counts forwarder names, and `_resolve_claude_klabauter.py`
is not itself a forwarder) while the CONTENT axis must report drift. This
test was RED before this chunk landed (`_check_content_axis` did not exist —
`AttributeError: module ... has no attribute '_check_content_axis'`).

Runs under pytest via the `coordinator/bin` testpaths admit (pyproject.toml).

Spec backlink: pln-auto-arm-the-dual-boot-for-mak-a41c72
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module():
    """Import check-forwarder-drift.py as a fresh module object each call —
    same pattern as this directory's other dash-named-script test files
    (e.g. test_sweep_terminal_plans.py's `_load_module`)."""
    path = os.path.join(SCRIPT_DIR, "check-forwarder-drift.py")
    spec = importlib.util.spec_from_file_location("check_forwarder_drift_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeFdModule:
    """Stand-in for the plugin_health forwarder-drift op module — supplies
    only the pieces `_check_content_axis` actually reads off it
    (`_PROG`, `_REMEDY`, `_resolve_settings_bin`), so this test never needs a
    real checkout or settings-home."""

    _PROG = "forwarder-drift"
    _REMEDY = "run `python3 -m coordinator_core.install.substrate` (fake remedy for test)"

    def __init__(self, settings_bin: Path):
        self._settings_bin = settings_bin

    def _resolve_settings_bin(self) -> Path:
        return self._settings_bin


_LIB_NAME = "_resolve_claude_klabauter.py"


def _write_lib(root: Path, contents: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dst = root / _LIB_NAME
    dst.write_text(contents)
    return dst


def test_name_axis_ok_content_axis_drifts_on_stale_installed_lib(tmp_path: Path):
    """AC6 — the load-bearing case. A settings-home/bin whose installed
    `_resolve_claude_klabauter.py` is an OLDER copy than the engine-checkout source:
    the NAME axis (a plain filename/count comparison, simulated directly
    here since it never inspects `_resolve_claude_klabauter.py`'s bytes) reports
    parity, while the CONTENT axis (`_check_content_axis`) must report
    drift and name the file."""
    mod = _load_module()

    checkout_root = tmp_path / "engine-checkout"
    source_dir = checkout_root / "coordinator" / "lib" / "resolve-claude-klabauter"
    _write_lib(source_dir, "# current source, 500 lines of two-tier gate\n")

    settings_bin = tmp_path / "settings-home" / "bin"
    _write_lib(settings_bin, "# stale installed copy, 486 lines behind\n")

    # NAME axis: a stale-but-present installed file is invisible to a count
    # comparison — same "364 derived == 364 installed" shape the plan's
    # Problem section cites as the live-incident false-green.
    # sanity: source dir resolved correctly
    assert source_dir.name == "resolve-claude-klabauter"
    name_axis_parity = True  # the file exists on both sides; the name axis never opens it
    assert name_axis_parity is True

    fd_module = _FakeFdModule(settings_bin)
    lines, any_drift = mod._check_content_axis(fd_module, str(checkout_root))

    assert any_drift is True
    joined = "\n".join(lines)
    assert "[warn]" in joined
    assert _LIB_NAME in joined
    assert fd_module._REMEDY in joined


def test_content_axis_clean_when_installed_matches_source(tmp_path: Path):
    mod = _load_module()

    checkout_root = tmp_path / "engine-checkout"
    source_dir = checkout_root / "coordinator" / "lib" / "resolve-claude-klabauter"
    _write_lib(source_dir, "# identical content\n")

    settings_bin = tmp_path / "settings-home" / "bin"
    _write_lib(settings_bin, "# identical content\n")

    fd_module = _FakeFdModule(settings_bin)
    lines, any_drift = mod._check_content_axis(fd_module, str(checkout_root))

    assert any_drift is False
    joined = "\n".join(lines)
    assert "[ok]" in joined
    assert "[warn]" not in joined


def test_content_axis_skips_when_settings_bin_missing(tmp_path: Path):
    mod = _load_module()
    checkout_root = tmp_path / "engine-checkout"
    settings_bin = tmp_path / "settings-home" / "bin"  # never created

    fd_module = _FakeFdModule(settings_bin)
    lines, any_drift = mod._check_content_axis(fd_module, str(checkout_root))

    assert any_drift is False
    assert any("[skip]" in line for line in lines)


def test_content_axis_lib_names_includes_resolve_claude_klabauter_file():
    mod = _load_module()
    names = mod._content_axis_lib_names()
    assert "_resolve_claude_klabauter.py" in names


def test_content_axis_lib_names_falls_back_when_substrate_unimportable(monkeypatch):
    # Review: code-reviewer 2026-08-12 (P2): the `except ImportError` branch
    # of `_content_axis_lib_names` had no test driving it for real — every
    # other test only exercises the success path (substrate imports fine on
    # this checkout). This drives the actual fallback by making the import
    # fail, rather than faking a downstream consumer.
    mod = _load_module()
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "coordinator_core.install.substrate":
            raise ImportError("simulated: substrate.py unreachable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    names = mod._content_axis_lib_names()

    assert names == ("_resolve_claude_klabauter.py",)


def test_content_axis_unresolved_source_reported_and_not_counted_clean(tmp_path: Path):
    # Review: code-reviewer 2026-08-12 (nit): a source file that cannot be
    # found is reported [warn] but must not be silently folded into a
    # "0 drifted" summary that reads as full clean coverage.
    mod = _load_module()

    checkout_root = tmp_path / "engine-checkout"
    # No source_dir created at all -- src.exists() is False for the installed file.

    settings_bin = tmp_path / "settings-home" / "bin"
    _write_lib(settings_bin, "# installed copy, no source-of-truth to compare against\n")

    fd_module = _FakeFdModule(settings_bin)
    lines, any_drift = mod._check_content_axis(fd_module, str(checkout_root))

    assert any_drift is False
    joined = "\n".join(lines)
    assert "[warn]" in joined
    assert "could not resolve source-of-truth" in joined
    ok_lines = [line for line in lines if line.startswith("[ok]")]
    assert len(ok_lines) == 1
    assert "0 checked" in ok_lines[0]
    assert "could not be compared" in ok_lines[0]
