"""close_out_and_stamp codes still-open rows that earlier fires' same-plan checkpoints landed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.frontmatter.body_blocks import locate_fenced_block

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN = "plan.md"
OTHER = "other.md"
OPEN_IDS = ["B1", "C2", "B3", "B4", "G1", "G2", "N1", "P1"]

_HEAD = """---
title: "Fixture plan - earlier fire rows"
created: 2026-09-01
author: test-fixture
status: executing
branch: "work/test/2026-09-01"
plan_id: "pln-fixture-earlier-fire-000001"
deliverable_id: "dlv-fixture-earlier-fire-000001"
---

# Fixture plan

## Tasks

```yaml plan-tasks
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, env=dict(os.environ),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, subject: str, trailers: str = "") -> str:
    name = f"f{len(list(root.glob('f*.txt')))}.txt"
    (root / name).write_text(subject + "\n")
    _git(root, "add", name)
    args = ["commit", "-qm", subject]
    if trailers:
        args += ["-m", trailers]
    _git(root, *args)
    return _git(root, "rev-parse", "HEAD")


def _cp(root, ids, plan=PLAN, base="", owed="", process=False, wave=0) -> str:
    subject = f"checkpoint(wave {wave}): {len(ids)} rows — {', '.join(ids)}"
    if process:
        subject += " (process files)"
    trailers = f"Checkpoint-Plan: {plan}"
    if base:
        trailers += f"\nCheckpoint-Base: {base[:7]}"
    if owed:
        trailers += f"\nCheckpoint-Gate-Owed: {owed}"
    return _commit(root, subject, trailers)


def _row(rid: str, ref: str | None = None) -> str:
    out = (
        f"- id: {rid}\n  title: Row {rid}\n  change_kind: code-edit\n"
        f"  surface: src/{rid}.py\n  deferred: false\n"
    )
    if ref:
        out += f"  disposition: coded\n  disposition_ref: {ref}\n  disposition_detail: landed\n"
    else:
        out += "  disposition: open\n"
    return out + f"  body: |\n    Do {rid}.\n"


def _seed(root: Path, open_ids: list[str], coded_ids: list[str]) -> str:
    """Init the repo, land a base commit, then the plan; returns the base sha."""
    _git(root, "init", "-q")
    base = _commit(root, "base")
    rows = "".join(_row(r, base) for r in coded_ids) + "".join(_row(r) for r in open_ids)
    (root / PLAN).write_text(_HEAD + rows + "```\n", encoding="utf-8", newline="\n")
    _git(root, "add", PLAN)
    _git(root, "commit", "-qm", "seed plan")
    return base


