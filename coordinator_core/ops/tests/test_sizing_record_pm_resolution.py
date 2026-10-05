"""
coordinator_core.ops.tests.test_sizing_record_pm_resolution — the "sizing.record_pm_resolution" writer.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_record_pm_resolution.py -q
"""

from __future__ import annotations

from pathlib import Path

import yaml

import coordinator_core.ops.sizing_record_pm_resolution as mod
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_QUOTE = "yes, prompt me after sizing"
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
]
_PREMISE = ["premise:", "  provenance: read", "  evidence: test fixture"]


def _seed(tmp_path: Path, pm_block: list[str] | None = None, block_first: bool = False) -> Path:
    (tmp_path / ".git").mkdir()
    path = tmp_path / _REL
    path.parent.mkdir(parents=True)
    pm = pm_block or []
    lines = (pm + _BASE + _PREMISE) if block_first else (_BASE + pm + _PREMISE)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(tmp_path: Path, key: str = "post_size_prompt", quote: str = _QUOTE, **extra) -> dict:
    params = {
        "sizing": _REL,
        "key": key,
        "pm_quote": quote,
        "decided_on": "2026-10-05",
        **extra,
    }
    return mod._handler(params, repo_root=tmp_path / ".git")


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_writes_new_block(tmp_path):
    p = _seed(tmp_path)
    r = _run(tmp_path)
    assert r["exit_code"] == 0 and r["applied"] is True, r
    d = _doc(p)
    assert d["pm_resolution"] == {"decided_on": "2026-10-05", "post_size_prompt": _QUOTE}
    assert list(d["pm_resolution"])[0] == "decided_on"
    assert d["route"] == "pm-decision" and d["xl_exit"] is None


def test_preserves_existing_keys(tmp_path):
    p = _seed(
        tmp_path, ["pm_resolution:", "  decided_on: '2026-09-01'", "  wiki_home: docs/wiki"]
    )
    assert _run(tmp_path, "xl_route_assent", "assent")["applied"] is True
    pm = _doc(p)["pm_resolution"]
    assert pm["wiki_home"] == "docs/wiki" and pm["xl_route_assent"] == "assent"
    assert pm["decided_on"] == "2026-10-05"


def test_block_not_last_lands_and_other_fields_byte_preserved(tmp_path):
    block = ["pm_resolution:", "  decided_on: '2026-09-01'", "  wiki_home: docs/wiki"]
    p = _seed(tmp_path, block, block_first=True)
    before = p.read_text(encoding="utf-8")
    assert _run(tmp_path)["applied"] is True
    after = p.read_text(encoding="utf-8")
    assert _doc(p)["pm_resolution"]["post_size_prompt"] == _QUOTE
    stripped_before = before.split("estimate:", 1)[1]
    assert after.endswith(stripped_before)


def test_identical_quote_is_idempotent_and_keeps_decided_on(tmp_path):
    p = _seed(tmp_path)
    _run(tmp_path)
    before = p.read_text(encoding="utf-8")
    r = _run(tmp_path, decided_on="2026-11-01")
    assert r["exit_code"] == 0 and r["applied"] is False, r
    assert p.read_text(encoding="utf-8") == before


def test_different_value_refused_without_supersede(tmp_path):
    p = _seed(tmp_path)
    _run(tmp_path)
    before = p.read_text(encoding="utf-8")
    r = _run(tmp_path, quote="no, do not prompt")
    assert r["exit_code"] == 1 and _QUOTE in r["error"], r
    assert p.read_text(encoding="utf-8") == before


def test_supersede_replaces(tmp_path):
    p = _seed(tmp_path)
    _run(tmp_path)
    r = _run(tmp_path, quote="no, do not prompt", supersede=True, decided_on="2026-10-06")
    assert r["exit_code"] == 0 and r["applied"] is True, r
    assert _doc(p)["pm_resolution"] == {
        "decided_on": "2026-10-06",
        "post_size_prompt": "no, do not prompt",
    }


def test_reserved_and_owned_keys_refused(tmp_path):
    p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    for key in ("decided_on", "xl_exit", "fork"):
        r = _run(tmp_path, key, supersede=True)
        assert r["exit_code"] == 1, (key, r)
    assert "sizing.record_xl_exit" in _run(tmp_path, "xl_exit")["error"]
    assert p.read_text(encoding="utf-8") == before


def test_bad_key_shape_refused(tmp_path):
    p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    for key in ("Bad", "1abc", "has-dash", "has space", "a" * 65):
        assert _run(tmp_path, key)["exit_code"] == 1, key
    assert p.read_text(encoding="utf-8") == before


def test_missing_params_malformed_date_and_escape_refused(tmp_path):
    p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    assert _run(tmp_path, quote="  ")["exit_code"] == 1
    assert _run(tmp_path, key="")["exit_code"] == 1
    assert _run(tmp_path, sizing="")["exit_code"] == 1
    assert _run(tmp_path, decided_on="10/05")["exit_code"] == 1
    assert _run(tmp_path, sizing="../escape.yaml")["exit_code"] == 1
    assert p.read_text(encoding="utf-8") == before


def test_registered_everywhere():
    assert OP_CLASSIFICATION.get("sizing.record_pm_resolution") is not None
    assert _OP_KEY_SCOPE.get("sizing.record_pm_resolution") == "common_dir"
    assert (
        OP_MODULE_MAP["sizing.record_pm_resolution"]
        == "coordinator_core.ops.sizing_record_pm_resolution"
    )


def test_sizing_assemble_cli_routes_to_writer(tmp_path, monkeypatch):
    from coordinator_core import sizing_assemble

    p = _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    base = ["--pm-resolution", "post_size_prompt", "--pm-quote", _QUOTE,
            "--decided-on", "2026-10-05"]
    assert sizing_assemble.main(base + ["--write", _REL]) == 0
    assert _doc(p)["pm_resolution"]["post_size_prompt"] == _QUOTE
    assert sizing_assemble.main(["--pm-resolution", "k", "--pm-quote", "x"]) != 0
    assert sizing_assemble.main(base + ["--xl-exit", "shape", "--write", _REL]) != 0
    assert sizing_assemble.main(
        ["--pm-resolution", "post_size_prompt", "--pm-quote", "other", "--write", _REL]
    ) != 0
    assert sizing_assemble.main(
        ["--pm-resolution", "post_size_prompt", "--pm-quote", "other", "--supersede",
         "--write", _REL]
    ) == 0
    assert _doc(p)["pm_resolution"]["post_size_prompt"] == "other"
