"""test_coordinator_data_root — unit coverage for the shared coordinator
data-dir resolver (`coordinator/bin/lib/coordinator_data_root.py`).

Covers all resolution rungs plus the fail-loud path:
  - co-located hit (rung 1, the dir sits beside bin/ under the same root)
  - codename-free ladder (rung 1.5, C2) — exercised for real (unstubbed) via
    env redirection, since it is the additive rung `coordinator_registry.
    doe_root()` itself does not carry
  - DoE-resident fallback (rung 2, delegates to coordinator_registry.doe_root())
  - neither rung resolves -> RuntimeError naming the dir and both rungs tried
  - import-time purity: importing this module under a stripped environment
    must succeed with zero subprocess/env dependency (rung 1 only)

Rung-1 (`_colocated_root`) is exercised by monkeypatching that function
directly on the imported module — a genuine module-level global, LOAD_GLOBAL-
resolved at call time, so the monkeypatch is visible to `data_root()`.

Rung-1.5 tests that want to isolate rung 2 (`doe_root`) neutralize
`cdr._cdr_codename_free_root` directly — on this dev machine the REAL
`.doe-root` pointer file is present and would otherwise win rung 1.5 ahead of
the rung-2 mock, since rung 1.5 runs first in `data_root()`.

Rung-2 (`doe_root` / `_DoeUnresolvable`) is exercised by monkeypatching
`coordinator_registry` itself, NOT `cdr` — `coordinator_data_root.data_root()`
imports `coordinator_registry.doe_root` LAZILY, inside the function body
(see that module's "Import-time purity" negative-spec), so the names are
resolved from the live `coordinator_registry` module at call time, not from
`coordinator_data_root`'s own module namespace. Patching `cdr.doe_root`
would silently no-op post-fix (AttributeError, in fact — the name no longer
lives there at all).

Spec backlink: docs/plans/2026-07-22-executable-surface-migration-data-root-fix.md [DEAD-CITATION: plan file never committed to this repo]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parents[1] / "bin" / "lib"
sys.path.insert(0, str(LIB_DIR))
import coordinator_data_root as cdr  # noqa: E402
import coordinator_registry  # noqa: E402
from coordinator_registry import _DoeUnresolvable  # noqa: E402

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_data_root_colocated_hit(tmp_path, monkeypatch) -> None:
    coordinator_root = tmp_path / "coordinator"
    (coordinator_root / "snippets").mkdir(parents=True)

    monkeypatch.setattr(cdr, "_colocated_root", lambda: coordinator_root)
    # Rung 2 must not even be consulted on a rung-1 hit; make it explode if it is.
    monkeypatch.setattr(
        coordinator_registry,
        "doe_root",
        lambda: (_ for _ in ()).throw(AssertionError("rung 2 should not run")),
    )

    resolved = cdr.data_root("snippets")
    assert resolved == coordinator_root / "snippets"
    assert resolved.is_dir()


def test_data_root_doe_resident_fallback(tmp_path, monkeypatch) -> None:
    colocated_miss = tmp_path / "claude-klabauter-coordinator"  # exists, but no snippets/ inside
    colocated_miss.mkdir()
    doe_root_dir = tmp_path / "DoE-claude"
    (doe_root_dir / "coordinator" / "snippets").mkdir(parents=True)

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(cdr, "_cdr_codename_free_root", lambda: "")
    monkeypatch.setattr(coordinator_registry, "doe_root", lambda: str(doe_root_dir))

    resolved = cdr.data_root("snippets")
    assert resolved == doe_root_dir / "coordinator" / "snippets"
    assert resolved.is_dir()


def test_data_root_fail_loud_when_neither_rung_resolves(tmp_path, monkeypatch) -> None:
    colocated_miss = tmp_path / "claude-klabauter-coordinator"
    colocated_miss.mkdir()

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(cdr, "_cdr_codename_free_root", lambda: "")
    monkeypatch.setattr(
        coordinator_registry,
        "doe_root",
        lambda: (_ for _ in ()).throw(_DoeUnresolvable("no registry entry")),
    )

    with pytest.raises(RuntimeError) as exc_info:
        cdr.data_root("snippets")

    message = str(exc_info.value)
    assert "snippets" in message
    assert "Rung 1" in message
    assert "Rung 2" in message


def test_data_root_fail_loud_doe_candidate_missing(tmp_path, monkeypatch) -> None:
    colocated_miss = tmp_path / "claude-klabauter-coordinator"
    colocated_miss.mkdir()
    doe_root_dir = tmp_path / "DoE-claude"
    doe_root_dir.mkdir()  # exists, but no coordinator/snippets/ inside

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(cdr, "_cdr_codename_free_root", lambda: "")
    monkeypatch.setattr(coordinator_registry, "doe_root", lambda: str(doe_root_dir))

    with pytest.raises(RuntimeError) as exc_info:
        cdr.data_root("snippets")

    message = str(exc_info.value)
    assert "snippets" in message
    assert str(doe_root_dir / "coordinator" / "snippets") in message


def test_f2_oss_flat_layout_fallback_when_private_join_absent(tmp_path, monkeypatch) -> None:
    """F2 regression (2026-08-08, hermetic-ac-reverify) -- when rung 2
    (`coordinator_registry.doe_root()`) resolves an OSS-flat root (`schemas/`
    directly under the root, no `coordinator/` segment -- e.g. a real
    marketplace-cache install), the terminal join must NOT unconditionally
    insert `coordinator/`. Must stay behaviourally identical to
    `coordinator_core/data_root.py`'s own fix (AC4)."""
    colocated_miss = tmp_path / "claude-klabauter-coordinator"
    colocated_miss.mkdir()
    doe_root_dir = tmp_path / "flat-doe-root"
    (doe_root_dir / "snippets").mkdir(parents=True)  # OSS-flat: no coordinator/ prefix

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(cdr, "_cdr_codename_free_root", lambda: "")
    monkeypatch.setattr(coordinator_registry, "doe_root", lambda: str(doe_root_dir))

    resolved = cdr.data_root("snippets")
    assert resolved == doe_root_dir / "snippets"


