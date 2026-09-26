
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.generator_provenance import discover_generators
from coordinator_core.ops.staleness_git import Verdict

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_SEAM_NAMES = (
    "replace_text",
    "replace_bytes",
    "create_exclusive",
    "append_claimed_line",
)


def _write(root: Path, rel_path: str, content: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_seam_call_reached_as_module_attribute_is_discovered(tmp_path, seam_name):
    _write(
        tmp_path,
        f"coordinator_core/gen_attr_{seam_name}.py",
        f"""
from coordinator_core.session import claimed_write

def run():
    claimed_write.{seam_name}("out_{seam_name}.txt", b"data")
""",
    )

    records = discover_generators(tmp_path)
    matches = [
        r for r in records if r.generator == f"coordinator_core/gen_attr_{seam_name}.py"
    ]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict == Verdict.UNDECLARED


@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_seam_call_reached_as_from_import_is_discovered(tmp_path, seam_name):
    _write(
        tmp_path,
        f"coordinator_core/gen_import_{seam_name}.py",
        f"""
from coordinator_core.session.claimed_write import {seam_name}

def run():
    {seam_name}("out_{seam_name}.txt", b"data")
""",
    )

    records = discover_generators(tmp_path)
    matches = [
        r for r in records if r.generator == f"coordinator_core/gen_import_{seam_name}.py"
    ]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict == Verdict.UNDECLARED


def test_unrelated_local_function_named_append_claimed_line_is_not_a_seam_write(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_local_shadow.py",
        """
def append_claimed_line(path, encoded):
    return path

def run():
    append_claimed_line("not_a_write.txt", b"data")
""",
    )

    records = discover_generators(tmp_path)
    matches = [
        r for r in records if r.generator == "coordinator_core/gen_local_shadow.py"
    ]
    assert matches == []
