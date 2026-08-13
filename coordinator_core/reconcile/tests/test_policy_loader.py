"""
coordinator_core.reconcile.tests.test_policy_loader -- C9 fail-closed loader fixtures.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C9

Covers the plan's required scenario matrix:
  - valid fixture policy -> loaded verbatim, no warning
  - absent policy file -> fail-closed SILENT (no auto-ship, no warning) -- asserted
    distinctly from the malformed case below
  - malformed policy file (fails grammar-pin validation) -> fail-closed LOUD
    (no auto-ship, surfaced data-defect warning) -- asserted distinctly
"""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.reconcile.policy_loader import load_policy, policy_report_fields

_VALID_POLICY = {
    "three_signal": {},
    "mechanical_commit_denylist": [
        "pickup:",
        "reclaim(docs)",
        "session-init",
        "memo:",
        "handoff.transition",
    ],
    "cross_handoff_attribution": True,
    "dry_run": True,
}


def _write_policy(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_valid_fixture_policy_loads_verbatim(tmp_path: Path) -> None:
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    _write_policy(policy_file, _VALID_POLICY)

    result = load_policy(str(policy_file))

    assert result.source == "loaded"
    assert result.warning is None
    assert result.policy["mechanical_commit_denylist"] == _VALID_POLICY["mechanical_commit_denylist"]
    assert result.policy["cross_handoff_attribution"] is True
    assert result.policy["dry_run"] is True
    assert result.policy["auto_ship_enabled"] is False


def test_valid_policy_explicit_auto_ship_enabled_true_is_honored(tmp_path: Path) -> None:
    """auto_ship_enabled is author-writable -- an explicit `true` in the file
    must load as `True`, proving the key is not silently pinned to False."""
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    armed = dict(_VALID_POLICY)
    armed["auto_ship_enabled"] = True
    _write_policy(policy_file, armed)

    result = load_policy(str(policy_file))

    assert result.source == "loaded"
    assert result.warning is None
    assert result.policy["auto_ship_enabled"] is True


def test_malformed_policy_auto_ship_enabled_wrong_type_is_fail_closed_loud(tmp_path: Path) -> None:
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    broken = dict(_VALID_POLICY)
    broken["auto_ship_enabled"] = "yes"  # should be a bool
    _write_policy(policy_file, broken)

    result = load_policy(str(policy_file))

    assert result.source == "malformed"
    assert result.warning is not None
    assert "auto_ship_enabled" in result.warning
    assert result.policy["auto_ship_enabled"] is False


def test_absent_policy_is_fail_closed_silent(tmp_path: Path) -> None:
    missing_path = tmp_path / "does-not-exist.yaml"

    result = load_policy(str(missing_path))

    assert result.source == "absent"
    assert result.warning is None
    assert result.policy["dry_run"] is True
    assert result.policy["auto_ship_enabled"] is False


def test_malformed_policy_missing_required_key_is_fail_closed_loud(tmp_path: Path) -> None:
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    broken = dict(_VALID_POLICY)
    del broken["mechanical_commit_denylist"]
    _write_policy(policy_file, broken)

    result = load_policy(str(policy_file))

    assert result.source == "malformed"
    assert result.warning is not None
    assert "mechanical_commit_denylist" in result.warning
    assert result.policy["dry_run"] is True
    assert result.policy["auto_ship_enabled"] is False


def test_malformed_policy_wrong_type_is_fail_closed_loud(tmp_path: Path) -> None:
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    broken = dict(_VALID_POLICY)
    broken["cross_handoff_attribution"] = "yes"  # should be a bool
    _write_policy(policy_file, broken)

    result = load_policy(str(policy_file))

    assert result.source == "malformed"
    assert result.warning is not None
    assert "cross_handoff_attribution" in result.warning
    assert result.policy["auto_ship_enabled"] is False


def test_malformed_policy_invalid_yaml_is_fail_closed_loud(tmp_path: Path) -> None:
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text("this: [is not, valid: yaml", encoding="utf-8")

    result = load_policy(str(policy_file))

    assert result.source == "malformed"
    assert result.warning is not None
    assert result.policy["auto_ship_enabled"] is False


def test_malformed_policy_non_mapping_root_is_fail_closed_loud(tmp_path: Path) -> None:
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")

    result = load_policy(str(policy_file))

    assert result.source == "malformed"
    assert result.warning is not None
    assert result.policy["auto_ship_enabled"] is False


def test_absent_and_malformed_are_distinct_branches(tmp_path: Path) -> None:
    missing = load_policy(str(tmp_path / "nope.yaml"))

    broken_path = tmp_path / "broken.yaml"
    broken_path.write_text("not_a_valid_policy: true", encoding="utf-8")
    malformed = load_policy(str(broken_path))

    assert missing.source != malformed.source
    assert missing.warning is None
    assert malformed.warning is not None
    assert missing.policy["auto_ship_enabled"] == malformed.policy["auto_ship_enabled"] is False


def test_policy_report_fields_covers_all_three_source_values(tmp_path: Path) -> None:
    """§ C10 / AC16 -- a downstream report must be able to distinguish
    absent/malformed/loaded AND see the path this call looked at, for every
    branch, including absent (the actionable "path we looked for and did not
    find" half named in the plan chunk body).
    """
    # absent: no file at the resolved path -- policy_path still reports the
    # path that was looked for, not None, per the plan's own "the path we
    # looked for and did not find" framing.
    absent_path = tmp_path / "does-not-exist.yaml"
    absent = load_policy(str(absent_path))
    absent_fields = policy_report_fields(absent)
    assert absent_fields == {"policy_source": "absent", "policy_path": str(absent_path)}

    # malformed: file present, fails grammar-pin validation.
    malformed_path = tmp_path / "broken.yaml"
    malformed_path.write_text("not_a_valid_policy: true", encoding="utf-8")
    malformed = load_policy(str(malformed_path))
    malformed_fields = policy_report_fields(malformed)
    assert malformed_fields == {"policy_source": "malformed", "policy_path": str(malformed_path)}

    # loaded: valid fixture policy.
    loaded_path = tmp_path / "auto-reconcile-policy.yaml"
    _write_policy(loaded_path, _VALID_POLICY)
    loaded = load_policy(str(loaded_path))
    loaded_fields = policy_report_fields(loaded)
    assert loaded_fields == {"policy_source": "loaded", "policy_path": str(loaded_path)}


def test_policy_report_fields_absent_with_no_resolvable_candidate_is_none_path(
    monkeypatch,
) -> None:
    """When resolution finds no candidate at all (no explicit path, no env
    var, `CLAUDE_PLUGIN_ROOT` unset/unresolvable) -- as opposed to a resolved
    path that turned out not to exist -- `policy_path` is None, not a
    fabricated path.
    """
    monkeypatch.delenv("AUTO_RECONCILE_POLICY", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    result = load_policy(None)
    fields = policy_report_fields(result)

    assert fields == {"policy_source": "absent", "policy_path": None}


# ---------------------------------------------------------------------------
# C2: repo-resident overlay discovery + key-by-key merge (plan
# `2026-08-13-repo-resident-policy-overlay-discovery-and-merge.md`).
#
# NEVER write `auto-reconcile-policy.local.yaml` under the real repo root --
# every fixture below lives under `tmp_path`, with `monkeypatch.chdir` used
# to establish repo identity via a fixture `.git` directory. A stray overlay
# at claude-klabauter's own root would arm this repo's auto-reconcile the moment the
# route resolves it.
# ---------------------------------------------------------------------------

_FULL_FLOOR = {
    "three_signal": {},
    "mechanical_commit_denylist": [
        "pickup:",
        "reclaim(docs)",
        "session-init",
        "memo:",
        "handoff.transition",
    ],
    "cross_handoff_attribution": True,
    "dry_run": True,
}


def _make_repo(tmp_path: Path) -> Path:
    """A fixture repo root: a directory with a `.git` marker, nothing else."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _clear_policy_env(monkeypatch) -> None:
    monkeypatch.delenv("AUTO_RECONCILE_POLICY", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


def test_overlay_route_precedence_below_explicit_path(tmp_path: Path, monkeypatch) -> None:
    """AC1: explicit `policy_path` wins over a discovered overlay."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    explicit_path = tmp_path / "explicit.yaml"
    _write_policy(explicit_path, _VALID_POLICY)

    result = load_policy(str(explicit_path))

    assert result.resolved_path == str(explicit_path)
    assert result.source == "loaded"


def test_overlay_route_precedence_below_env_var(tmp_path: Path, monkeypatch) -> None:
    """AC1: `AUTO_RECONCILE_POLICY` env var wins over a discovered overlay."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    env_path = tmp_path / "env.yaml"
    _write_policy(env_path, _VALID_POLICY)
    monkeypatch.setenv("AUTO_RECONCILE_POLICY", str(env_path))

    result = load_policy(None)

    assert result.resolved_path == str(env_path)
    assert result.source == "loaded"


def test_overlay_route_precedence_above_plugin_root_default(tmp_path: Path, monkeypatch) -> None:
    """AC1: a discovered overlay wins over the `CLAUDE_PLUGIN_ROOT` floor default."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _VALID_POLICY)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    result = load_policy(None)

    assert result.resolved_path == str(overlay_path)
    assert result.source == "loaded"


def test_overlay_absent_falls_through_to_plugin_root_default(tmp_path: Path, monkeypatch) -> None:
    """AC1/AC3: no overlay present -> route 4 (`CLAUDE_PLUGIN_ROOT` default) resolves,
    byte-unchanged from today's behaviour."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _VALID_POLICY)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    result = load_policy(None)

    assert result.resolved_path == str(floor_path)
    assert result.source == "loaded"


def test_repo_root_resolves_from_nested_cwd(tmp_path: Path, monkeypatch) -> None:
    """AC2: repo identity resolves via a `.git` walk-up from a nested cwd
    inside the fixture repo, with no `git` subprocess involved."""
    repo = _make_repo(tmp_path)
    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    _clear_policy_env(monkeypatch)

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    result = load_policy(None)

    assert result.resolved_path == str(overlay_path)
    assert result.source == "loaded"


def test_no_subprocess_import_in_policy_loader() -> None:
    """AC2: no `subprocess`/`resolve_git_root` import added to the module --
    repo identity is a pathlib walk-up, spawn-free."""
    import coordinator_core.reconcile.policy_loader as policy_loader_module

    assert "subprocess" not in vars(policy_loader_module)
    assert not hasattr(policy_loader_module, "resolve_git_root")
    source = Path(policy_loader_module.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in source


def test_overlay_partial_merges_over_floor_key_by_key(tmp_path: Path, monkeypatch) -> None:
    """AC4: floor with all four keys + overlay restating only `dry_run` ->
    merged carries the floor's other three keys unchanged."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _FULL_FLOOR)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"dry_run": False})

    result = load_policy(None)

    assert result.policy["mechanical_commit_denylist"] == _FULL_FLOOR["mechanical_commit_denylist"]
    assert result.policy["cross_handoff_attribution"] == _FULL_FLOOR["cross_handoff_attribution"]
    assert result.policy["three_signal"] == _FULL_FLOOR["three_signal"]
    assert result.policy["dry_run"] is False


def test_overlay_dry_run_only_merges_as_loaded_not_malformed(tmp_path: Path, monkeypatch) -> None:
    """AC5: the `dry_run`-only overlay merges validly and loads as
    `source == "loaded"`, not `malformed`, because validation runs against
    the merged result."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _FULL_FLOOR)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"dry_run": False})

    result = load_policy(None)

    assert result.source == "loaded"
    assert result.warning is None


def test_merged_result_still_missing_required_key_is_malformed(tmp_path: Path, monkeypatch) -> None:
    """AC5: a floor-plus-overlay pair still missing a required key after
    merge is `malformed`."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    incomplete_floor = dict(_FULL_FLOOR)
    del incomplete_floor["mechanical_commit_denylist"]

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, incomplete_floor)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"dry_run": False})

    result = load_policy(None)

    assert result.source == "malformed"
    assert result.warning is not None
    assert "mechanical_commit_denylist" in result.warning


