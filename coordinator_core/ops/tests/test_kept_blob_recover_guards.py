"""Guards on doctor.recover_kept_blobs: explicit-only (no hook, sweep or doctor path reaches it), streaming, scan summary.

The verb is a bulk remediation under DR-344's bulk-transfer carve-out, granted by G-EM ruling (a);
these three guards are its receipt, so the 500ms bar is not re-raised against it.
"""
from __future__ import annotations

import ast
import inspect
import os
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import doctor_kept_blobs as dkb
from coordinator_core.ops import kept_blob_transcripts as kbt
from coordinator_core.ops.tests._kept_blob_fixture import git_repo, kept, write_transcript

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OPS_DIR = Path(dkb.__file__).resolve().parent
_HOOKS_DIR = _OPS_DIR.parent / "hooks"
_REPO = _OPS_DIR.parent.parent
_OP_NAME = "doctor.recover_kept_blobs"
_RECOVER_NAMES = {"recover", "_recover_op", "recover_kept_blobs"}
_FORBIDDEN_IDENTS = _RECOVER_NAMES | {"doctor_kept_blobs"}


def _called_names(tree: ast.AST) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def _reaches_recover(source: str, root: str) -> set:
    """Recover-names called by `root` or any module-level function it reaches through Name calls."""
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen, todo, hit = set(), [root], set()
    while todo:
        name = todo.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        if name in _RECOVER_NAMES:
            hit.add(name)
        for call in _called_names(funcs[name]):
            if call in _RECOVER_NAMES:
                hit.add(call)
            elif call in funcs:
                todo.append(call)
    return hit


