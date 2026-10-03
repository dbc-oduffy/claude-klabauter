"""
coordinator_core.ops.tests.test_sizing_record_xl_exit — the "sizing.record_xl_exit" writer.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_record_xl_exit.py -q
"""

from __future__ import annotations

from pathlib import Path

import yaml

import coordinator_core.ops.sizing_record_xl_exit as mod
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_QUOTE = "go roadmap on that one"


def _seed(tmp_path: Path, xl_exit: str = "null") -> Path:
    (tmp_path / ".git").mkdir()
    path = tmp_path / "state" / "sizings" / "20260101-a.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                "schema: sizing-object",
                "intent: Test intent, verbatim.",
                "estimate:",
                "  tshirt: XL",
                "  provisional: true",
                "route: pm-decision",
                "detents: []",
                "fork: null",
                f"xl_exit: {xl_exit}",
                "status: routed",
                "premise:",
                "  provenance: read",
                "  evidence: test fixture",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _run(tmp_path: Path, pick: str, quote: str = _QUOTE, **extra) -> dict:
    params = {
        "sizing": "state/sizings/20260101-a.yaml",
        "xl_exit": pick,
        "pm_quote": quote,
        "decided_on": "2026-10-03",
        **extra,
    }
    return mod._handler(params, repo_root=tmp_path / ".git")


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_records_pick_quote_and_date(tmp_path):
    p = _seed(tmp_path)
    r = _run(tmp_path, "roadmap")
    assert r["exit_code"] == 0 and r["applied"] is True, r
    d = _doc(p)
    assert d["xl_exit"] == "roadmap"
    assert d["pm_resolution"] == {"decided_on": "2026-10-03", "xl_exit": _QUOTE}
    assert d["route"] == "pm-decision"


def test_shape_then_roadmap_overwrites(tmp_path):
    p = _seed(tmp_path)
    assert _run(tmp_path, "shape", "shape it first")["applied"] is True
    r = _run(tmp_path, "roadmap")
    assert r["exit_code"] == 0 and r["applied"] is True, r
    d = _doc(p)
    assert d["xl_exit"] == "roadmap" and d["pm_resolution"]["xl_exit"] == _QUOTE


def test_same_pick_is_idempotent_and_keeps_first_quote(tmp_path):
    p = _seed(tmp_path)
    _run(tmp_path, "roadmap")
    r = _run(tmp_path, "roadmap", "different words")
    assert r["exit_code"] == 0 and r["applied"] is False
    assert _doc(p)["pm_resolution"]["xl_exit"] == _QUOTE


def test_settled_exit_is_not_re_decided(tmp_path):
    p = _seed(tmp_path, "roadmap")
    before = p.read_text(encoding="utf-8")
    for pick in ("shape", "accept_multi_session"):
        r = _run(tmp_path, pick)
        assert r["exit_code"] == 1 and "already" in r["error"], r
    assert p.read_text(encoding="utf-8") == before


def test_preserves_existing_pm_resolution_keys(tmp_path):
    p = _seed(tmp_path)
    p.write_text(
        p.read_text(encoding="utf-8")
        + "pm_resolution:\n  decided_on: '2026-09-01'\n  wiki_home: docs/wiki\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "shape")["applied"] is True
    pm = _doc(p)["pm_resolution"]
    assert pm["wiki_home"] == "docs/wiki" and pm["xl_exit"] == _QUOTE
    assert pm["decided_on"] == "2026-10-03"


def test_refusals(tmp_path):
    p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    assert _run(tmp_path, "roadmap", "  ")["exit_code"] == 1
    assert _run(tmp_path, "split")["exit_code"] == 1
    assert _run(tmp_path, "roadmap", decided_on="10/03")["exit_code"] == 1
    assert _run(tmp_path, "roadmap", sizing="../escape.yaml")["exit_code"] == 1
    assert p.read_text(encoding="utf-8") == before


def test_registered_everywhere():
    assert OP_CLASSIFICATION.get("sizing.record_xl_exit") is not None
    assert _OP_KEY_SCOPE.get("sizing.record_xl_exit") == "common_dir"
    assert OP_MODULE_MAP["sizing.record_xl_exit"] == "coordinator_core.ops.sizing_record_xl_exit"


def test_sizing_assemble_cli_routes_to_writer(tmp_path, monkeypatch):
    from coordinator_core import sizing_assemble

    p = _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    argv = ["--xl-exit", "shape", "--pm-quote", _QUOTE, "--decided-on", "2026-10-03",
            "--write", "state/sizings/20260101-a.yaml"]
    assert sizing_assemble.main(argv) == 0
    assert _doc(p)["xl_exit"] == "shape"
    assert sizing_assemble.main(["--xl-exit", "shape", "--pm-quote", "x"]) != 0
