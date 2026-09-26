
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.cartography_chunk_table  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_chunk_table import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_OVERSIZED,
    build_chunk_table_artifact,
)
from coordinator_core.cartography.chunk_table import compute_chunk_table
from coordinator_core.win_portability import no_console_passthrough_kwargs

_OP_NAME = "cartography.chunk_table"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_chunk_table @register_op did not fire"
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, **no_console_passthrough_kwargs())

    (root / "systemA").mkdir()
    (root / "systemA" / "big.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    (root / "systemA" / "small.py").write_text("a\nb\n", encoding="utf-8")
    (root / "systemA" / "binary.py").write_bytes(b"\xff\xfe\x00\x01not-utf8\n")

    subprocess.run(["git", "add", "-A"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, **no_console_passthrough_kwargs())
    return root


_SYSTEMS = {"systemA": ["systemA"]}


def test_absent_threshold_no_file_opened_and_matches_pre_param_shape(git_repo, monkeypatch):
    opened: list[Path] = []
    import coordinator_core.ops.cartography_chunk_table as mod

    original = mod._loc_for

    def spy(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(mod, "_loc_for", spy)

    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )

    assert opened == []
    assert "oversized" not in artifact
    assert artifact["schema_version"] == SCHEMA_VERSION


def test_absent_threshold_byte_identical_to_no_oversized_support(git_repo):
    with_default = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    explicit_none = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=None
    )
    assert json.dumps(with_default, indent=2) == json.dumps(explicit_none, indent=2)


def test_threshold_at_boundary_included(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert "systemA/big.py" in artifact["oversized"]


def test_loc_strictly_above_threshold_included(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=3
    )
    assert "systemA/big.py" in artifact["oversized"]


def test_loc_below_threshold_excluded(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert "systemA/small.py" not in artifact["oversized"]


def test_threshold_one_above_max_loc_excludes_everything(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=6
    )
    assert artifact["oversized"] == []


def test_none_loc_file_never_oversized(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=1
    )
    assert "systemA/binary.py" not in artifact["oversized"]
    assert "systemA/big.py" in artifact["oversized"]
    assert "systemA/small.py" in artifact["oversized"]


def test_oversized_is_sorted(git_repo):
    (git_repo / "systemA" / "zzz_big.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    (git_repo / "systemA" / "aaa_big.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "more"], cwd=git_repo, check=True, **no_console_passthrough_kwargs())

    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert artifact["oversized"] == sorted(artifact["oversized"])
    assert "systemA/aaa_big.py" in artifact["oversized"]
    assert "systemA/zzz_big.py" in artifact["oversized"]


def test_schema_version_unchanged_when_threshold_absent(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert artifact["schema_version"] == SCHEMA_VERSION


def test_schema_version_bumps_when_threshold_supplied(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert artifact["schema_version"] == SCHEMA_VERSION_OVERSIZED
    assert SCHEMA_VERSION_OVERSIZED != SCHEMA_VERSION


def test_schema_version_stays_first_key_absent_threshold(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert next(iter(artifact)) == "schema_version"


def test_schema_version_stays_first_key_with_threshold(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert next(iter(artifact)) == "schema_version"


def test_absent_threshold_byte_identical_to_independently_constructed_baseline(git_repo):
    result = compute_chunk_table(git_repo, _SYSTEMS, 10)
    pre_param_baseline = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "run1",
        "target_root": str(git_repo),
        "systems": dict(_SYSTEMS),
        "chunk_size": 10,
        "buckets": result.buckets,
        "unbucketed": result.unbucketed,
        "counts": result.counts,
    }
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert json.dumps(artifact, indent=2) == json.dumps(pre_param_baseline, indent=2)