def _identifiers_and_strings(path: Path) -> set:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.update(a.name.split("."))
            if isinstance(node, ast.ImportFrom) and node.module:
                out.update(node.module.split("."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.update(t for t in (_OP_NAME, "doctor_kept_blobs", "recover_kept_blobs") if t in node.value)
    return out


def _table_offenders(value, _depth: int = 0) -> list:
    """Entries of a dispatch table (data) that resolve to the recover verb, the op name or its module."""
    found = []
    if _depth > 6:
        return found
    if isinstance(value, str):
        if any(t in value for t in (_OP_NAME, "doctor_kept_blobs", "recover_kept_blobs", "_recover_op")):
            found.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            found += _table_offenders(k, _depth + 1) + _table_offenders(v, _depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            found += _table_offenders(v, _depth + 1)
    elif callable(value):
        name = getattr(value, "__name__", "")
        mod = getattr(value, "__module__", "") or ""
        if name in _RECOVER_NAMES or mod.endswith("doctor_kept_blobs"):
            found.append(f"{mod}.{name}")
    return found


def _hook_tables() -> dict:
    """Every dispatch table, as data: the carrier registry plus each carrier's own public tables."""
    from coordinator_core.hooks import fanin_registries

    tables = {"FANIN_DISPATCHERS": dict(fanin_registries.FANIN_DISPATCHERS)}
    for carrier in fanin_registries.FANIN_DISPATCHERS:
        mod = fanin_registries.load_carrier(carrier)
        for key, val in vars(mod).items():
            if key.startswith("__"):
                continue
            if isinstance(val, (dict, list, tuple, set, frozenset)):
                tables[f"{carrier}.{key}"] = val
    return tables


# --- guard 1: explicit-only -------------------------------------------------------------------


def test_sweep_body_never_calls_recover():
    assert not _called_names(ast.parse(inspect.getsource(dkb.sweep).lstrip())) & _RECOVER_NAMES


def test_sweep_transitive_callees_never_reach_recover():
    src = Path(dkb.__file__).read_text(encoding="utf-8")
    for root in ("sweep", "kept_blobs_layer"):
        assert not _reaches_recover(src, root), root


def test_transitive_walk_is_red_when_a_callee_reaches_recover(tmp_path):
    spliced = tmp_path / "spliced.py"
    spliced.write_text(
        "def sweep(r):\n    return _helper(r)\n\n"
        "def _helper(r):\n    return _deeper(r)\n\n"
        "def _deeper(r):\n    return _recover_op({})\n\n"
        "def _recover_op(p):\n    return p\n",
        encoding="utf-8",
    )
    assert _reaches_recover(spliced.read_text(encoding="utf-8"), "sweep") == {"_recover_op"}


def test_transitive_walk_is_green_for_an_unrelated_callee_chain(tmp_path):
    clean = "def sweep(r):\n    return _helper(r)\n\ndef _helper(r):\n    return r\n"
    assert _reaches_recover(clean, "sweep") == set()


def test_doctor_py_never_names_the_op_or_recover():
    idents = _identifiers_and_strings(_OPS_DIR / "doctor.py")
    assert not idents & (_RECOVER_NAMES | {_OP_NAME})
    assert "kept_blobs_layer" in idents


def test_hooks_package_never_names_the_op_or_recover():
    offenders = {}
    for path in sorted(_HOOKS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            hit = _identifiers_and_strings(path) & (_FORBIDDEN_IDENTS | {_OP_NAME})
        except SyntaxError:
            continue
        if hit:
            offenders[path.relative_to(_HOOKS_DIR).as_posix()] = sorted(hit)
    assert not offenders, offenders


def test_hook_dispatch_tables_have_no_entry_resolving_to_recover():
    tables = _hook_tables()
    assert "FANIN_DISPATCHERS" in tables and len(tables) > 1
    bad = {k: _table_offenders(v) for k, v in tables.items() if _table_offenders(v)}
    assert not bad, bad


def test_dispatch_table_check_is_red_for_a_spliced_table(monkeypatch):
    from coordinator_core.hooks import fanin_registries

    monkeypatch.setitem(fanin_registries.FANIN_DISPATCHERS, "x_dispatch", _OP_NAME)
    assert _table_offenders(fanin_registries.FANIN_DISPATCHERS) == [_OP_NAME]
    assert _table_offenders({"leg": dkb._recover_op}) == [f"{dkb._recover_op.__module__}._recover_op"]
    assert _table_offenders({"leg": ("a", "b")}) == []


def test_no_other_engine_module_outside_ops_names_the_recover_module():
    referrers = set()
    for path in _REPO.joinpath("coordinator_core").rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts or path == Path(dkb.__file__).resolve():
            continue
        rel = path.relative_to(_REPO / "coordinator_core").as_posix()
        if _identifiers_and_strings(path) & {"doctor_kept_blobs", "recover_kept_blobs"}:
            referrers.add(rel)
    assert not {r for r in referrers if r.startswith("hooks/") or "sweep" in r}, referrers


# --- guard 2: streaming -----------------------------------------------------------------------


def _big_transcripts(root: Path, repo: Path, count: int = 8, size: int = 1_000_000) -> None:
    for i in range(count):
        write_transcript(
            root / f"s{i}.jsonl",
            [
                ("use", f"id-{i}", "Write", {"file_path": str(repo / "n.txt"), "content": f"v{i}\n"}, f"2026-10-25T00:00:{i:02d}Z"),
                ("raw", {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * size}]}}),
            ],
        )


def test_collect_edits_holds_at_most_one_transcript_at_a_time(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tr = tmp_path / "tr"
    _big_transcripts(tr, repo)
    tracemalloc.start()
    try:
        out = kbt.collect_edits(repo, ["n.txt"], [tr])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(out["n.txt"]) == 8
    assert peak < 4_500_000, f"peak {peak} suggests all 8 MB transcripts were held at once"


def test_transcript_files_are_scanned_one_at_a_time(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    tr = tmp_path / "tr"
    _big_transcripts(tr, repo, count=4, size=10)
    live, peak = [], [0]
    real = kbt._scan_file

    def spy(path, *a):
        live.append(path)
        peak[0] = max(peak[0], len(live))
        try:
            return real(path, *a)
        finally:
            live.remove(path)

    monkeypatch.setattr(kbt, "_scan_file", spy)
    kbt.collect_edits(repo, ["n.txt"], [tr])
    assert peak[0] == 1


# --- guard 3: scan summary --------------------------------------------------------------------


def test_scan_summary_reports_roots_transcripts_window(tmp_path):
    repo = git_repo(tmp_path)
    kept(repo, "run-g3", {"n.txt": dkb.blob_sha(b"never\n")})
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    epochs = (1_700_000_000, 1_700_000_900)
    for i, (root, epoch) in enumerate(zip((r1, r2), epochs)):
        f = write_transcript(
            root / "a.jsonl",
            [("use", f"u{i}", "Write", {"file_path": str(repo / "n.txt"), "content": "z\n"}, "2026-10-25T00:00:01Z")],
        )
        os.utime(f, (epoch, epoch))
    scan: dict = {}
    dkb.recover(repo, transcript_roots=[r1, r2], scan=scan)
    assert set(scan) == {"roots", "transcripts", "window"}
    assert scan["roots"] == 2
    assert scan["transcripts"] == 2
    assert scan["window"] == [
        datetime.fromtimestamp(e, timezone.utc).isoformat() for e in epochs
    ]


def test_scan_summary_marks_an_empty_scan(tmp_path):
    repo = git_repo(tmp_path)
    kept(repo, "run-g3e", {"n.txt": dkb.blob_sha(b"never\n")})
    scan: dict = {}
    dkb.recover(repo, transcript_roots=[tmp_path / "none"], scan=scan)
    assert scan == {"roots": 1, "transcripts": 0, "window": None}


def test_recover_op_result_carries_scanned(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    kept(repo, "run-g3o", {"n.txt": dkb.blob_sha(b"never\n")})
    tr = tmp_path / "tr"
    for i in range(2):
        write_transcript(
            tr / f"a{i}.jsonl",
            [("use", f"u{i}", "Write", {"file_path": str(repo / "n.txt"), "content": "z\n"}, f"2026-10-25T00:00:0{i + 1}Z")],
        )
    monkeypatch.setattr(kbt, "default_transcript_root", lambda: tr)
    out = dkb._recover_op({"repo_root": str(repo)})
    assert set(out["scanned"]) == {"roots", "transcripts", "window"}
    assert out["scanned"]["roots"] == 1
    assert out["scanned"]["transcripts"] == 2
    assert out["scanned"]["window"] is not None

