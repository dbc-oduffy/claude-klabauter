"""Tests for coordinator_core.snippet_sync.verify_registry_consistency.

The four-script (`verify-<X>-sync.sh`) leg this module once ported from the
retired `coordinator/bin` bash oracle (721 LoC, deleted at DoE's `93887f6f`
de-bash cutover) was retired 2026-07-22 — see the module docstring and the
actioned inbound memo
`cross-repo/inbox/2026-07-22-claude-central-em-snippet-registry-consistency-fix-locus.md`.
Remaining coverage below exercises only the surviving consistency checks:
registry-exists, TOML-parse, schema_version gate, and per-snippet
`[snippet.<name>]` enrollment.

Spec backlink: DoE docs/plans/2026-06-15-snippet-sync-consumer-registry.md § Dispatch Ledger C4, C8
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.snippet_sync import registry
from coordinator_core.snippet_sync import verify_registry_consistency as vrc


def _write_plugin_root(
    tmp_path: Path,
    *,
    schema_version: object = 2,
    enrolled: list[str] | None = None,
    omit_schema_version: bool = False,
    extra_row_lines: dict[str, list[str]] | None = None,
    omit_delivery: bool = False,
) -> Path:
    """Build a synthetic plugin_root tree: snippets/registry.toml only.

    `delivery` is emitted on every row from schema_version 3 onward (where it is
    REQUIRED) and omitted below it (where it does not exist and defaults to
    "paste"); `omit_delivery` forces the absence at v3+ to exercise that rule.
    `extra_row_lines` appends raw TOML lines to a named row — the seam for the
    v4 `excluded_consumer` / `eligible_glob` cases.
    """
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "snippets").mkdir(parents=True)

    enrolled = vrc.SNIPPET_NAMES if enrolled is None else enrolled
    extra_row_lines = extra_row_lines or {}
    numeric_version = schema_version if isinstance(schema_version, int) else 0

    toml_lines = []
    if not omit_schema_version:
        toml_lines.append(f"schema_version = {schema_version}")
    for name in enrolled:
        toml_lines.append(f"[snippet.{name}]")
        toml_lines.append('sentinel_begin = "b"')
        toml_lines.append('sentinel_end = "e"')
        toml_lines.append(f'consumers = ["snippets/{name}.md"]')
        if numeric_version >= 3 and not omit_delivery:
            toml_lines.append('delivery = "paste"')
        toml_lines.extend(extra_row_lines.get(name, []))
    (plugin_root / "snippets" / "registry.toml").write_text(
        "\n".join(toml_lines) + "\n", encoding="utf-8"
    )

    return plugin_root


def test_all_enrolled_passes(tmp_path):
    plugin_root = _write_plugin_root(tmp_path)
    outcome = vrc.run(plugin_root)
    assert outcome.exit_code == 0
    assert any("OK:" in line for line in outcome.lines)


def test_missing_enrollment_fails(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path, enrolled=[n for n in vrc.SNIPPET_NAMES if n != "reviewer-calibration"]
    )
    outcome = vrc.run(plugin_root)
    assert outcome.exit_code == 1
    assert any(
        "FAIL [enrollment] reviewer-calibration: snippet not enrolled in registry.toml" in line
        for line in outcome.stderr_lines
    )


def test_missing_registry_toml_exits_2(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    plugin_root.mkdir()
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 2


def test_missing_schema_version_exits_2_not_3(tmp_path):
    """Negative-spec: faithful reproduction of the oracle's own bug — a
    generic 'ERROR ...' parser-output early-exit fires before the dedicated
    schema-version-value check, so a MISSING schema_version exits 2, not the
    documented 3 (see module docstring negative-spec)."""
    plugin_root = _write_plugin_root(tmp_path, omit_schema_version=True)
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 2


def test_unsupported_schema_version_value_exits_3(tmp_path):
    plugin_root = _write_plugin_root(tmp_path, schema_version=99)
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 3


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_every_version_the_sibling_reader_supports_is_readable_here(tmp_path, schema_version):
    """THE regression. The local gate was `str(schema_version) not in ("1","2")`
    while `registry.py` already read 1-4, so DoE's v4 registry (2026-08-03)
    made this verifier exit 3 on every run for six weeks. v3 and v4 fail against
    that old gate; all four must pass now, and the supported set is read off the
    sibling reader so the two cannot diverge again silently.
    """
    assert schema_version in registry._SUPPORTED_SCHEMA_VERSIONS
    plugin_root = _write_plugin_root(tmp_path, schema_version=schema_version)
    outcome = vrc.run(plugin_root)
    assert outcome.exit_code == 0
    assert any("OK:" in line for line in outcome.lines)


def test_delivery_absent_at_v3_is_rejected(tmp_path):
    """v3 made `delivery` REQUIRED on every row. A bare version bump that
    accepted v3 without this check would report clean while checking nothing."""
    plugin_root = _write_plugin_root(tmp_path, schema_version=3, omit_delivery=True)
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 1
    assert "'delivery'" in str(exc_info.value)


def test_delivery_absent_below_v3_is_permitted(tmp_path):
    """On v1/v2 the field does not exist and its absence means "paste"."""
    plugin_root = _write_plugin_root(tmp_path, schema_version=2, omit_delivery=True)
    assert vrc.run(plugin_root).exit_code == 0


def test_invalid_delivery_value_is_reported(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=4,
        omit_delivery=True,
        extra_row_lines={name: ['delivery = "scan"'] for name in vrc.SNIPPET_NAMES},
    )
    outcome = vrc.run(plugin_root)
    assert outcome.exit_code == 1
    assert any(
        "FAIL [fields] reviewer-calibration" in line and "delivery" in line
        for line in outcome.stderr_lines
    )


def test_v4_fields_rejected_below_v4(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=3,
        extra_row_lines={"reviewer-calibration": ['eligible_glob = "snippets/*.md"']},
    )
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 1
    assert "schema_version >= 4" in str(exc_info.value)


def test_excluded_consumer_needs_a_nonempty_reason(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=4,
        extra_row_lines={
            "reviewer-calibration": [
                "[[snippet.reviewer-calibration.excluded_consumer]]",
                'path = "snippets/bespoke.md"',
                'reason = "   "',
            ]
        },
    )
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 1
    assert "reason" in str(exc_info.value)


def test_excluded_consumer_contradicting_consumers_fails(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=4,
        extra_row_lines={
            "reviewer-calibration": [
                "[[snippet.reviewer-calibration.excluded_consumer]]",
                'path = "snippets/reviewer-calibration.md"',
                'reason = "also enrolled — a contradiction"',
            ]
        },
    )
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 1
    assert "contradictory" in str(exc_info.value)


def test_v4_fields_forbidden_on_a_scan_row(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=4,
        extra_row_lines={
            "reviewer-calibration": [
                'consumer_source = "scan"',
                'eligible_glob = "snippets/*.md"',
            ]
        },
    )
    with pytest.raises(vrc.ConsistencyError) as exc_info:
        vrc.run(plugin_root)
    assert exc_info.value.exit_code == 1
    assert "FORBIDDEN" in str(exc_info.value)


def test_eligible_glob_gap_is_reported(tmp_path):
    """The v4 completeness check: a glob member in neither `consumers` nor
    `excluded_consumer` is the defect the field pair exists to catch."""
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=4,
        extra_row_lines={"reviewer-calibration": ['eligible_glob = "snippets/*.md"']},
    )
    (plugin_root / "snippets" / "reviewer-calibration.md").write_text("x", encoding="utf-8")
    (plugin_root / "snippets" / "undeclared.md").write_text("x", encoding="utf-8")

    outcome = vrc.run(plugin_root)
    assert outcome.exit_code == 1
    gap_lines = [l for l in outcome.stderr_lines if "FAIL [eligible_glob]" in l]
    assert len(gap_lines) == 1
    assert "snippets/undeclared.md" in gap_lines[0]


def test_eligible_glob_gap_closed_by_excluded_consumer(tmp_path):
    plugin_root = _write_plugin_root(
        tmp_path,
        schema_version=4,
        extra_row_lines={
            "reviewer-calibration": [
                'eligible_glob = "snippets/*.md"',
                "[[snippet.reviewer-calibration.excluded_consumer]]",
                'path = "snippets/undeclared.md"',
                'reason = "sanctioned bespoke variant"',
            ]
        },
    )
    (plugin_root / "snippets" / "reviewer-calibration.md").write_text("x", encoding="utf-8")
    (plugin_root / "snippets" / "undeclared.md").write_text("x", encoding="utf-8")

    assert vrc.run(plugin_root).exit_code == 0


def test_list_checks_enumerates_all_snippets():
    lines = vrc.list_checks()
    assert lines[0].startswith("check:schema_version")
    assert lines[1].startswith("check:registry_exists")
    for name in vrc.SNIPPET_NAMES:
        assert any(f"[{name}]" in line for line in lines)


def test_list_checks_advertises_the_sibling_readers_version_set():
    """A version this reader accepts but does not advertise is how the last
    divergence hid. Both come off `_SUPPORTED_SCHEMA_VERSIONS`."""
    header = vrc.list_checks()[0]
    for version in registry._SUPPORTED_SCHEMA_VERSIONS:
        assert str(version) in header


def test_main_list_mode_returns_0(capsys):
    plugin_root_placeholder = "/nonexistent"  # --list never touches disk
    rc = vrc.main([plugin_root_placeholder, "--list"])
    assert rc == 0
    out = capsys.readouterr().out

    # `--list` is `list_checks()` verbatim, one line each — the check TEXT is
    # pinned by the two list_checks tests above, not restated here.
    assert out.splitlines() == vrc.list_checks()
    assert any(line.startswith("check:eligible_glob_complete") for line in out.splitlines())


def test_main_unknown_argument_exits_2(tmp_path, capsys):
    plugin_root = _write_plugin_root(tmp_path)
    rc = vrc.main([str(plugin_root), "--bogus"])
    assert rc == 2


def test_main_happy_path_exit_0(tmp_path):
    plugin_root = _write_plugin_root(tmp_path)
    rc = vrc.main([str(plugin_root)])
    assert rc == 0
