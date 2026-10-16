"""Tests for `sizing-assemble --acceptance-rows-json`: the rows land as a schema-valid top-level array."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops.dispatch_emit.sizing_m_mint import concrete_acceptance
from coordinator_core.session import record_homes

_NAME = "2026-10-08-a.yaml"
_SCHEMA = (
    Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_DRAFT = """schema: sizing-object
intent: "Add the thing"
estimate:
  tshirt: M
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""
_ROWS = [
    {"statement": "The flag parses", "writes": ["pkg/cli.py", "pkg/tests/test_cli.py"], "check": "python -m pytest pkg/tests/test_cli.py -q"},
    {"statement": "Docs name it", "writes": ["docs/cli.md"], "check": "grep -q flag docs/cli.md"},
]


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(repo: Path, *extra: str) -> tuple[int, dict]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    code = sa.main(["--tshirt", "M", "--interaction-mode", "pm", "--write", rel, *extra])
    return code, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_rows_are_written_validate_and_pass_concrete_acceptance(repo: Path) -> None:
    code, doc = _run(repo, "--acceptance-rows-json", json.dumps(_ROWS))
    assert code == sa.EXIT_OK
    assert doc["acceptance_rows"] == _ROWS
    assert validate_frontmatter(doc, _SCHEMA) == []
    assert concrete_acceptance(doc) == _ROWS


def test_absent_flag_writes_no_key(repo: Path) -> None:
    code, doc = _run(repo)
    assert code == sa.EXIT_OK
    assert "acceptance_rows" not in doc


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "{}",
        "[]",
        json.dumps(["x"]),
        json.dumps([{"statement": "s", "writes": [], "check": "c"}]),
        json.dumps([{"statement": "", "writes": ["a"], "check": "c"}]),
        json.dumps([{"statement": "s", "writes": ["a"], "check": " "}]),
        json.dumps([{"statement": "s", "writes": ["a"], "check": "c", "extra": 1}]),
    ],
)
def test_malformed_value_is_refused(repo: Path, bad: str) -> None:
    code, doc = _run(repo, "--acceptance-rows-json", bad)
    assert code == sa.EXIT_USAGE
    assert "acceptance_rows" not in doc