def test_overlay_cannot_arm_auto_ship_by_omission(tmp_path: Path, monkeypatch) -> None:
    """AC6: overlay `{dry_run: false}` over a floor that never mentions
    `auto_ship_enabled` -- `auto_ship_enabled` stays `False`, fail-closed."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _FULL_FLOOR)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"dry_run": False})

    result = load_policy(None)

    assert result.policy["auto_ship_enabled"] is False


def test_overlay_resolved_path_and_source_reported(tmp_path: Path, monkeypatch) -> None:
    """AC7: `resolved_path` names the overlay when one was discovered, and
    `source` is `loaded`."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _FULL_FLOOR)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"dry_run": False})

    result = load_policy(None)

    assert result.resolved_path == str(overlay_path)
    assert result.source == "loaded"


def test_overlay_with_malformed_floor_is_fail_closed_loud(tmp_path: Path, monkeypatch) -> None:
    """A floor that exists but fails to parse must NOT be silently treated as
    absent (merged over `{}`) -- that would risk the merged-plus-overlay
    result passing grammar validation on the overlay's own keys alone and
    reporting `source="loaded"`, losing the malformed-floor signal `source`
    exists to carry. This must surface loud, like any other malformed
    branch."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    floor_path.write_text("this: [is not, valid: yaml", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    result = load_policy(None)

    assert result.source == "malformed"
    assert result.warning is not None
    assert str(floor_path) in result.warning
    assert result.policy["auto_ship_enabled"] is False


def test_overlay_with_non_mapping_floor_is_fail_closed_loud(tmp_path: Path, monkeypatch) -> None:
    """A floor file whose parsed root isn't a mapping is malformed, not
    silently absorbed as an absent-equivalent empty floor."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    floor_path.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    result = load_policy(None)

    assert result.source == "malformed"
    assert result.warning is not None
    assert result.policy["auto_ship_enabled"] is False


def test_overlay_absent_floor_still_falls_closed_silent(tmp_path: Path, monkeypatch) -> None:
    """Sanity check the malformed-floor fix didn't regress the genuinely
    absent-floor case: no floor file at all still merges over `{}`
    (byte-unchanged prior behaviour), not treated as malformed."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, _FULL_FLOOR)

    result = load_policy(None)

    assert result.source == "loaded"
    assert result.warning is None


def test_overlay_absent_no_git_root_falls_through(tmp_path: Path, monkeypatch) -> None:
    """AC3: no `.git` ancestor at all (unresolvable repo root) falls
    straight through to the existing default, no overlay route taken."""
    no_repo = tmp_path / "no_repo"
    no_repo.mkdir()
    monkeypatch.chdir(no_repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    _write_policy(floor_path, _VALID_POLICY)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    result = load_policy(None)

    assert result.resolved_path == str(floor_path)
    assert result.source == "loaded"
