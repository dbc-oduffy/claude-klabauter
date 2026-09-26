"""
Tests for coordinator_core.install.check_install_singularity.

Independently re-derives the oracle's own AC coverage (DoE-claude
coordinator/lib/tests/test-check-install-singularity.sh T1-T12) against a
synthetic ~/.claude layout, rather than re-asserting the port's own
transcription — each test builds a fresh filesystem fixture representing one
oracle scenario and calls the module's public entry points directly
(no subprocess, no re-import of the port's own constants).

Spec backlink:
  docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R1b AC4/AC5/AC5b
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
  (BIG_PORT Wave B, item check-install-singularity)
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.install import check_install_singularity as sut


def _make_tree(path):
    path.mkdir(parents=True, exist_ok=True)
    manifest = path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"name": "coordinator-claude"}', encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Build a minimal ~/.claude tree under tmp_path/home plus a SEPARATE
    settings-home tree under tmp_path/settings-home, scrub all
    coordinator-resolution env vars, and point HOME/COORDINATOR_SETTINGS_HOME
    at them.

    The registry lives under settings-home (``machine_resolver.registry_get``
    precedence), NOT under ``~/.claude`` — kept as two distinct roots here
    (rather than one shared tmp dir) so a future regression that reads the
    registry from the wrong directory fails loudly instead of accidentally
    passing because both roots happen to coincide."""
    fake_home = tmp_path / "home"
    flat_path = fake_home / ".claude" / "plugins" / "coordinator-claude"
    _make_tree(flat_path)
    settings_json = fake_home / ".claude" / "settings.json"
    settings_json.write_text("{}", encoding="utf-8")

    settings_home_dir = tmp_path / "settings-home"
    (settings_home_dir / "machine-local").mkdir(parents=True)

    for var in ("COORDINATOR_CLONE", "COORDINATOR_ROOT", "CLAUDE_PLUGIN_ROOT", "CLAUDE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))

    return {
        "fake_home": fake_home,
        "flat_path": flat_path,
        "settings_home_dir": settings_home_dir,
        "reg_file": settings_home_dir / "machine-local" / "registry.local.toml",
        "settings_json": settings_json,
    }


def _run():
    rc, out, err = sut.run()
    return rc, out + err


# T1 — .claude-suffixed CLAUDE_HOME -> FAIL
def test_t1_claude_home_suffix_guard_fails(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(home["fake_home"] / ".claude"))
    rc, text = _run()
    assert rc != 0
    assert "remediation" in text.lower()


def test_t2_doubled_venv_pin_fails(home):
    home["reg_file"].write_text(
        '[coordinator]\npython = "/home/user/.claude/.claude/.coordinator-venv/bin/python"\n',
        encoding="utf-8",
    )
    rc, text = _run()
    assert rc != 0
    assert "remediation" in text.lower()


def test_t3_multiple_trees_fails(home, tmp_path):
    extra_tree = tmp_path / "extra" / "coordinator-claude" / "coordinator"
    _make_tree(extra_tree)
    home["reg_file"].write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{extra_tree.as_posix()}"\n', encoding="utf-8"
    )
    rc, text = _run()
    assert rc != 0
    assert "remediation" in text.lower()


def test_t4_settings_disagree_fails(home, tmp_path):
    canonical_path = str(home["flat_path"])
    other_path = tmp_path / "other" / "coordinator-claude"
    _make_tree(other_path)

    home["settings_json"].write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "coordinator-claude": {"source": {"source": "directory", "path": canonical_path}}
                }
            }
        ),
        encoding="utf-8",
    )
    plugins_dir = home["fake_home"] / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps(
            {"coordinator-claude": {"source": {"source": "directory", "path": str(other_path)}}}
        ),
        encoding="utf-8",
    )
    rc, text = _run()
    assert rc != 0
    assert "remediation" in text.lower()


def test_t5_live_path_matches_flat_dedupes_to_zero(home):
    coord_subdir = home["flat_path"] / "coordinator"
    _make_tree(coord_subdir)
    home["reg_file"].write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{coord_subdir.as_posix()}"\n', encoding="utf-8"
    )
    rc, text = _run()
    assert rc == 0, text


