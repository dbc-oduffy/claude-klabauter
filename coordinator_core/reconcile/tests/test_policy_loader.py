"""
coordinator_core.reconcile.tests.test_policy_loader -- C9 fail-closed loader fixtures.

Spec backlink: docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C9

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
