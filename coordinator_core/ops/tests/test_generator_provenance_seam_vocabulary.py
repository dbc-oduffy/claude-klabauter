
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


# --- declared-outside-staleness vocabulary: MUTATES_APPEND, GENERATES_EXTERNAL,
# UNSTAMPED_BY_DESIGN -------------------------------------------------------

import subprocess


def _git_commit_all(root: Path) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "a@b"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=str(root), check=True, capture_output=True, creationflags=flags)


def _record(tmp_path: Path, module_source: str, name: str = "declared"):
    _write(tmp_path, f"coordinator_core/{name}.py", module_source)
    _write(tmp_path, "state/ledger.md", "# ledger\n")
    _write(tmp_path, "src.txt", "x\n")
    _git_commit_all(tmp_path)
    matches = [r for r in discover_generators(tmp_path) if r.generator == f"coordinator_core/{name}.py"]
    assert len(matches) == 1
    return matches[0]


def test_mutates_append_concrete_path_is_declared_not_undeclared(tmp_path):
    record = _record(
        tmp_path,
        """
from pathlib import Path

MUTATES_APPEND = ["state/ledger.md"]

def run():
    Path("state/ledger.md").write_text("row\\n")
""",
    )
    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == ("state/ledger.md",)
    assert "MUTATES_APPEND" in record.detail


def test_mutates_append_may_sit_beside_wildcard_mutates(tmp_path):
    record = _record(
        tmp_path,
        """
from pathlib import Path

MUTATES = ["state/*.md"]
MUTATES_APPEND = ["state/ledger.md"]

def run():
    Path("state/ledger.md").write_text("row\\n")
""",
    )
    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == ("state/ledger.md", "state/*.md")


@pytest.mark.parametrize("bad", ['"state/ledger.md"', "[]", '["state/*.md"]', "[7]"])
def test_malformed_mutates_append_is_undeclared(tmp_path, bad):
    record = _record(
        tmp_path,
        f"""
from pathlib import Path

MUTATES_APPEND = {bad}

def run():
    Path("state/ledger.md").write_text("row\\n")
""",
    )
    assert record.verdict == Verdict.UNDECLARED
    assert "MUTATES_APPEND" in record.detail


def test_generates_external_without_generates_is_declared(tmp_path):
    record = _record(
        tmp_path,
        """
import json

GENERATES_EXTERNAL = True

def run(dest):
    with open(dest, "w") as fh:
        json.dump({}, fh)
""",
    )
    assert record.verdict == Verdict.MUTATES_DECLARED
    assert "GENERATES_EXTERNAL" in record.detail


def test_generates_external_must_be_literal_true(tmp_path):
    record = _record(
        tmp_path,
        """
GENERATES_EXTERNAL = "yes"

def run(dest):
    open(dest, "w").write("x")
""",
    )
    assert record.verdict == Verdict.UNDECLARED


def test_unstamped_by_design_drops_matching_pairs_from_comparison(tmp_path):
    _write(tmp_path, "state/audits/one.md", "x\n")
    record = _record(
        tmp_path,
        """
GENERATES = [
    {"artifact": "state/audits/one.md", "stamp_key": "generated_at", "sources": ["src.txt"]},
    {"artifact": "state/ledger.md", "stamp_key": "generated_at", "sources": ["src.txt"]},
]
UNSTAMPED_BY_DESIGN = ["state/audits/*.md"]
""",
    )
    assert [pair.artifact for pair in record.pairs] == ["state/ledger.md"]
    assert "UNSTAMPED_BY_DESIGN" in record.detail


def test_unstamped_by_design_does_not_exempt_a_pair_outside_its_globs(tmp_path):
    record = _record(
        tmp_path,
        """
GENERATES = [
    {"artifact": "state/ledger.md", "stamp_key": "generated_at", "sources": ["src.txt"]},
]
UNSTAMPED_BY_DESIGN = ["state/audits/*.md"]
""",
    )
    assert [pair.artifact for pair in record.pairs] == ["state/ledger.md"]


def test_undeclared_writer_still_refused_without_any_declaration(tmp_path):
    record = _record(
        tmp_path,
        """
from pathlib import Path

def run():
    Path("state/ledger.md").write_text("row\\n")
""",
    )
    assert record.verdict == Verdict.UNDECLARED


def test_memo_schema_generator_declares_its_semver_stamped_artifacts_unstamped():
    from fnmatch import fnmatch

    from coordinator_core.contract import emit_memo_schema

    artifacts = [pair["artifact"] for pair in emit_memo_schema.GENERATES]
    assert artifacts
    assert all(
        any(fnmatch(artifact, glob) for glob in emit_memo_schema.UNSTAMPED_BY_DESIGN)
        for artifact in artifacts
    )
