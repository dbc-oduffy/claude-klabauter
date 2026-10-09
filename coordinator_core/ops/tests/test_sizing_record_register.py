"""
coordinator_core.ops.tests.test_sizing_record_register — the "sizing.record_register" writer.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_record_register.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.sizing_record_register as mod
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_REL = "state/sizings/20260101-a.yaml"

_BASE = [
    "schema: sizing-object",
    "intent: Test intent, verbatim.",
    "estimate:",
    "  tshirt: XL",
    "  provisional: true",
    "route: pm-decision",
    "detents: []",
    "fork: null",
    "xl_exit: null",
    "status: routed",
    "premise:",
    "  provenance: read",
    "  evidence: test fixture",
]

_RULING = {"source": "pm", "quote": "later", "ref": "t1", "on": "2026-10-09"}


def _row(rid: str, **extra) -> dict:
    return {"id": rid, "source_text": f"text {rid}", "surface": "api", "status": "open", **extra}


def _register(*rows: dict, **extra) -> dict:
    return {"sources": [{"doc": "docs/x.md", "ref": "abc"}], "rows": list(rows), **extra}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _seed(repo: Path, newline: str = "\n") -> Path:
    path = repo / _REL
    path.parent.mkdir(parents=True)
    path.write_bytes((newline.join(_BASE) + newline).encode("utf-8"))
    return path


def _run(repo: Path, register, **extra) -> dict:
    params = {"sizing": _REL, "register": register, **extra}
    return mod._handler(params, repo_root=repo / ".git")


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_registration_quad() -> None:
    assert OP_CLASSIFICATION["sizing.record_register"].name == "MUTATING"
    assert OP_MODULE_MAP["sizing.record_register"] == mod.__name__
    assert _OP_KEY_SCOPE["sizing.record_register"] == "common_dir"


def test_valid_write_validates_against_schema(repo):
    p = _seed(repo)
    r = _run(repo, _register(_row("a"), _row("b")))
    assert r["exit_code"] == 0 and r["applied"] is True, r
    doc = _doc(p)
    assert [x["id"] for x in doc["requirement_register"]["rows"]] == ["a", "b"]
    assert validate_frontmatter(doc, mod._SIZING_SCHEMA_PATH) == []
    assert doc["route"] == "pm-decision"


def test_input_rollup_is_replaced_by_computed(repo):
    p = _seed(repo)
    bogus = {"open": 99, "partial": 0, "met": 0, "deferred": 0, "waived": 0}
    r = _run(repo, _register(_row("a"), _row("b", status="partial"), rollup=bogus))
    assert r["exit_code"] == 0, r
    rollup = _doc(p)["requirement_register"]["rollup"]
    assert rollup["open"] == 1 and rollup["partial"] == 1
    assert rollup["coverage"] == {"met": 0, "total": 2}
    assert r["rollup"]["open"] == 1


def test_sources_kept_verbatim(repo):
    p = _seed(repo)
    reg = _register(_row("a"))
    reg["sources"] = [{"doc": "docs/x.md", "ref": "abc"}, {"doc": "docs/y.md", "ref": "§2"}]
    assert _run(repo, reg)["exit_code"] == 0
    assert _doc(p)["requirement_register"]["sources"] == reg["sources"]


@pytest.mark.parametrize(
    "params",
    [
        {"sizing": "", "register": {"x": 1}},
        {"sizing": _REL},
        {"sizing": _REL, "register": ["not", "a", "mapping"]},
        {"sizing": "../../etc/x.yaml", "register": _register(_row("a"))},
        {"sizing": "state/sizings/absent.yaml", "register": _register(_row("a"))},
        {"sizing": _REL, "register": _register(_row("a"), _row("a"))},
        {"sizing": _REL, "register": _register(_row("a", status="deferred"))},
        {"sizing": _REL, "register": _register(_row("a", surface="bogus"))},
        {
            "sizing": _REL,
            "register": _register({"id": "a", "surface": "api", "status": "open"}),
        },
    ],
)
def test_refusals_leave_bytes_unchanged(repo, params):
    p = _seed(repo)
    before = p.read_bytes()
    r = mod._handler(params, repo_root=repo / ".git")
    assert r["exit_code"] == 1 and r["applied"] is False and r["error"], r
    assert p.read_bytes() == before


def test_refusal_messages_name_ids(repo):
    _seed(repo)
    assert "a" in _run(repo, _register(_row("a"), _row("a")))["error"]
    assert "z" in _run(repo, _register(_row("z", status="waived")))["error"]


def test_ruled_deferred_row_is_accepted(repo):
    _seed(repo)
    r = _run(repo, _register(_row("a", status="deferred", ruling=_RULING)))
    assert r["exit_code"] == 0, r


def test_identical_rewrite_is_a_noop(repo):
    p = _seed(repo)
    assert _run(repo, _register(_row("a")))["applied"] is True
    before = p.read_bytes()
    r = _run(repo, _register(_row("a")))
    assert r["exit_code"] == 0 and r["applied"] is False
    assert p.read_bytes() == before


def test_different_register_needs_supersede(repo):
    p = _seed(repo)
    _run(repo, _register(_row("a")))
    before = p.read_bytes()
    r = _run(repo, _register(_row("a"), _row("b")))
    assert r["exit_code"] == 1 and "supersede" in r["error"]
    assert p.read_bytes() == before
    r = _run(repo, _register(_row("a"), _row("b")), supersede=True)
    assert r["exit_code"] == 0 and r["applied"] is True
    assert len(_doc(p)["requirement_register"]["rows"]) == 2


def test_crlf_sizing_stays_crlf(repo):
    p = _seed(repo, newline="\r\n")
    assert _run(repo, _register(_row("a")))["exit_code"] == 0
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_no_process_spawned(repo, monkeypatch):
    _seed(repo)

    def boom(*a, **k):
        raise AssertionError("spawn on the op path")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _run(repo, _register(_row("a")))["exit_code"] == 0
