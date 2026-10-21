"""Tests for the baton.close_report header reader and successor index."""

from __future__ import annotations

import ast
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