def test_f2_private_layout_still_wins_when_both_would_resolve(tmp_path, monkeypatch) -> None:
    """F2 regression: the private-layout join must still be tried FIRST --
    unchanged default behaviour for every existing caller/test resolving a
    private-layout root."""
    colocated_miss = tmp_path / "claude-klabauter-coordinator"
    colocated_miss.mkdir()
    doe_root_dir = tmp_path / "both-doe-root"
    (doe_root_dir / "coordinator" / "snippets").mkdir(parents=True)
    (doe_root_dir / "snippets").mkdir(parents=True)

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(cdr, "_cdr_codename_free_root", lambda: "")
    monkeypatch.setattr(coordinator_registry, "doe_root", lambda: str(doe_root_dir))

    resolved = cdr.data_root("snippets")
    assert resolved == doe_root_dir / "coordinator" / "snippets"


def test_f6_marketplace_cache_rung_claude_home_matches_registry_twin(tmp_path, monkeypatch) -> None:
    """F6 regression (2026-08-08, hermetic-ac-reverify) -- with `CLAUDE_HOME`
    set, `_cdr_marketplace_cache_rung()` must probe the SAME directory
    `coordinator_registry.py::_mp_marketplace_cache_rung()` does (`claude_home()`
    returns `CLAUDE_HOME` as-is, not `<CLAUDE_HOME>/.claude`). Before the fix,
    this module inlined a `CLAUDE_HOME or HOME or USERPROFILE` ladder and then
    always joined `.claude`, so with `CLAUDE_HOME` set the two probed
    different directories."""
    claude_home_dir = tmp_path / "f6-claude-home-set-directly"
    version_dir = claude_home_dir / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "4.0.0"
    (version_dir / "schemas").mkdir(parents=True)
    (version_dir / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home_dir))
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)

    result = cdr._cdr_marketplace_cache_rung()
    registry_result = coordinator_registry._mp_marketplace_cache_rung()

    assert result == str(version_dir)
    assert result == registry_result


def test_f6_flat_layout_rung_claude_home_matches_registry_twin(tmp_path, monkeypatch) -> None:
    """F6 regression: same convergence for `_cdr_flat_layout_probe_rung()` /
    `_mp_flat_layout_probe_rung()`."""
    claude_home_dir = tmp_path / "f6-flat-claude-home-set-directly"
    flat_root = claude_home_dir / "plugins" / "coordinator-claude"
    (flat_root / ".claude-plugin").mkdir(parents=True)
    (flat_root / ".claude-plugin" / "plugin.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home_dir))
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)

    result = cdr._cdr_flat_layout_probe_rung()
    registry_result = coordinator_registry._mp_flat_layout_probe_rung()

    assert result == str(flat_root)
    assert result == registry_result