def test_t6_single_tree_ok(home):
    rc, text = _run()
    assert rc == 0, text


def test_registry_local_wins_over_tracked_for_live_path(home):
    coord_subdir = home["flat_path"] / "coordinator"
    _make_tree(coord_subdir)
    tracked = home["settings_home_dir"] / "machine-local" / "registry.toml"
    tracked.write_text('"plugin.mirrors.coordinator-claude.live_path" = ""\n', encoding="utf-8")
    home["reg_file"].write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{coord_subdir}"\n', encoding="utf-8"
    )
    rc, text = _run()
    assert rc == 0, text


def test_empty_string_tracked_declaration_is_a_miss_not_a_second_tree(home, tmp_path):
    tracked = home["settings_home_dir"] / "machine-local" / "registry.toml"
    tracked.write_text('"plugin.mirrors.coordinator-claude.live_path" = ""\n', encoding="utf-8")
    rc, text = _run()
    assert rc == 0, text


def test_t7_absent_local_settings_concordant(home):
    home["settings_json"].write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "coordinator-claude": {
                        "source": {"source": "directory", "path": str(home["flat_path"])}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plugins_dir = home["fake_home"] / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps(
            {"coordinator-claude": {"source": {"source": "directory", "path": str(home["flat_path"])}}}
        ),
        encoding="utf-8",
    )
    assert not (home["fake_home"] / ".claude" / "settings.local.json").exists()
    rc, text = _run()
    assert rc == 0, text


# T8 — single COORDINATOR_CLONE (.git-backed) -> exempt -> exit 0
def test_t8_coordinator_clone_exempt(home, tmp_path, monkeypatch):
    clone_dir = tmp_path / "clone" / "coordinator-claude"
    _make_tree(clone_dir)
    (clone_dir / ".git").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_CLONE", str(clone_dir))
    rc, text = _run()
    assert rc == 0, text
    assert "dev-loop override" in text.lower()


# T9 — single COORDINATOR_ROOT (parent .git-backed) -> exempt -> exit 0
def test_t9_coordinator_root_exempt(home, tmp_path, monkeypatch):
    root_plugin = tmp_path / "root_plugin" / "coordinator-claude"
    _make_tree(root_plugin)
    (root_plugin / ".git").mkdir(parents=True)
    (root_plugin / "coordinator").mkdir()
    monkeypatch.setenv("COORDINATOR_ROOT", str(root_plugin / "coordinator"))
    rc, text = _run()
    assert rc == 0, text


# T10 — CLAUDE_PLUGIN_ROOT is NOT exempt (harness-injected)
def test_t10_claude_plugin_root_not_exempt(home, tmp_path, monkeypatch):
    cpr_dir = tmp_path / "cpr" / "coordinator-claude"
    _make_tree(cpr_dir)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(cpr_dir))
    rc, text = _run()
    assert rc != 0, text


# T12 — CLAUDE_HOME already .claude-suffixed (sentinel leakage shape) -> FAIL
def test_t12_claude_home_leakage_shape_fails(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(home["fake_home"] / ".claude" / ".claude"))
    rc, text = _run()
    assert rc != 0, text


def test_main_returns_zero_on_clean_install(home, capsys):
    rc = sut.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "singularity OK" in captured.out


def test_main_returns_one_on_split(home, tmp_path, monkeypatch, capsys):
    extra_tree = tmp_path / "extra" / "coordinator-claude" / "coordinator"
    _make_tree(extra_tree)
    home["reg_file"].write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{extra_tree.as_posix()}"\n', encoding="utf-8"
    )
    rc = sut.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "remediation" in captured.err.lower()


def test_main_help_returns_zero(capsys):
    rc = sut.main(["--help"])
    assert rc == 0


def test_to_plugin_root_bare_coordinator_unchanged():
    assert sut._to_plugin_root("coordinator") == "coordinator"


