"""A landing the ceremony split into two commits is recorded by passing both shas, in ``shas`` alone or across ``sha`` + ``shas``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"
REGISTER = "state/sizings/register.yaml"
SCRIPT = "scripts/tool.py"
TEST = "scripts/tests/test_tool.py"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit_files(repo: Path, files: dict) -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "files")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def split(tmp_path):
    """A halted solo run withholding three paths that two commits landed: the register, then the script and its test."""
    _git(tmp_path, "init", "-q")
    _commit_files(tmp_path, {"seed": "s"})
    d = tmp_path / "state" / "warp" / SOLO
    d.mkdir(parents=True)
    marker = d / "halted.json"
    marker.write_text(json.dumps({
        "status": "halted", "halted": "x", "sizing_rel": None,
        "run_artifacts": {"withheld": {p: "r" for p in (REGISTER, SCRIPT, TEST)}},
    }), encoding="utf-8")
    register = _commit_files(tmp_path, {REGISTER: "r"})
    code = _commit_files(tmp_path, {SCRIPT: "s", TEST: "t"})
    return tmp_path, marker, register, code


def _status(marker: Path) -> str:
    return json.loads(marker.read_text(encoding="utf-8"))["status"]


@pytest.mark.parametrize("params", [
    lambda a, b: {"shas": [a, b]},
    lambda a, b: {"shas": [b, a]},
    lambda a, b: {"sha": a, "shas": [b]},
    lambda a, b: {"sha": b, "shas": [a[:10]]},
])
def test_both_split_shas_record_the_run_landed(split, params):
    root, marker, register, code = split
    out = op._handler({"run_id": SOLO, **params(register, code)}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == code
    assert sorted(out["shas"]) == sorted([register, code])
    assert out["covers"] == {register: [REGISTER], code: sorted([SCRIPT, TEST])}
    assert _status(marker) == "landed"


@pytest.mark.parametrize("which", [0, 1])
def test_either_split_sha_alone_refuses_and_leaves_the_run_halted(split, which):
    root, marker, register, code = split
    before = marker.read_bytes()
    out = op._handler({"run_id": SOLO, "sha": (register, code)[which]}, root)
    assert not out["ok"] and "did not change withheld" in out["error"]
    assert marker.read_bytes() == before and _status(marker) == "halted"


def test_a_list_in_sha_is_still_malformed(split):
    root, _, register, code = split
    out = op._handler({"run_id": SOLO, "sha": [register, code]}, root)
    assert not out["ok"] and "lowercase hex sha" in out["error"]


def test_a_sha_that_changed_no_withheld_path_is_named_and_refused(split):
    root, marker, register, code = split
    extra = _commit_files(root, {"other.txt": "o"})
    out = op._handler({"run_id": SOLO, "shas": [register, code, extra]}, root)
    assert not out["ok"] and extra in out["error"] and _status(marker) == "halted"


def test_sibling_run_records_the_split_landing(split):
    root, _, register, code = split
    run = c.SiblingRun("A", "A", (), "state/sizings/A.yaml", c.STATUS_READY)
    parent = "ask-parent-20261015T000000-abcdef"
    rec = root / c.parent_record_rel(parent)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(parent, (run,)).to_json()), encoding="utf-8")
    st = root / c.status_rel("A")
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"parent_id": parent, "status": "halted"}), encoding="utf-8")
    d = root / "state" / "warp" / "A"
    d.mkdir(parents=True, exist_ok=True)
    (d / "halted.json").write_text(json.dumps({
        "status": "halted", "halted": "x", "plan": "state/plans/p.md",
        "run_artifacts": {"withheld": {p: "r" for p in (REGISTER, SCRIPT, TEST)}},
    }), encoding="utf-8")
    out = op._handler({"run_id": "A", "shas": [register, code]}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == code
    assert json.loads(st.read_text(encoding="utf-8"))["status"] == "landed"
