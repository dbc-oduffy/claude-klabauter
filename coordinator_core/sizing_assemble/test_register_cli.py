"""
coordinator_core.sizing_assemble.test_register_cli — `sizing-assemble --register`.

Run (from repo root):
    python3 -m pytest coordinator_core/sizing_assemble/test_register_cli.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa

_REL = "state/sizings/20260101-a.yaml"
_SIZING = (
    "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XL\n"
    "  provisional: true\nroute: pm-decision\ndetents: []\nfork: null\nxl_exit: null\n"
    "status: routed\npremise:\n  provenance: read\n  evidence: fixture\n"
)
_REGISTER = (
    "sources:\n  - doc: docs/x.md\n    ref: abc\nrows:\n"
    "  - id: a\n    source_text: text a\n    surface: cli\n    status: open\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _REL).write_text(_SIZING, encoding="utf-8")
    (tmp_path / "reg.yaml").write_text(_REGISTER, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_register_with_write_exits_zero_and_carries_register(repo):
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    doc = yaml.safe_load((repo / _REL).read_text(encoding="utf-8"))
    assert doc["requirement_register"]["rows"][0]["id"] == "a"
    assert doc["requirement_register"]["rollup"]["open"] == 1


def test_register_without_write_is_usage(repo):
    assert sa.main(["--register", "reg.yaml"]) == sa.EXIT_USAGE


def test_register_with_pm_resolution_is_usage(repo):
    argv = ["--register", "reg.yaml", "--pm-resolution", "k", "--pm-quote", "q", "--write", _REL]
    assert sa.main(argv) == sa.EXIT_USAGE


def test_register_with_xl_exit_is_usage(repo):
    argv = ["--register", "reg.yaml", "--xl-exit", "shape", "--pm-quote", "q", "--write", _REL]
    assert sa.main(argv) == sa.EXIT_USAGE


def test_missing_yaml_is_usage(repo):
    assert sa.main(["--register", "nope.yaml", "--write", _REL]) == sa.EXIT_USAGE


def test_invalid_register_exits_one(repo):
    (repo / "reg.yaml").write_text(_REGISTER.replace("cli", "bogus"), encoding="utf-8")
    before = (repo / _REL).read_bytes()
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == sa.EXIT_BUSINESS_FAIL
    assert (repo / _REL).read_bytes() == before


def test_register_twice_is_idempotent_and_supersede_is_accepted(repo):
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    assert sa.main(["--register", "reg.yaml", "--supersede", "--write", _REL]) == 0