def test_to_plugin_root_strips_trailing_coordinator_component():
    assert sut._to_plugin_root("/foo/bar/coordinator") == "/foo/bar"


def test_to_plugin_root_leaves_non_coordinator_basename():
    assert sut._to_plugin_root("/foo/bar/coordinator-claude") == "/foo/bar/coordinator-claude"


def test_has_parent_child_pair_detects_offset_shape():
    assert sut._has_parent_child_pair(["X:/DoE-claude", "X:/DoE-claude/coordinator"])


def test_has_parent_child_pair_false_for_genuinely_distinct_trees():
    assert not sut._has_parent_child_pair(["X:/DoE-claude", "X:/other/coordinator-claude"])


def test_parent_child_split_remediation_names_offset_shape_not_deletion(home, monkeypatch):
    parent = str(home["fake_home"] / ".claude" / "plugins" / "coordinator-claude")
    child = parent + "/coordinator/extra"

    fake_trees = sut._TreeSet()
    fake_trees._seen[parent] = parent
    fake_trees._seen[child] = child

    monkeypatch.setattr(
        sut, "_check4_tree_enumeration", lambda *a, **k: (fake_trees, "")
    )
    rc, text = _run()
    assert rc != 0
    assert "one-level-offset" in text.lower()
    assert "do not remove" in text.lower()


def test_no_offset_note_for_genuinely_distinct_trees(home, monkeypatch):
    tree_a = str(home["fake_home"] / ".claude" / "plugins" / "coordinator-claude")
    tree_b = str(home["fake_home"].parent / "unrelated" / "coordinator-claude")

    fake_trees = sut._TreeSet()
    fake_trees._seen[tree_a] = tree_a
    fake_trees._seen[tree_b] = tree_b

    monkeypatch.setattr(
        sut, "_check4_tree_enumeration", lambda *a, **k: (fake_trees, "")
    )
    rc, text = _run()
    assert rc != 0
    assert "one-level-offset" not in text.lower()
    assert "remove the extra tree" in text.lower()


# only, so a native-Windows `CLAUDE_PLUGIN_ROOT` (`X:\DoE-claude\coordinator`)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only separator normalization")
def test_to_plugin_root_windows_backslash_normalizes_like_forward_slash():
    forward = sut._to_plugin_root("X:/DoE-claude/coordinator")
    backslash = sut._to_plugin_root("X:\\DoE-claude\\coordinator")
    assert backslash == forward == "X:/DoE-claude"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only separator normalization")
def test_to_plugin_root_windows_double_trailing_backslash_not_stripped():
    assert sut._to_plugin_root("X:\\foo\\coordinator\\\\") == "X:/foo/coordinator/"


@pytest.mark.skipif(os.name == "nt", reason="backslash is a legal POSIX filename character")
def test_to_plugin_root_posix_backslash_left_untouched():
    raw = "/foo/bar\\coordinator"
    assert sut._to_plugin_root(raw) == raw


@pytest.mark.skipif(os.name != "nt", reason="Windows-only separator normalization")
def test_windows_backslash_claude_plugin_root_dedupes_with_forward_slash_live_path(home, monkeypatch):
    """Full-gate regression: the registry's live_path is stored
    forward-slashed while CLAUDE_PLUGIN_ROOT (harness-injected, native
    Windows) names the SAME tree with backslashes. Both must normalize to
    ONE canonical plugin root and the gate must exit 0 -- pre-fix, the
    backslashed form bypassed `_to_plugin_root`'s forward-slash-only suffix
    test, entered the tree set at content level instead of plugin-root
    level, and the gate reported the one clone as an "accidental split"
    into 2 distinct trees, with remediation advising deletion of half the
    operator's own clone."""
    coord_subdir = home["flat_path"] / "coordinator"
    _make_tree(coord_subdir)
    forward = str(coord_subdir).replace("\\", "/")
    backslash = str(coord_subdir).replace("/", "\\")
    home["reg_file"].write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{forward}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", backslash)

    rc, text = _run()

    assert rc == 0, text
