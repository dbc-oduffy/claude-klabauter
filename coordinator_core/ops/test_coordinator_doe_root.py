"""
Tests for coordinator_core.ops.coordinator_doe_root.

Mirrors the bash oracle's own test coverage (T1-T5) plus a rung-2.5-specific
case (T6) the bash suite exercises only indirectly via T3's fallthrough.
Scenarios are driven the same way the bash tests drive them: a fake
`machine-local` stub placed first on PATH, environment variables scoped per-test via
monkeypatch, and a fresh `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"]` state.

Port of: coordinator-doe-root.test.sh (example-doctrine-repo 09e5e5f9, 2026-07-19)

NOTE (2026-07-21): the module is no longer process-global-state-bearing via
`os.environ` — the bash oracle's `export` was retired because it leaked across the
shared pytest interpreter (see the module's own docstring § DECISION REVERSAL). The
same-process re-resolution guard is now an explicit module-scope memo, which
`_clean_env` below resets per-test. T2/T5/T6 assert the ABSENCE of the export.
"""

from __future__ import annotations

import os
import stat
import textwrap

import pytest

from coordinator_core.ops import coordinator_doe_root as mod
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def _write_stub(path, python_body: str) -> str:
    """Write a fake `machine-local` CLI at `path`, resolved via `shutil.which` in
    `coordinator_doe_root.py` -- see `coordinator_core.testing.fake_machine_local`
    for the Windows PATHEXT/exec rationale. `python_body` is Python source (reads
    `sys.argv[1:]`), not a shell script -- callers below were ported from bash
    stub bodies to Python bodies for cross-platform execution."""
    return str(write_fake_machine_local(path, python_body))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Reset the module-scope resolution memo before AND after each test: it is
    # interpreter-lifetime state (correct under spawn-per-call, shared under pytest),
    # so a value pinned by one test would otherwise leak into the next. Mirrors the
    # _reset_central_root_memo fixture in test_deliverable_rollup.py.
    mod._reset_doe_root_cache()
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    yield
    mod._reset_doe_root_cache()


def test_t1_env_short_circuit_no_machine_local_call(tmp_path, monkeypatch):
    stubdir = tmp_path / "t1-stub"
    stubdir.mkdir()
    sentinel = tmp_path / "t1-called"
    _write_stub(
        str(stubdir),
        "import pathlib\n"
        f"pathlib.Path({str(sentinel)!r}).touch()\n"
        "print('/should-not-be-returned')\n",
    )
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "/tmp/fake-doe-root")
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == "/tmp/fake-doe-root"
    assert not sentinel.exists()


def test_t2_registry_resolution(tmp_path, monkeypatch):
    stubdir = tmp_path / "t2-stub"
    stubdir.mkdir()
    expected = "/x/example-doctrine-repo"
    _write_stub(str(stubdir), f"print({expected!r})\n")
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == expected
    # Inverted 2026-07-21: the resolver is pure and no longer exports on rung 2.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_t3_fail_loud_returns_none_and_remediation(tmp_path, monkeypatch, capsys):
    stubdir = tmp_path / "t3-stub"
    stubdir.mkdir()
    _write_stub(str(stubdir), "import sys\nsys.exit(1)\n")
    fake_home = tmp_path / "t3-empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()
    assert result is None

    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "repos.example_doctrine_repo" in captured.err


def test_t4_memo_idempotency_second_call_skips_machine_local(tmp_path, monkeypatch):
    """Renamed from `test_t4_export_idempotency_...` (2026-07-21): the
    single-machine-local-call property is now carried by the module-scope memo
    rather than by the retired `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"]` export. The
    assertion is unchanged -- only the mechanism under it moved."""
    stubdir = tmp_path / "t4-stub"
    stubdir.mkdir()
    sentinel = tmp_path / "t4-call-count"
    _write_stub(
        str(stubdir),
        "with open(" + repr(str(sentinel)) + ", 'a') as _f:\n"
        "    _f.write('called\\n')\n"
        "print('/x/example-doctrine-repo')\n",
    )
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    first = mod.coordinator_doe_root()
    second = mod.coordinator_doe_root()

    assert first == "/x/example-doctrine-repo"
    assert second == "/x/example-doctrine-repo"
    # B1 review fix (2026-08-08): the stub resolves rung 2 (repos.example_doctrine_repo)
    # immediately, so neither the codename-free ladder (now rung 2.75, tried
    # only when rungs 2/2.5 both fail) nor rung 2.5 itself is ever reached on
    # this stub. The property this test guards -- exactly one machine-local
    # call per outer resolution, and zero further calls on the memoized
    # second call -- now holds with the count it always should have had: 1.
    assert sentinel.read_text().count("called\n") == 1


