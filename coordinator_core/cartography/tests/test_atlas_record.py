"""
coordinator_core.cartography.tests.test_atlas_record — tests for atlas_record.

Spec backlink: docs/plans/2026-08-06-churn-emergent-detection-file-granularity.md
§ chunk C2.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.cartography.atlas_record import (
    ATLAS_UNREADABLE,
    RecordedAtlas,
    expand_recorded_mapping,
    is_source_candidate,
    load_recorded_atlas,
    recorded_system_for_path,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def real_atlas() -> RecordedAtlas:
    atlas = load_recorded_atlas(REPO_ROOT)
    assert atlas.error is None
    return atlas


# --- is_source_candidate -----------------------------------------------

@pytest.mark.parametrize(
    "relpath,expected",
    [
        ("coordinator_core/ipc.py", True),
        ("coordinator_core/ops/emit/foo.js", True),
        ("archive/x.py", False),
        ("tasks/x.py", False),
        ("dist/x.py", False),
        ("pip/x.py", False),
        ("coordinator_core/conftest.py", False),
        ("coordinator_core/test_foo.py", False),
        ("coordinator_core/foo_test.py", False),
        ("coordinator_core/foo.test.js", False),
        ("coordinator_core/cartography/tests/test_atlas_record.py", False),
        ("coordinator_core/cartography/tests/helpers.py", False),
        ("coordinator_core/foo.txt", False),
        ("coordinator_core/foo.md", False),
        ("README.md", False),
    ],
)
def test_is_source_candidate(relpath: str, expected: bool) -> None:
    assert is_source_candidate(relpath) is expected


# --- recorded_system_for_path (rule ordering / dispatch) ----------------

def test_rule4_wins_over_rule9_emit_engine(real_atlas: RecordedAtlas) -> None:
    assert (
        recorded_system_for_path("coordinator_core/ops/emit/sections/_shared.py", real_atlas)
        == "emit-engine"
    )


def test_rule9_ops_flat_fallback(real_atlas: RecordedAtlas) -> None:
    assert (
        recorded_system_for_path("coordinator_core/ops/some_flat_op.py", real_atlas)
        == "ops-flat"
    )


def test_rule11_assemblers_suffix(real_atlas: RecordedAtlas) -> None:
    assert (
        recorded_system_for_path("coordinator_core/pickup_assemble/x.py", real_atlas)
        == "assemblers"
    )
    assert (
        recorded_system_for_path("coordinator_core/workstream_complete/y.py", real_atlas)
        == "assemblers"
    )


def test_rule12_engine_runtime_flat_top_level(real_atlas: RecordedAtlas) -> None:
    assert recorded_system_for_path("coordinator_core/ipc.py", real_atlas) == "engine-runtime"


def test_rule1_state_not_a_system(real_atlas: RecordedAtlas) -> None:
    assert recorded_system_for_path("state/audits/x.py", real_atlas) is None


def test_rule2_operator_cli(real_atlas: RecordedAtlas) -> None:
    assert recorded_system_for_path("coordinator/bin/foo.py", real_atlas) == "operator-cli"
    assert recorded_system_for_path("bin/foo.py", real_atlas) == "operator-cli"


def test_rule3_install_substrate(real_atlas: RecordedAtlas) -> None:
    assert recorded_system_for_path("scripts/setup.py", real_atlas) == "install-substrate"


def test_rule10_package_table(real_atlas: RecordedAtlas) -> None:
    assert (
        recorded_system_for_path("coordinator_core/cartography/atlas_record.py", real_atlas)
        == "cartography"
    )


def test_uncatalogued_when_no_rule_covers(real_atlas: RecordedAtlas) -> None:
    assert recorded_system_for_path("coordinator_core/some_new_pkg/x.py", real_atlas) is None


# --- load_recorded_atlas --------------------------------------------------

def test_load_recorded_atlas_real_repo(real_atlas: RecordedAtlas) -> None:
    assert real_atlas.error is None
    assert real_atlas.rules
    assert real_atlas.last_mapped == "2026-08-06"
    assert real_atlas.system_files["cartography"] == 7


def test_load_recorded_atlas_missing(tmp_path: Path) -> None:
    atlas = load_recorded_atlas(tmp_path)
    assert atlas.error == ATLAS_UNREADABLE
    assert atlas.error_detail
    assert atlas.rules == ()
    assert atlas.package_systems == {}
    assert atlas.system_files == {}
    assert atlas.last_mapped is None


# --- expand_recorded_mapping ----------------------------------------------

def test_expand_recorded_mapping_small_list(real_atlas: RecordedAtlas) -> None:
    tracked = [
        "coordinator_core/cartography/atlas_record.py",  # recorded package -> by_system
        "coordinator_core/some_new_pkg/x.py",  # not in recorded table -> uncatalogued
        "archive/should_be_excluded.py",  # excluded -> neither, not counted
    ]
    expansion = expand_recorded_mapping(tracked, real_atlas)

    assert expansion.by_system.get("cartography") == (
        "coordinator_core/cartography/atlas_record.py",
    )
    assert "coordinator_core/some_new_pkg/x.py" in expansion.uncatalogued
    assert "archive/should_be_excluded.py" not in expansion.uncatalogued
    for paths in expansion.by_system.values():
        assert "archive/should_be_excluded.py" not in paths
    assert expansion.considered_count == 2
