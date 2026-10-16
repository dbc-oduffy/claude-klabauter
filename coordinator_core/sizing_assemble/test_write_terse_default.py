"""`sizing-assemble --write` prints only `written: <path>` by default; `--json` keeps the decision."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes

_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS
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

_ARGS = ["--tshirt", "M", "--intent", "Ship the thing", "--premise-provenance", "read",
         "--premise-evidence", "read it", "--no-plan-hand-off", "--no-verbatims"]


@pytest.fixture()
def target(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    path = Path(record_homes.record_path(str(tmp_path), "sizings", "2026-10-11-x.yaml"))
    path.write_text(_DRAFT, encoding="utf-8")
    return path


def test_write_prints_only_written_line(target, capsys):
    code = sizing_assemble.main([*_ARGS, "--write", str(target)])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("written: ")
    assert out.strip() == f"written: {Path(record_homes.record_path('', 'sizings', '2026-10-11-x.yaml')).as_posix()}"
    assert len(out.strip().splitlines()) == 1


def test_json_flag_keeps_the_decision(target, capsys):
    code = sizing_assemble.main([*_ARGS, "--write", str(target), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert "write" in json.loads(out)


def test_no_write_output_is_the_decision(capsys):
    code = sizing_assemble.main(["--tshirt", "M", "--premise-provenance", "read", "--premise-evidence", "x"])
    assert code == 0
    assert "route" in json.loads(capsys.readouterr().out)