def test_t5_rung3_pointer_file_fallback_via_clone_root_script(tmp_path, monkeypatch):
    """C11 (2026-07-21): rung 3 (`_resolve_via_clone_root_script`) now calls the
    native `coordinator_core.resolve_coordinator_clone.resolve_clone_root()` port
    in-process instead of shelling to a fake `resolve-coordinator-clone.sh`. The
    on-disk fake script below is left in place but is NOT invoked -- the native
    resolver's own `.doe-root`-pointer-plus-`.git` rung resolves directly to
    `fake_doe_root`, exercising the same rung-3 fallback path this test was
    originally written to characterize, just through the ported module rather
    than a subprocess.
    """
    stubdir = tmp_path / "t5-stub"
    stubdir.mkdir()
    _write_stub(str(stubdir), "import sys\nsys.exit(1)\n")

    fake_home = tmp_path / "t5-fake-home"
    fake_doe_root = tmp_path / "t5-fake-doe-root"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_doe_root / "coordinator" / "lib").mkdir(parents=True)
    (fake_doe_root / ".git").mkdir()
    (fake_home / ".claude" / ".doe-root").write_text(str(fake_doe_root))

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == str(fake_doe_root)
    # The resolver is PURE as of 2026-07-21 -- it no longer exports REPO_EXAMPLE_DOCTRINE_REPO
    # (see the module docstring's DECISION REVERSAL section). This assertion was
    # inverted from `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"] == str(fake_doe_root)`: it now
    # pins the absence of the interpreter-global write, which is the property that
    # actually matters to every other test in the suite.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_t6_rung25_live_path_fallback(tmp_path, monkeypatch):
    """Rung 2 (repos.example_doctrine_repo) empty, rung 2.5 (live_path) resolves."""
    stubdir = tmp_path / "t6-stub"
    stubdir.mkdir()
    _write_stub(
        str(stubdir),
        textwrap.dedent(
            """\
            import sys
            argv2 = sys.argv[2] if len(sys.argv) > 2 else None
            if argv2 == "repos.example_doctrine_repo":
                sys.exit(1)
            if argv2 == "plugin.mirrors.coordinator-claude.live_path":
                print("/x/live-path-doe")
                sys.exit(0)
            sys.exit(1)
            """
        ),
    )
    fake_home = tmp_path / "t6-empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{stubdir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = mod.coordinator_doe_root()

    assert result == "/x/live-path-doe"
    # Inverted 2026-07-21: the resolver is pure and no longer exports on rung 2.5.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_negative_no_machine_local_no_pointer_file(tmp_path, monkeypatch):
    """Negative corpus: machine-local absent from PATH entirely, no pointer file,
    no COORDINATOR_ROOT/CLAUDE_PLUGIN_ROOT override -- must fail loud (None), never
    raise."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "neg-empty-home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result is None

    rc = mod.main([])
    assert rc == 1


def test_c1b_codename_free_rung_resolves_with_registry_unreachable(tmp_path, monkeypatch):
    """C1B — proves the new codename-free ladder is load-bearing, not merely
    present: with the registry unreachable (empty PATH, no `machine-local`
    binary at all) and REPO_EXAMPLE_DOCTRINE_REPO unset, a planted `.doe-root` pointer
    under a redirected settings-home resolves the OSS-flat manifest layout.
    Mirrors the C1B brief's executed probe (published mirror, registry
    reachability removed -> previously `None`)."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "c1b-fake-home"
    fake_settings_home = tmp_path / "c1b-fake-settings-home"
    fake_plugin_root = tmp_path / "c1b-fake-plugin-root"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_settings_home / "machine-local").mkdir(parents=True)
    (fake_plugin_root / "schemas").mkdir(parents=True)
    (fake_plugin_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (fake_settings_home / "machine-local" / ".doe-root").write_text(str(fake_plugin_root))

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(fake_settings_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(fake_plugin_root)
    assert "REPO_EXAMPLE_DOCTRINE_REPO" not in os.environ


def test_c1b_codename_free_rung_private_manifest_layout(tmp_path, monkeypatch):
    """Same rung, private example-doctrine-repo-repo manifest shape (`coordinator/schemas/...`)
    instead of the OSS-flat one -- both published layouts must be probed."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "c1b-priv-fake-home"
    fake_settings_home = tmp_path / "c1b-priv-fake-settings-home"
    fake_doe_root = tmp_path / "c1b-priv-fake-doe-root"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_settings_home / "machine-local").mkdir(parents=True)
    (fake_doe_root / "coordinator" / "schemas").mkdir(parents=True)
    (fake_doe_root / "coordinator" / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (fake_settings_home / "machine-local" / ".doe-root").write_text(str(fake_doe_root))

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(fake_settings_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(fake_doe_root)


def test_c1b_codename_free_rung_flat_layout_marker(tmp_path, monkeypatch):
    """Codename-free rung (b): flat `~/.claude/plugins/coordinator-claude`
    layout, gated on the `.claude-plugin/plugin.json` marker, resolves when
    no `.doe-root` pointer is present -- registry still unreachable."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "c1b-flat-fake-home"
    flat_root = fake_home / ".claude" / "plugins" / "coordinator-claude"
    (flat_root / ".claude-plugin").mkdir(parents=True)
    (flat_root / ".claude-plugin" / "plugin.json").write_text("{}")
    (flat_root / "schemas").mkdir()
    (flat_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(flat_root)


def test_c1b_codename_free_rung_gated_on_manifest_presence(tmp_path, monkeypatch):
    """A resolved, existing directory that lacks BOTH manifest layouts must
    NOT be accepted by the codename-free ladder -- falls through to the
    (dead, on this stripped fixture) existing chain and returns None,
    exactly like the pre-C1B empty-.doe-root case in
    test_negative_no_machine_local_no_pointer_file."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "c1b-nogate-fake-home"
    fake_settings_home = tmp_path / "c1b-nogate-fake-settings-home"
    no_manifest_root = tmp_path / "c1b-nogate-no-manifest-root"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_settings_home / "machine-local").mkdir(parents=True)
    no_manifest_root.mkdir()
    (fake_settings_home / "machine-local" / ".doe-root").write_text(str(no_manifest_root))

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(fake_settings_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result is None


def test_c1e_plugin_root_content_root_normalized_to_repo_root(tmp_path, monkeypatch):
    """C1E — regression: CLAUDE_PLUGIN_ROOT is a CONTENT root in the
    private/dev layout (`<repo_root>/coordinator`), one level below the repo
    root this ladder must return. Before the C1E fix, `_cf_manifest_present`
    accepted the content root unconverted (it also satisfies the
    manifest-present gate, via the OSS-flat relpath landing on
    `<content_root>/schemas/...`), and `coordinator_doe_root()` returned the
    wrong level. Registry unreachable, no `.doe-root` pointer, no flat
    layout -- only the CLAUDE_PLUGIN_ROOT rung is live."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "c1e-fake-home"
    (fake_home / ".claude").mkdir(parents=True)
    repo_root = tmp_path / "c1e-doe-repo"
    content_root = repo_root / "coordinator"
    (content_root / "schemas").mkdir(parents=True)
    (content_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")
    (repo_root / ".claude-plugin").mkdir(parents=True)
    (repo_root / ".claude-plugin" / "plugin.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(content_root))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(repo_root), (
        f"expected the REPO root {repo_root!r}, got {result!r} "
        "(the un-normalized content root -- C1E regression)"
    )


def test_b5_plugin_root_normalizes_without_marketplace_marker(tmp_path):
    """B5 review fix (2026-08-08): the private example-doctrine-repo repo root may not carry
    `.claude-plugin/plugin.json` (the C1E fix's marker was an unverified
    premise). With no marketplace marker ANYWHERE, a candidate whose basename
    is "coordinator" and whose parent has the manifest at
    `<parent>/coordinator/schemas/coordinator-registry.manifest.json` must
    still normalize to the parent (repo root), not fall through to the
    unnormalized content root."""
    repo_root = tmp_path / "b5-doe-repo"
    content_root = repo_root / "coordinator"
    (content_root / "schemas").mkdir(parents=True)
    (content_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    result = mod._cf_repo_root_from_plugin_root_candidate(str(content_root))

    assert result == str(repo_root)


def test_b7_plugin_root_candidate_case_insensitive_basename(tmp_path):
    """B7 review fix (2026-08-08): the "coordinator" basename comparison
    must be case-insensitive on Windows, where a registry value or env var
    can carry any casing that resolves to the same path."""
    repo_root = tmp_path / "b7-doe-repo"
    content_root = repo_root / "Coordinator"
    (repo_root / ".claude-plugin").mkdir(parents=True)
    (repo_root / ".claude-plugin" / "plugin.json").write_text("{}")
    content_root.mkdir()

    result = mod._cf_repo_root_from_plugin_root_candidate(str(content_root))

    assert result == str(repo_root)


def test_b7_plugin_root_candidate_bare_drive_root_not_truncated():
    """B7 review fix (2026-08-08): rstrip("/\\\\") must not silently turn a
    bare drive root into a drive-relative path.
    abs-path-ok: drive-root syntax fixture, not a hardcoded host path."""
    drive_root = "C:" + "\\"

    result = mod._cf_repo_root_from_plugin_root_candidate(drive_root)

    assert result == drive_root


def test_f1_marketplace_cache_rung_resolves_real_marketplace_layout(tmp_path, monkeypatch):
    """F1 regression (2026-08-08, hermetic-ac-reverify) -- Claude Code's REAL
    marketplace-install location (`<claude_home>/plugins/cache/coordinator-
    claude/coordinator/<version>/`) must resolve via `coordinator_doe_root()`
    the same way it already did for the two bin/-side twins
    (`coordinator_registry.py::_mp_marketplace_cache_rung`,
    `coordinator/bin/lib/coordinator_data_root.py::_cdr_marketplace_cache_rung`).
    Registry unreachable (empty PATH), no `.doe-root` pointer, no
    CLAUDE_PLUGIN_ROOT -- only the marketplace-cache rung is live."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "f1-mktcache-fake-home"
    version_dir = fake_home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "4.0.0"
    (version_dir / "schemas").mkdir(parents=True)
    (version_dir / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(version_dir)


def test_f1_marketplace_cache_rung_newest_version_wins(tmp_path, monkeypatch):
    """F1 regression: multiple installed versions under the cache -- the
    numerically-newest version dir must win (DR-148-safe compare), mirroring
    both bin/-side twins' own version-compare tests."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "f1-mktcache-multi-fake-home"
    cache_parent = fake_home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator"
    for version in ("1.2.0", "4.0.0", "3.9.9"):
        version_dir = cache_parent / version
        (version_dir / "schemas").mkdir(parents=True)
        (version_dir / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(cache_parent / "4.0.0")


def test_f1_marketplace_cache_rung_ordered_ahead_of_flat_layout(tmp_path, monkeypatch):
    """F1: the marketplace-cache rung must be tried BEFORE the flat-layout
    rung (matching both bin/-side twins' rung order: pointer -> marketplace
    cache -> flat layout -> plugin root -> live_path) so a box carrying both
    a real marketplace-cache install AND a flat dev-install clone prefers the
    real one."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    fake_home = tmp_path / "f1-order-fake-home"

    version_dir = fake_home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "4.0.0"
    (version_dir / "schemas").mkdir(parents=True)
    (version_dir / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    flat_root = fake_home / ".claude" / "plugins" / "coordinator-claude"
    (flat_root / ".claude-plugin").mkdir(parents=True)
    (flat_root / ".claude-plugin" / "plugin.json").write_text("{}")
    (flat_root / "schemas").mkdir()
    (flat_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_bin))

    result = mod.coordinator_doe_root()

    assert result == str(version_dir)


def test_main_cli_success_prints_no_trailing_newline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "/tmp/some-doe-root")
    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "/tmp/some-doe-root"


# ---------------------------------------------------------------------------
# Characterisation tests for repo_root_from_plugin_root_candidate(), the
# single-sourced helper (state/debt-backlog/2026-08-08-three-divergent-
# copies-of-the-plugin-roo-8d584d3b90d3.yaml). This module's own
# _cf_repo_root_from_plugin_root_candidate() is a thin default-args wrapper,
# already covered by the b5/b7 tests above. These tests pin the OTHER call
# shape -- coordinator_registry.py's historical parameters -- reproduced via
# the shared function directly, so a future edit cannot silently change
# either call site's behaviour without a test failing.
# ---------------------------------------------------------------------------


def test_shared_helper_normpath_guard_bare_drive_root_falls_through_unchanged():
    """drive_root_guard="normpath" (coordinator_registry.py's shape) and
    drive_root_guard="preserve" (this module's B7-fixed default) return the
    SAME thing for a bare drive-root-syntax candidate with no marketplace
    marker present -- the unmatched-fallback branch returns the original
    `candidate`, not either guard's internal (possibly truncated) working
    value. See the function's "KNOWN CROSS-COPY DIVERGENCE" docstring note
    for the latent, not-unit-testable-here case where they would differ.
    abs-path-ok: drive-root syntax fixture, not a hardcoded host path."""
    drive_root = "C:" + "\\"

    result = mod.repo_root_from_plugin_root_candidate(drive_root, drive_root_guard="normpath")

    assert result == drive_root


def test_shared_helper_casefold_basename_compare_is_case_insensitive_everywhere(tmp_path):
    """coordinator_registry.py's basename_compare="casefold" shape is
    case-insensitive regardless of platform, unlike this module's own
    normcase-based default."""
    repo_root = tmp_path / "doe-repo"
    content_root = repo_root / "COORDINATOR"
    content_root.mkdir(parents=True)
    (repo_root / ".claude-plugin").mkdir()
    (repo_root / ".claude-plugin" / "plugin.json").write_text("{}")

    result = mod.repo_root_from_plugin_root_candidate(str(content_root), basename_compare="casefold")

    assert result == str(repo_root)


def test_shared_helper_manifest_relpath_fallback_disabled_matches_registry_copy(tmp_path):
    """coordinator_registry.py's copy never carried the B5 manifest-relpath
    fallback -- manifest_relpath_fallback=False must fall through
    unnormalized even though the manifest-relpath shape is present."""
    repo_root = tmp_path / "doe-repo"
    content_root = repo_root / "coordinator"
    (content_root / "schemas").mkdir(parents=True)
    (content_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    result = mod.repo_root_from_plugin_root_candidate(str(content_root), manifest_relpath_fallback=False)

    assert result == str(content_root)


def test_shared_helper_allow_unchanged_fallback_false_returns_empty(tmp_path):
    candidate = str(tmp_path / "unrecognized")
    tmp_path_target = tmp_path / "unrecognized"
    tmp_path_target.mkdir()

    result = mod.repo_root_from_plugin_root_candidate(candidate, allow_unchanged_fallback=False)

    assert result == ""


def test_shared_helper_unknown_drive_root_guard_raises():
    with pytest.raises(ValueError):
        mod.repo_root_from_plugin_root_candidate("x", drive_root_guard="bogus")


def test_shared_helper_unknown_basename_compare_raises(tmp_path):
    # Must reach the basename-compare branch: no marketplace marker directly
    # under candidate, so isfile() short-circuit doesn't return first.
    candidate = str(tmp_path / "coordinator")
    (tmp_path / "coordinator").mkdir()
    with pytest.raises(ValueError):
        mod.repo_root_from_plugin_root_candidate(candidate, basename_compare="bogus")
