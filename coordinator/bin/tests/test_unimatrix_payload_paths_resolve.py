"""Every `Path(__file__)....parents[n] / "seg" / ...` path a coordinator/bin/unimatrix-*.py
script reads must land on a file the klabauter publish rows ship, at the same relative spot.

Derives the paths from the scripts' own AST and maps them through
`setup/publish-targets.portable` (source_subdir -> dest_subdir, allowlist, `!` exclusions).
"""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

import pytest

pytestmark = pytest.mark.complete

REPO = Path(__file__).resolve().parents[3]
TARGETS = REPO / "setup" / "publish-targets.portable"
BIN = REPO / "coordinator" / "bin"
PUBLISH_DEST = "publish-mirror:claude_klabauter"


def _rows() -> list[dict]:
    rows = []
    for line in TARGETS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = [x.strip() for x in line.rstrip("\r").split("|")]
        if len(f) < 7 or f[2] != PUBLISH_DEST:
            continue
        entries = [e for e in f[6].split(",") if e]
        rows.append({
            "name": f[0],
            "src": f[3],
            "dst": f[4],
            "allow": {e for e in entries if not e.startswith("!")},
            "excl": {e[1:] for e in entries if e.startswith("!")},
        })
    return rows


def _shipped(dest_path: str, rows: list[dict]) -> str | None:
    """Name of the row that ships `dest_path` (a payload-relative file), or None."""
    for r in rows:
        prefix = r["dst"].rstrip("/") + "/" if r["dst"] else ""
        if prefix and not dest_path.startswith(prefix):
            continue
        rel = dest_path[len(prefix):]
        if not rel or rel in r["excl"]:
            continue
        if r["allow"] and rel.split("/")[0] not in r["allow"]:
            continue
        if (REPO / r["src"] / rel).is_file():
            return r["name"]
    return None


def _scan_parents(script: Path) -> tuple[list[tuple[int, list[str]]], list[str]]:
    """Scan the script for `<...>.parents[n] / "a" / "b"` chains.

    Returns (literal chains as (n, ["a", "b"]), descriptions of chains with a non-literal operand).
    """
    tree = ast.parse(script.read_text(encoding="utf-8"))
    inner_divs = {id(n.left) for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    found: list[tuple[int, list[str]]] = []
    nonliteral: list[str] = []
    for node in ast.walk(tree):
        if id(node) in inner_divs:
            continue
        segs: list[str] = []
        dynamic = False
        cur = node
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            if isinstance(cur.right, ast.Constant) and isinstance(cur.right.value, str):
                segs.insert(0, cur.right.value)
            else:
                dynamic = True
            cur = cur.left
        if (isinstance(cur, ast.Subscript) and isinstance(cur.value, ast.Attribute)
                and cur.value.attr == "parents" and isinstance(cur.slice, ast.Constant)
                and isinstance(cur.slice.value, int)):
            if dynamic:
                nonliteral.append(f"{script.name}:{node.lineno} parents[{cur.slice.value}] chain with a non-literal operand")
            else:
                found.append((cur.slice.value, segs))
    return found, nonliteral


def _parents_reads(script: Path) -> list[tuple[int, list[str]]]:
    return _scan_parents(script)[0]


def test_no_parents_chain_has_a_non_literal_operand():
    dynamic = [d for s in _scripts() for d in _scan_parents(s)[1]]
    assert not dynamic, "unverifiable path reads (operand not a string literal): " + "; ".join(dynamic)


def _scripts() -> list[Path]:
    return sorted(BIN.glob("unimatrix-*.py"))


def test_scripts_are_discovered_and_ship():
    rows = _rows()
    scripts = _scripts()
    assert scripts
    unshipped = [s.name for s in scripts if not _shipped(f"coordinator/bin/{s.name}", rows)]
    assert not unshipped, f"unimatrix scripts missing from the publish payload: {unshipped}"


def test_parents_reads_are_found():
    reads = [(s.name, r) for s in _scripts() for r in _parents_reads(s)]
    assert any(segs for _, (_, segs) in reads), "AST scan found no parents[n]/<segment> read"


def test_every_parents_path_resolves_inside_payload():
    rows = _rows()
    missing = []
    for script in _scripts():
        for n, segs in _parents_reads(script):
            if not segs:
                continue
            dest = PurePosixPath("coordinator/bin") / script.name
            target = dest.parents[n].joinpath(*segs).as_posix()
            if not _shipped(target, rows):
                missing.append(f"{script.name}: parents[{n}]/{'/'.join(segs)} -> {target}")
    assert not missing, "path read by a published unimatrix script is not in the payload: " + "; ".join(missing)


def test_unmapped_path_is_refused():
    assert _shipped("coordinator/templates/unimatrix/not-a-real-file.md", _rows()) is None
    assert _shipped("elsewhere/peer-doctrine.md", _rows()) is None
