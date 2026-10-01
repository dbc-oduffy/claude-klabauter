
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import data_root as dr_mod
from coordinator_core.win_portability import no_console_creationflags

_BIN_LIB_DIR = Path(__file__).resolve().parent.parent / "coordinator" / "bin" / "lib"


def test_colocated_rung_wins_when_dir_present(tmp_path, monkeypatch):
    (tmp_path / "schemas").mkdir()
    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: tmp_path)
    monkeypatch.setattr(
        dr_mod,
        "coordinator_content_root",
        lambda: (_ for _ in ()).throw(AssertionError("rung 2 should not run")),
    )
    result = dr_mod.data_root("schemas")
    assert result == tmp_path / "schemas"


def test_doe_resident_rung_used_when_colocated_missing(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()
    content_root = tmp_path / "doe"
    (content_root / "coordinator" / "schemas").mkdir(parents=True)

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_content_root", lambda: str(content_root))

    result = dr_mod.data_root("schemas")
    assert result == content_root / "coordinator" / "schemas"


def test_raises_when_content_root_unresolved(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_content_root", lambda: None)

    with pytest.raises(RuntimeError, match="cannot resolve data dir 'schemas'"):
        dr_mod.data_root("schemas")


def test_raises_when_content_root_resolved_but_dir_missing(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()
    content_root = tmp_path / "doe"
    content_root.mkdir()

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_content_root", lambda: str(content_root))

    with pytest.raises(RuntimeError, match="cannot resolve data dir 'schemas'"):
        dr_mod.data_root("schemas")


def test_f2_oss_flat_layout_fallback_when_private_join_absent(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()
    content_root = tmp_path / "flat-content-root"
    (content_root / "schemas").mkdir(parents=True)

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_content_root", lambda: str(content_root))

    result = dr_mod.data_root("schemas")
    assert result == content_root / "schemas"


def test_f2_private_layout_still_wins_when_both_would_resolve(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()
    content_root = tmp_path / "both-content-root"
    (content_root / "coordinator" / "schemas").mkdir(parents=True)
    (content_root / "schemas").mkdir(parents=True)

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_content_root", lambda: str(content_root))

    result = dr_mod.data_root("schemas")
    assert result == content_root / "coordinator" / "schemas"


def test_colocated_root_points_at_coordinator_dir():
    resolved = dr_mod._colocated_root()
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / "coordinator_core").is_dir()
    assert resolved == repo_root / "coordinator"


def test_codename_free_ladder_reaches_both_twins_via_real_delegation(tmp_path, monkeypatch) -> None:
    if str(_BIN_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_BIN_LIB_DIR))
    import coordinator_registry  # noqa: PLC0415 (bin/lib sibling)
    from coordinator_core.ops import coordinator_content_root as content_root_mod

    colocated_core_miss = tmp_path / "core-miss"
    colocated_core_miss.mkdir()
    colocated_bin_miss = tmp_path / "bin-miss"
    colocated_bin_miss.mkdir()

    empty_claude_home = tmp_path / "isolated-claude-home"
    empty_claude_home.mkdir()
    empty_settings_home = tmp_path / "isolated-settings-home"
    empty_settings_home.mkdir()

    plugin_root = tmp_path / "plugin-root"
    (plugin_root / "coordinator" / "schemas").mkdir(parents=True)
    (plugin_root / "coordinator" / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (plugin_root / "coordinator" / "snippets").mkdir(parents=True)

    # coordinator_content_root() is NOT stubbed — it IS the C1B ladder this test
    # proves engine-side gets "for free" via delegation (per C2's finding:
    # this module needs no ladder of its own). Its rung-1 env override
    # (REPO_CONTENT_ROOT) is cleared so it does not short-circuit ahead of the
    # ladder this test targets.
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    monkeypatch.delenv("CONTENT_ROOT", raising=False)
    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_core_miss)

    if str(_BIN_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_BIN_LIB_DIR))
    import coordinator_data_root as cdr_mod  # noqa: PLC0415

    monkeypatch.setattr(cdr_mod, "_colocated_root", lambda: colocated_bin_miss)
    monkeypatch.setattr(
        coordinator_registry,
        "content_root",
        lambda: (_ for _ in ()).throw(AssertionError("bin rung 2 should not run — C2 ladder must win")),
    )

    monkeypatch.setenv("CLAUDE_HOME", str(empty_claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_settings_home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    content_root_mod._reset_content_root_cache()
    try:
        core_result = dr_mod.data_root("snippets")
        bin_result = cdr_mod.data_root("snippets")
    finally:
        content_root_mod._reset_content_root_cache()

    expected = plugin_root / "coordinator" / "snippets"
    assert core_result == expected
    assert bin_result == expected
    assert core_result == bin_result


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_module_imports_standalone_in_an_oss_shaped_hermetic_subprocess(tmp_path) -> None:
    """The publish pre-swap FUNCTION gate imports this file as a FLAT top-level
    `data_root` module with only the staging dir on `PYTHONPATH` — no
    `coordinator_core` package to import through, by construction (that hermetic
    shape is the point of the gate: `coordinator/bin/publish.py ::
    _function_gate_modules_and_search_paths_for_repo_root` strips the
    `coordinator_core` prefix for a row staged at its own root).

    Regression, 2026-08-21: a module-level `from coordinator_core.ops.
    coordinator_content_root import coordinator_content_root` failed that gate with
    `ModuleNotFoundError: No module named 'coordinator_core'`, so the ENGINE row
    of the claude-klabauter target never published while the other eight landed —
    the mirror silently lagged the source tree fleet-wide
    (`state/bug-backlog/2026-08-21-the-engine-row-cannot-publish-data-root-b0706ca7fc0d.yaml`).
    This asserts the gate's own import, not a proxy for it.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    shutil.copy2(Path(dr_mod.__file__), staging / "data_root.py")

    proc = subprocess.run(
        [sys.executable, "-c", "import importlib; importlib.import_module('data_root'); print('GATE_OK')"],
        cwd=str(staging),
        env={
            **{k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "COMSPEC")},
            "PYTHONPATH": str(staging),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, f"hermetic import failed:\n{proc.stderr}"
    assert "GATE_OK" in proc.stdout


def test_deferred_resolver_still_honours_a_monkeypatched_module_attribute(tmp_path, monkeypatch) -> None:
    colocated_base = tmp_path / "colocated-miss"
    content_root = tmp_path / "doe"
    (content_root / "coordinator" / "schemas").mkdir(parents=True)

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_content_root", lambda: str(content_root))

    assert dr_mod._resolve_content_root() == str(content_root)
    assert dr_mod.data_root("schemas") == content_root / "coordinator" / "schemas"
