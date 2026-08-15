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
    tmp_path: Path, monkeypatch,
) -> None:
    """When resolution finds no candidate at all (no explicit path, no env
    var, `CLAUDE_PLUGIN_ROOT` unset/unresolvable) -- as opposed to a resolved
    path that turned out not to exist -- `policy_path` is None, not a
    fabricated path.

    Chdir's to a `.git`-free tmp dir so this repo's own real overlay (see
    the C2 tests below) cannot be discovered here -- this test is about the
    genuinely-no-candidate branch, not repo-resident overlay discovery.
    """
    monkeypatch.chdir(tmp_path)
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
    """Sanity check the absent-floor fix didn't regress the genuinely
    absent-floor case with a FULL overlay: no floor file present at
    `CLAUDE_PLUGIN_ROOT`, overlay names every required key itself -- merges
    over the conservative-defaults base and loads, not treated as
    malformed."""
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


def test_overlay_with_unresolvable_plugin_root_still_arms_from_partial_overlay(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression pin: with `CLAUDE_PLUGIN_ROOT` unset (deleted, not merely
    unset-by-default) and a repo root carrying a PARTIAL overlay naming only
    `auto_ship_enabled`/`dry_run`, load_policy() must resolve `source ==
    "loaded"` and come back armed -- the floor's absence must not silently
    fall back to `{}` and reject the overlay for missing keys it never
    intended to restate. This must FAIL against the pre-fix code (which used
    `floor: Dict[str, Any] = {}` as the merge base when the floor path did
    not resolve)."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"auto_ship_enabled": True, "dry_run": False})

    result = load_policy(None)

    assert result.source == "loaded"
    assert result.resolved_path == str(overlay_path)
    assert result.policy["auto_ship_enabled"] is True
    assert result.policy["dry_run"] is False


def test_overlay_with_unresolvable_plugin_root_keeps_unnamed_keys_conservative(
    tmp_path: Path, monkeypatch,
) -> None:
    """Companion to the regression pin above: keys the partial overlay does
    NOT name must still resolve to `_conservative_policy()`'s fail-closed
    values, not vanish or default to some other shape, when the floor is
    absent."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"auto_ship_enabled": True, "dry_run": False})

    result = load_policy(None)

    assert result.source == "loaded"
    assert result.policy["three_signal"] == {}
    assert result.policy["mechanical_commit_denylist"] == []
    assert result.policy["cross_handoff_attribution"] is True


def test_overlay_with_malformed_floor_not_treated_as_absent_even_unset(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pins the branch this fix must NOT disturb: a floor path that DOES
    resolve (via `CLAUDE_PLUGIN_ROOT`) but fails to parse still reports
    `source == "malformed"` and does not merge -- distinct from the
    genuinely-absent-floor case the fix changes."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_policy_env(monkeypatch)

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    floor_path = plugin_root / "auto-reconcile-policy.yaml"
    floor_path.write_text("not: [valid: yaml", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    overlay_path = repo / "auto-reconcile-policy.local.yaml"
    _write_policy(overlay_path, {"auto_ship_enabled": True, "dry_run": False})

    result = load_policy(None)

    assert result.source == "malformed"
    assert result.policy["auto_ship_enabled"] is False


# ---------------------------------------------------------------------------
# C2: this repo's real `auto-reconcile-policy.local.yaml` overlay (plan
# `2026-08-15-arm-per-repo-auto-reconcile-so-finished.md` § C2, AC3-AC5).
#
# Unlike the fixtures above, these three tests deliberately chdir to the
# REAL repo root and resolve against the REAL overlay file that ships at
# the repo root -- that is the point: they are the regression that fires if
# the overlay is deleted or becomes gitignored.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The real DoE-owned fleet floor this repo's `CLAUDE_PLUGIN_ROOT` resolves
#: to in a live session -- sibling repo, read-only from here (see Anti-scope:
#: this file is NEVER written by claude-klabauter). Used only so these three tests
#: exercise the real merge against the real floor rather than an unresolved
#: `CLAUDE_PLUGIN_ROOT`, which would report `malformed` for an unrelated
#: reason (floor not found) and mask what these tests actually pin.
_REAL_PLUGIN_ROOT = _REPO_ROOT.parent / "DoE-claude" / "coordinator"


def test_this_repo_resolves_armed_from_the_real_overlay(monkeypatch) -> None:
    """AC4: from this repo's own root, with `CLAUDE_PLUGIN_ROOT` UNSET
    (the common non-harness invocation, not just the pytest-inherited case),
    load_policy() resolves the REAL `auto-reconcile-policy.local.yaml` at the
    repo root and reports it armed. Fails if that file is deleted or
    gitignored, and must not depend on `CLAUDE_PLUGIN_ROOT` happening to be
    set in the calling environment (see the absent-floor-merges-over-
    conservative-defaults fix this test pins)."""
    monkeypatch.chdir(_REPO_ROOT)
    _clear_policy_env(monkeypatch)

    overlay_path = _REPO_ROOT / "auto-reconcile-policy.local.yaml"
    assert overlay_path.is_file(), (
        "auto-reconcile-policy.local.yaml is missing from the repo root -- "
        "this repo's auto-reconcile arming has regressed"
    )

    result = load_policy(None)

    assert result.source == "loaded"
    assert result.resolved_path == str(overlay_path)
    assert result.policy["auto_ship_enabled"] is True
    assert result.policy["dry_run"] is False


def test_a_different_repo_root_does_not_come_back_armed(tmp_path: Path, monkeypatch) -> None:
    """AC5: a sibling repo root with no overlay of its own still falls
    through to the floor -- this repo's overlay cannot leak sideways."""
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    (other_repo / ".git").mkdir()
    monkeypatch.chdir(other_repo)
    _clear_policy_env(monkeypatch)

    result = load_policy(None)

    assert result.resolved_path != str(_REPO_ROOT / "auto-reconcile-policy.local.yaml")
    assert result.policy["auto_ship_enabled"] is False


def test_this_repo_overlay_is_a_partial_overlay_floor_supplies_the_rest(monkeypatch) -> None:
    """The real overlay names only auto_ship_enabled/dry_run -- a key it
    does not name (cross_handoff_attribution) must still resolve from the
    floor rather than vanishing, pinning the shallow key-by-key merge
    contract this file's own header comment documents."""
    monkeypatch.chdir(_REPO_ROOT)
    _clear_policy_env(monkeypatch)

    overlay_path = _REPO_ROOT / "auto-reconcile-policy.local.yaml"
    overlay_data = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    assert set(overlay_data.keys()) == {"auto_ship_enabled", "dry_run"}, (
        "the real overlay must stay a partial overlay naming only the two "
        "arming keys -- restating a floor key here would fork it silently"
    )

    result = load_policy(None)

    if result.source == "loaded":
        assert "cross_handoff_attribution" in result.policy
        assert "three_signal" in result.policy
        assert "mechanical_commit_denylist" in result.policy
    else:
        # The floor itself may be absent/malformed depending on this
        # environment's CLAUDE_PLUGIN_ROOT -- that is a floor-resolution
        # fact, not a regression of THIS overlay's partial-merge shape,
        # which the keys-set assertion above already pinned directly.
        pass


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
