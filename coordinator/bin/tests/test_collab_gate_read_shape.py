"""Pins the frozen read shape of the collaboration verdict (`collab_gate.read_verdict`).

Zero git spawns. The no-spawn guarantee is asserted statically (AST of module-scope imports) and
at runtime (subprocess entry points patched to fail).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

GATE_PATH = Path(__file__).resolve().parents[2] / "lib" / "collab_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("collab_gate_under_test", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()


def _write(root: Path, text: str | bytes) -> None:
    p = root / "state" / "collaboration-verdict.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")


def test_verdict_fields_are_the_frozen_four():
    names = [f.name for f in dataclasses.fields(gate.Verdict)]
    assert names == ["mode", "source", "author_count", "checked_at"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.Verdict("solo", "detected", 1, "x").mode = "multi"


def test_absent_cache_is_solo_unresolved(tmp_path):
    v = gate.read_verdict(tmp_path)
    assert (v.mode, v.source) == ("solo", "unresolved")


def test_valid_cache_round_trips(tmp_path):
    doc = {"mode": "multi", "source": "detected", "author_count": 3,
           "checked_at": "2026-09-30T12:00:00Z"}
    _write(tmp_path, json.dumps(doc))
    v = gate.read_verdict(tmp_path)
    assert dataclasses.asdict(v) == doc


@pytest.mark.parametrize("payload", [
    '{"mode": "multi", "source": "det',
    b"\xff\xfe\x00garbage",
    "[]",
    "null",
    "",
    json.dumps({"mode": "multi"}),
    json.dumps({"mode": "both", "source": "detected", "author_count": 1, "checked_at": "t"}),
    json.dumps({"mode": "solo", "source": "unresolved", "author_count": 0, "checked_at": ""}),
    json.dumps({"mode": "solo", "source": "detected", "author_count": True, "checked_at": "t"}),
    json.dumps({"mode": "solo", "source": "detected", "author_count": -1, "checked_at": "t"}),
    json.dumps({"mode": "solo", "source": "detected", "author_count": 1, "checked_at": 5}),
])
def test_unparseable_or_off_schema_cache_is_corrupt(tmp_path, payload):
    _write(tmp_path, payload)
    v = gate.read_verdict(tmp_path)
    assert (v.mode, v.source) == ("solo", "corrupt")


def test_directory_at_cache_path_is_corrupt_not_raised(tmp_path):
    (tmp_path / "state" / "collaboration-verdict.json").mkdir(parents=True)
    assert gate.read_verdict(tmp_path).source == "corrupt"


def test_read_never_spawns(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn attempted")

    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, boom)
    gate.read_verdict(tmp_path)
    _write(tmp_path, "{broken")
    gate.read_verdict(tmp_path)


def _module_scope_imports(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def test_import_graph_has_no_collab_detect_and_no_subprocess():
    tree = ast.parse(GATE_PATH.read_text(encoding="utf-8"))
    assert _module_scope_imports(tree) <= {"__future__", "json", "dataclasses", "pathlib"}
    all_imported = {
        n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    assert not {"collab_detect", "subprocess", "os"} & all_imported
