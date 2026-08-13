"""
Tests for coordinator_core.ops.generator_provenance.

Negative-spec: fixture modules under `tmp_path` only — never the real repo's
generator modules — and discovery is never exercised via import (see the
`sys.modules` assertion below), matching AC6.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coordinator_core.ops.generator_provenance import discover_generators
from coordinator_core.ops.staleness_git import Verdict


def _write(root: Path, rel_path: str, content: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_declared_generator_resolves_its_pairs(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x", encoding="utf-8")

    _write(
        tmp_path,
        "coordinator_core/gen_declared.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_declared.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert len(record.pairs) == 1
    pair = record.pairs[0]
    assert pair.artifact == "out.json"
    assert pair.stamp_key == "generated_at"
    assert pair.sources == ("src.txt",)


def test_undeclared_generator_reports_undeclared_not_skipped(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_undeclared.py",
        """
from pathlib import Path

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_undeclared.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_generates_empty_list_is_declared_empty_not_undeclared(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_empty.py",
        """
from pathlib import Path

GENERATES = []

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_empty.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert record.pairs == ()


def test_known_writer_with_no_generates_is_named_undeclared_by_ast_sweep(tmp_path):
    _write(
        tmp_path,
        "bin/write_surface_manifest.py",
        """
import json

def run():
    with open("manifest.json", "w") as fh:
        json.dump({}, fh)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "bin/write_surface_manifest.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "write_surface_manifest.py" in matches[0].detail


def test_sweep_imports_nothing(tmp_path):
    module_name = "gen_should_never_be_imported"
    before = set(sys.modules)

    _write(
        tmp_path,
        f"coordinator_core/{module_name}.py",
        """
from pathlib import Path

raise RuntimeError("this module must never be imported by the sweep")

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    discover_generators(tmp_path)

    assert module_name not in sys.modules
    assert set(sys.modules) - before == set()


def test_ac10_malformed_sources_variants_resolve_to_undeclared(tmp_path):
    (tmp_path / "present.txt").write_text("x", encoding="utf-8")

    _write(
        tmp_path,
        "coordinator_core/gen_empty_sources.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": []},
]

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(
        tmp_path,
        "coordinator_core/gen_notalist_sources.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": "present.txt"},
]

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(
        tmp_path,
        "coordinator_core/gen_absent_source.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["does-not-exist.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    by_name = {r.generator: r for r in records}

    assert by_name["coordinator_core/gen_empty_sources.py"].verdict == Verdict.UNDECLARED
    assert by_name["coordinator_core/gen_notalist_sources.py"].verdict == Verdict.UNDECLARED
    assert by_name["coordinator_core/gen_absent_source.py"].verdict == Verdict.UNDECLARED
