"""Tests for the baton.close_report header reader and successor index."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from coordinator_core.ops import baton_close_report_readers as readers
from coordinator_core.ops.baton_close_report_contract import HEADER_KEYS

REL = "state/handoffs"


def _write(root: Path, name: str, text: str, newline: str = "\n") -> None:
    d = root / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(text.replace("\n", newline).encode("utf-8"))


def _baton(status: str = "open", extra: str = "") -> str:
    return f"---\ntitle: t\nstatus: {status}\npredecessor: none\n{extra}---\n\nbody\nstatus: shipped\n"


def test_lf_and_crlf_parse_identically(tmp_path: Path) -> None:
    extra = 'governing_plan: docs/plans/p.md  # c\nsizing_object: "state/sizings/s.yaml"\nroadmap_id: rm-1\norigin_plan_id: pln-x\n'
    _write(tmp_path, "a.md", _baton(extra=extra))
    _write(tmp_path, "b.md", _baton(extra=extra), newline="\r\n")
    headers, unreadable = readers.read_baton_headers(str(tmp_path))
    assert unreadable == []
    a, b = headers[f"{REL}/a.md"], headers[f"{REL}/b.md"]
    assert {k: v for k, v in a.items() if k != "path"} == {k: v for k, v in b.items() if k != "path"}
    assert a["governing_plan"] == "docs/plans/p.md"
    assert a["sizing_object"] == "state/sizings/s.yaml"
    assert a["roadmap_id"] == "rm-1"
    assert a["origin_plan_id"] == "pln-x"
    assert a["predecessor"] is None
    assert set(a) == set(HEADER_KEYS)


def test_deliverable_id_and_kind_read_and_default_none(tmp_path: Path) -> None:
    _write(tmp_path, "d.md", _baton(extra='deliverable_id: "dlv-x"\nkind: spinoff\n'))
    _write(tmp_path, "n.md", _baton(extra="deliverable_id: null\n"))
    _write(tmp_path, "z.md", _baton())
    headers, _ = readers.read_baton_headers(str(tmp_path))
    d = headers[f"{REL}/d.md"]
    assert (d["deliverable_id"], d["kind"]) == ("dlv-x", "spinoff")
    for name in ("n", "z"):
        h = headers[f"{REL}/{name}.md"]
        assert (h["deliverable_id"], h["kind"]) == (None, None)


def test_goal_ids_scalar_inline_list_and_block_list(tmp_path: Path) -> None:
    _write(tmp_path, "s.md", _baton(extra='origin_goal_id: "goal-one"\n'))
    _write(tmp_path, "i.md", _baton(extra="origin_goal_id: [goal-a, goal-b]\n"))
    _write(
        tmp_path,
        "b.md",
        _baton(extra='origin_goal_id:\n  - "goal-x"\n  - goal-y\nhandoff_id: h\n'),
    )
    _write(tmp_path, "n.md", _baton(extra="origin_goal_id: null\n"))
    headers, _ = readers.read_baton_headers(str(tmp_path))
    assert headers[f"{REL}/s.md"]["origin_goal_id"] == ["goal-one"]
    assert headers[f"{REL}/i.md"]["origin_goal_id"] == ["goal-a", "goal-b"]
    assert headers[f"{REL}/b.md"]["origin_goal_id"] == ["goal-x", "goal-y"]
    assert headers[f"{REL}/n.md"]["origin_goal_id"] == []


def test_missing_closing_fence_is_unreadable(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", "---\ntitle: t\nstatus: open\nno fence here\n")
    _write(tmp_path, "nofm.md", "no frontmatter\n")
    _write(tmp_path, "nostatus.md", "---\ntitle: t\n---\n")
    _write(tmp_path, "ok.md", _baton())
    headers, unreadable = readers.read_baton_headers(str(tmp_path))
    assert set(unreadable) == {f"{REL}/bad.md", f"{REL}/nofm.md", f"{REL}/nostatus.md"}
    assert list(headers) == [f"{REL}/ok.md"]


def test_terminal_status_excluded_and_claimed_kept(tmp_path: Path) -> None:
    for name, st in (("o", "open"), ("c", "claimed"), ("d", "done"), ("x", "closed")):
        _write(tmp_path, f"{name}.md", _baton(st))
    headers, unreadable = readers.read_baton_headers(str(tmp_path))
    assert set(headers) == {f"{REL}/o.md", f"{REL}/c.md"}
    assert unreadable == []


def test_header_larger_than_one_chunk(tmp_path: Path) -> None:
    pad = "".join(f"k{i}: {'v' * 60}\n" for i in range(600))
    _write(tmp_path, "big.md", _baton(extra=pad + "sizing_object: state/sizings/s.yaml\n"))
    headers, unreadable = readers.read_baton_headers(str(tmp_path))
    assert unreadable == []
    assert headers[f"{REL}/big.md"]["sizing_object"] == "state/sizings/s.yaml"


def test_missing_handoffs_dir(tmp_path: Path) -> None:
    assert readers.read_baton_headers(str(tmp_path)) == ({}, [])


def test_successor_index(tmp_path: Path) -> None:
    _write(tmp_path, "p.md", _baton())
    _write(tmp_path, "c.md", _baton().replace("predecessor: none", f"predecessor: '{REL}/p.md'"))
    _write(
        tmp_path,
        "w.md",
        _baton().replace("predecessor: none", f"predecessor: {REL}\\w2.md"),
    )
    headers, _ = readers.read_baton_headers(str(tmp_path))
    assert readers.successor_index(headers) == {f"{REL}/p.md", f"{REL}/w2.md"}


def test_terminal_successor_is_not_live(tmp_path: Path) -> None:
    _write(tmp_path, "p.md", _baton())
    _write(tmp_path, "c.md", _baton("done").replace("predecessor: none", f"predecessor: {REL}/p.md"))
    headers, _ = readers.read_baton_headers(str(tmp_path))
    assert readers.successor_index(headers) == set()


def test_module_imports_neither_yaml_nor_subprocess() -> None:
    tree = ast.parse(Path(readers.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & {"yaml", "subprocess"}


# --- run_claims -------------------------------------------------------------

SHA = "a" * 40
SIZ = "state/sizings/s.yaml"


def _hdr(path="state/handoffs/b.md", status="open", kind="spinoff", sizing=SIZ):
    return {"path": path, "status": status, "predecessor": None, "governing_plan": None,
            "origin_plan_id": None, "sizing_object": sizing, "roadmap_id": None,
            "origin_goal_id": [], "deliverable_id": None, "kind": kind}


def _warp(root, run_id, *, baton=None, sizing=None):
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    if baton is not None:
        (d / "run-baton.json").write_text(json.dumps({"baton": baton}))
    if sizing is not None:
        (d / "run.json").write_text(json.dumps({"sizing_rel": sizing}))


def _job(root, name, **payload):
    d = root / ".coordinator-local" / "close-jobs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload))


def _claims(root, *headers):
    return readers.run_claims(str(root), {h["path"]: h for h in headers})


def test_run_claims_sizing_join_committed(tmp_path):
    _warp(tmp_path, "r1", sizing=SIZ)
    _job(tmp_path, "r1.workflow.json", terminal_sha=SHA)
    got = _claims(tmp_path, _hdr())["state/handoffs/b.md"]
    assert got == [{"class": "run", "field": "run.sizing_rel",
                    "target_path": ".coordinator-local/close-jobs/r1.workflow.json",
                    "target_status": "committed", "done": True}]


def test_run_claims_baton_field_backslash_joins_and_bare_job_name(tmp_path):
    _warp(tmp_path, "r1", baton="state\\handoffs\\b.md")
    _job(tmp_path, "r1.json", terminal_sha=SHA)
    got = _claims(tmp_path, _hdr(sizing=None))["state/handoffs/b.md"]
    assert got[0]["field"] == "run.baton" and got[0]["done"] is True
    assert got[0]["target_path"].endswith("close-jobs/r1.json")


def test_run_claims_ended_without_sha_and_short_sha(tmp_path):
    _warp(tmp_path, "r1", sizing=SIZ)
    _job(tmp_path, "r1.workflow.json", terminal_sha=None)
    _warp(tmp_path, "r2", baton="state/handoffs/b.md")
    _job(tmp_path, "r2.workflow.json", terminal_sha="abc123")
    got = _claims(tmp_path, _hdr())["state/handoffs/b.md"]
    assert [g["target_status"] for g in got] == ["ended", "ended"]
    assert not any(g["done"] for g in got)


def test_run_claims_live_when_no_close_job(tmp_path):
    _warp(tmp_path, "r1", sizing=SIZ)
    got = _claims(tmp_path, _hdr())["state/handoffs/b.md"]
    assert got[0]["target_status"] == "live" and got[0]["target_path"] == "state/warp/r1"
    assert got[0]["done"] is False


def test_run_claims_ambiguous_sizing_join_yields_nothing(tmp_path):
    _warp(tmp_path, "r1", sizing=SIZ)
    _job(tmp_path, "r1.workflow.json", terminal_sha=SHA)
    assert _claims(tmp_path, _hdr(), _hdr(path="state/handoffs/c.md")) == {}


def test_run_claims_claimed_batons_join(tmp_path):
    _warp(tmp_path, "r1", sizing=SIZ)
    _job(tmp_path, "r1.workflow.json", terminal_sha=SHA, claimed_batons=["state/handoffs/c.md"])
    got = _claims(tmp_path, _hdr(), _hdr(path="state/handoffs/c.md", sizing="state/sizings/o.yaml"))
    assert got["state/handoffs/c.md"][0]["field"] == "run.claimed_batons"


def test_run_claims_missing_dirs_and_malformed_json_yield_nothing(tmp_path):
    assert _claims(tmp_path, _hdr()) == {}
    d = tmp_path / "state" / "warp" / "r1"
    d.mkdir(parents=True)
    (d / "run.json").write_text("{not json")
    (d / "run-baton.json").write_text("[1]")
    assert _claims(tmp_path, _hdr()) == {}


def test_run_claims_reads_nothing_when_no_header_qualifies(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("listdir called")
    with monkeypatch.context() as m:
        m.setattr(readers.os, "listdir", boom)
        assert readers.run_claims(str(tmp_path), {"x": _hdr(kind=None)}) == {}
