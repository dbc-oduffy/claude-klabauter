"""Tests for coordinator_core.snippet_sync.verify_registry_consistency.

The four-script (`verify-<X>-sync.sh`) leg this module once ported from the
retired `coordinator/bin` bash oracle (721 LoC, deleted at example-doctrine-repo's `93887f6f`
de-bash cutover) was retired 2026-07-22 — see the module docstring and the
actioned inbound memo
`cross-repo/inbox/2026-07-22-claude-central-em-snippet-registry-consistency-fix-locus.md`.
Remaining coverage below exercises only the surviving consistency checks:
registry-exists, TOML-parse, schema_version gate, and per-snippet
`[snippet.<name>]` enrollment.

Spec backlink: example-doctrine-repo docs/plans/2026-06-15-snippet-sync-consumer-registry.md § Dispatch Ledger C4, C8
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.snippet_sync import verify_registry_consistency as vrc


def _write_plugin_root(
    tmp_path: Path,
    *,
    schema_version: object = 2,
    enrolled: list[str] | None = None,
    omit_schema_version: bool = False,
) -> Path:
    """Build a synthetic plugin_root tree: snippets/registry.toml only."""
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "snippets").mkdir(parents=True)

    enrolled = vrc.SNIPPET_NAMES if enrolled is None else enrolled

    toml_lines = []
    if not omit_schema_version:
        toml_lines.append(f"schema_version = {schema_version}")
    for name in enrolled:
        toml_lines.append(f"[snippet.{name}]")
        toml_lines.append('sentinel_begin = "b"')
        toml_lines.append('sentinel_end = "e"')
        toml_lines.append(f'consumers = ["snippets/{name}.md"]')
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


def test_list_checks_enumerates_all_snippets():
    lines = vrc.list_checks()
    assert lines[0].startswith("check:schema_version")
    assert lines[1].startswith("check:registry_exists")
    for name in vrc.SNIPPET_NAMES:
        assert any(f"[{name}]" in line for line in lines)


def test_main_list_mode_returns_0(capsys):
    plugin_root_placeholder = "/nonexistent"  # --list never touches disk
    rc = vrc.main([plugin_root_placeholder, "--list"])
    assert rc == 0
    out = capsys.readouterr().out

    expected_lines = [
        "check:schema_version — registry.toml schema_version ∈ {1,2} (exit 3 on unknown/higher version)",
        "check:registry_exists — registry.toml exists on disk",
    ]
    for name in vrc.SNIPPET_NAMES:
        expected_lines.append(
            f"check:enrollment[{name}] — snippet enrolled as [snippet.{name}] in registry.toml"
        )

    assert out.splitlines() == expected_lines


def test_main_unknown_argument_exits_2(tmp_path, capsys):
    plugin_root = _write_plugin_root(tmp_path)
    rc = vrc.main([str(plugin_root), "--bogus"])
    assert rc == 2


def test_main_happy_path_exit_0(tmp_path):
    plugin_root = _write_plugin_root(tmp_path)
    rc = vrc.main([str(plugin_root)])
    assert rc == 0