def _example_game_repo_shape(root: Path) -> dict:
    base = _seed(root, OPEN_IDS, ["S1", "V1"])
    s = {"base": base}
    s["b3"] = _cp(root, ["B3", "G1", "G2"], base=base, wave=0)
    _commit(root, "non-run commit")
    _cp(root, ["P1"], plan=OTHER)
    peer_halt = _commit(root, "warp: record halted run artifacts", "Halt-Plan: peer.md")
    s["p1"] = _cp(root, ["P1"], base=peer_halt, wave=1)
    s["b"] = _cp(root, ["B1", "C2", "G1", "G2", "N1"], base=s["p1"], wave=2)
    _cp(root, ["B1", "C2", "G1", "G2", "N1"], process=True, wave=2)
    halt_a = _commit(root, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    s["b4"] = _cp(root, ["B4"], base=halt_a, wave=3)
    _commit(root, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    v1 = _cp(root, ["V1"], wave=4)
    _cp(root, ["V1"], process=True, wave=4)
    _commit(root, "mark 1 rows coded", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Stamp: {v1}")
    _cp(root, ["S1", "N1", "P1"], base=halt_a, owed="N1, P1", wave=5)
    _commit(root, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    _commit(root, "terminal")
    return s


def _rows(root: Path) -> dict:
    text = (root / PLAN).read_text(encoding="utf-8")
    body = locate_fenced_block(text).body
    return {str(r["id"]): r for r in yaml.safe_load(body)}


def _status(root: Path) -> str:
    for line in (root / PLAN).read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no status")


def _run(monkeypatch, root: Path, dry_run: bool = False):
    monkeypatch.chdir(root)
    return coas.close_out_and_stamp(PLAN, repo_root=root, dry_run=dry_run)


def test_close_out_codes_eight_rows_at_their_checkpoints_and_stamps_implemented(tmp_path, monkeypatch):
    s = _example_game_repo_shape(tmp_path)
    pre = _git(tmp_path, "rev-parse", "HEAD")
    code, result = _run(monkeypatch, tmp_path)
    assert code == coas.EXIT_OK, result
    assert result["missing_chunk_ids"] == []
    assert result["shipped"] is True
    assert result["status_target"] == "implemented", result["message"]
    assert _status(tmp_path) == "implemented"
    assert {k: set(v) for k, v in result["checkpoint_rows_coded"].items()} == {
        s["b4"]: {"B4"},
        s["b"]: {"B1", "C2", "G1", "G2", "N1"},
        s["p1"]: {"P1"},
        s["b3"]: {"B3"},
    }
    rows = _rows(tmp_path)
    expect = {"B4": s["b4"], "P1": s["p1"], "B3": s["b3"]}
    expect.update({r: s["b"] for r in ("B1", "C2", "G1", "G2", "N1")})
    for rid, sha in expect.items():
        assert rows[rid]["disposition"] == "coded"
        assert rows[rid]["disposition_ref"] == sha
    assert int(_git(tmp_path, "rev-list", "--count", f"{pre}..HEAD")) == 1


def test_dry_run_reports_the_same_verdict_and_leaves_the_plan_bytes_alone(tmp_path, monkeypatch):
    s = _example_game_repo_shape(tmp_path)
    before = (tmp_path / PLAN).read_bytes()
    head = _git(tmp_path, "rev-parse", "HEAD")
    code, result = _run(monkeypatch, tmp_path, dry_run=True)
    assert code == coas.EXIT_OK, result
    assert result["missing_chunk_ids"] == []
    assert result["status_target"] == "implemented"
    assert set(result["checkpoint_rows_coded"]) == {s["b4"], s["b"], s["p1"], s["b3"]}
    assert (tmp_path / PLAN).read_bytes() == before
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_a_gate_owed_only_landing_stays_missing_and_is_named(tmp_path, monkeypatch):
    _seed(tmp_path, ["A1", "Z1"], [])
    a1 = _cp(tmp_path, ["A1", "Z1"], owed="Z1")
    _commit(tmp_path, "terminal")
    code, result = _run(monkeypatch, tmp_path)
    assert code == coas.EXIT_OK, result
    assert result["missing_chunk_ids"] == ["Z1"]
    assert result["status_target"] is None
    assert result["checkpoint_rows_coded"] == {a1: ["A1"]}
    assert "1 chunk(s) still uncommitted (Z1)" in _git(tmp_path, "log", "-1", "--format=%s")
    assert _status(tmp_path) == "executing"
    assert _rows(tmp_path)["Z1"]["disposition"] == "open"


def test_no_checkpoints_leaves_the_partial_path_untouched(tmp_path, monkeypatch):
    _seed(tmp_path, ["A1", "Z1"], [])
    _commit(tmp_path, "unrelated")
    spine_before = locate_fenced_block((tmp_path / PLAN).read_text(encoding="utf-8")).body
    code, result = _run(monkeypatch, tmp_path)
    assert code == coas.EXIT_OK, result
    assert result["missing_chunk_ids"] == ["A1", "Z1"]
    assert result["checkpoint_rows_coded"] == {}
    assert result["checkpoint_walk"]["capped"] is False
    assert "2 chunk(s) still uncommitted" in result["message"]
    assert locate_fenced_block((tmp_path / PLAN).read_text(encoding="utf-8")).body == spine_before


def test_the_checkpoint_step_spawns_no_process(tmp_path, monkeypatch, record_property):
    import time

    s = _example_game_repo_shape(tmp_path)
    text = (tmp_path / PLAN).read_text(encoding="utf-8")
    rows, _ = coas._parse_spine_rows(text, PLAN)
    from coordinator_core.ops.dispatch_emit import land_run  # noqa: F401 -- pre-import

    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    started = time.process_time()
    new_text, coded, walk = coas._code_rows_from_checkpoints(text, PLAN, tmp_path, rows, OPEN_IDS)
    record_property("process_ms", round((time.process_time() - started) * 1000, 1))
    assert set(coded) == {s["b4"], s["b"], s["p1"], s["b3"]}
    assert new_text != text
    assert walk["capped"] is False