def test_codename_free_ladder_wins_before_registry_rung_real_delegation(tmp_path, monkeypatch) -> None:
    """Rung 1.5, exercised for real (no stubbing of the ladder itself) — only
    env vars are redirected to a temp layout, matching the "environment
    redirection to a temp dir only" constraint. Proves `data_root()` resolves
    via the codename-free ladder WITHOUT ever calling `coordinator_registry.
    doe_root()`, which is left unmocked and made to explode if reached.
    """
    colocated_miss = tmp_path / "claude-klabauter-coordinator"
    colocated_miss.mkdir()

    empty_claude_home = tmp_path / "isolated-claude-home"
    empty_claude_home.mkdir()
    empty_settings_home = tmp_path / "isolated-settings-home"
    empty_settings_home.mkdir()

    plugin_root = tmp_path / "plugin-root"
    (plugin_root / "coordinator" / "schemas").mkdir(parents=True)
    (plugin_root / "coordinator" / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (plugin_root / "coordinator" / "snippets").mkdir(parents=True)

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(
        coordinator_registry,
        "doe_root",
        lambda: (_ for _ in ()).throw(AssertionError("rung 2 should not run — rung 1.5 must win")),
    )
    monkeypatch.setenv("CLAUDE_HOME", str(empty_claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_settings_home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    resolved = cdr.data_root("snippets")
    assert resolved == plugin_root / "coordinator" / "snippets"
    assert resolved.is_dir()


def test_c1e_plugin_root_content_root_normalized_to_repo_root(tmp_path, monkeypatch) -> None:
    """C1E — regression: CLAUDE_PLUGIN_ROOT is a CONTENT root in the
    private/dev layout (`<repo_root>/coordinator`), one level below the repo
    root `data_root()` needs (it appends "coordinator" / dir_name itself).
    Before the C1E fix, `_cdr_manifest_present` accepted the content root
    unconverted (satisfies the OSS-flat relpath gate at
    `<content_root>/schemas/...`), so `data_root()` built
    `<repo_root>/coordinator/coordinator/<dir_name>` -- a path that exists
    nowhere -- instead of the real `<repo_root>/coordinator/<dir_name>`.
    """
    colocated_miss = tmp_path / "claude-klabauter-coordinator"
    colocated_miss.mkdir()

    empty_claude_home = tmp_path / "c1e-isolated-claude-home"
    empty_claude_home.mkdir()
    empty_settings_home = tmp_path / "c1e-isolated-settings-home"
    empty_settings_home.mkdir()

    repo_root = tmp_path / "c1e-doe-repo"
    content_root = repo_root / "coordinator"
    (content_root / "schemas").mkdir(parents=True)
    (content_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (content_root / "snippets").mkdir(parents=True)
    (repo_root / ".claude-plugin").mkdir(parents=True)
    (repo_root / ".claude-plugin" / "plugin.json").write_text("{}")

    monkeypatch.setattr(cdr, "_colocated_root", lambda: colocated_miss)
    monkeypatch.setattr(
        coordinator_registry,
        "doe_root",
        lambda: (_ for _ in ()).throw(AssertionError("rung 2 should not run — rung 1.5 must win")),
    )
    monkeypatch.setenv("CLAUDE_HOME", str(empty_claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_settings_home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(content_root))

    resolved = cdr.data_root("snippets")
    assert resolved == content_root / "snippets"
    assert resolved.is_dir()


def test_import_is_pure_under_stripped_environment() -> None:
    """Importing coordinator_data_root must succeed with zero subprocess/env
    dependency — it must not eagerly import `coordinator_registry` (which
    eagerly resolves its manifest via a `machine-local` subprocess needing
    `HOME`) at module top level. A subprocess with `env={}` genuinely
    exercises a stripped environment (no HOME, no PATH, nothing) rather than
    mocking anything that would pass regardless of the fix.
    """
    probe = (
        "import sys; "
        f"sys.path.insert(0, {str(LIB_DIR)!r}); "
        "import coordinator_data_root; "
        "print('import OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env={},
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"import failed under stripped environment.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "import OK" in result.stdout
