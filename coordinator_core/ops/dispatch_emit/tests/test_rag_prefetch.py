"""rag_prefetch: block bounding, applicability, tool family and the rag_use receipt against the 1.6.0 schema."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import rag_prefetch as rp

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "contract" / "doe-wake-digest.schema.json").read_text(encoding="utf-8")
)
RAG_USE_SCHEMA = {**SCHEMA["properties"]["rag_use"], "definitions": SCHEMA.get("definitions", {}),
                  "$defs": SCHEMA.get("$defs", {})}

ROLES = {
    "gated": ["*research*", "*reviewer*"], "exempt": ["*executor*"],
    "ue_cadence": {"window_tool_calls": 4, "min_rag_calls": 1}, "tool_prefix": "mcp__project-rag__",
    "source": "file",
}


def _row(aid, atype, rag=0, grep=0, errors=0, positions=None, tool_calls=0):
    return {"agent_id": aid, "agent_type": atype, "label": f"work:{aid}", "rag_calls": rag,
            "grep_calls": grep, "rag_errors": errors, "rag_positions": positions or [], "tool_calls": tool_calls}


def test_block_prefixes_mark_and_stays_under_cap():
    text = "First sentence here. " * 400
    out = rp.citation_block(text)
    assert out.startswith(rp.PREFETCH_BLOCK_MARK)
    assert len(out) <= rp.PREFETCH_BLOCK_CHAR_CAP
    assert out.endswith(".")


def test_block_cuts_at_whole_sentence_with_small_cap():
    out = rp.citation_block("Alpha one. Beta two. Gamma three.", cap=len(rp.PREFETCH_BLOCK_MARK) + 2 + 22)
    assert out.endswith("Beta two.")
    assert "Gamma" not in out


def test_block_blank_text_is_empty():
    assert rp.citation_block("   ") == ""
    assert rp.citation_block(None) == ""


def test_block_short_text_untouched():
    assert rp.citation_block("one cite") == f"{rp.PREFETCH_BLOCK_MARK}\n\none cite"


def test_applies_follows_serves_repo(monkeypatch, tmp_path):
    from coordinator_core.bash_guards import _rag_serving

    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: True)
    assert rp.prefetch_applies(tmp_path) is True
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: False)
    assert rp.prefetch_applies(tmp_path) is False
    assert rp.prefetch_applies(None) is False


def test_applies_false_when_probe_raises(monkeypatch, tmp_path):
    from coordinator_core.bash_guards import _rag_serving

    def boom(cwd=None):
        raise RuntimeError("x")

    monkeypatch.setattr(_rag_serving, "serves_repo", boom)
    assert rp.prefetch_applies(tmp_path) is False


@pytest.mark.parametrize("names,expected", [
    (["context7"], "context7"), (["typescript-lsp"], "lsp"), ([], "none"),
])
def test_tool_family_without_rag(tmp_path, monkeypatch, names, expected):
    monkeypatch.setattr(rp.rag_use, "rag_tier", lambda *a, **k: "none")
    assert rp.tool_family(tmp_path, mcp_names=names, plugin_roots=[]) == expected


def test_tool_family_example_retrieval_repo_when_installed(tmp_path):
    assert rp.tool_family(tmp_path, tier="rag", mcp_names=["context7"], plugin_roots=[]) == "project-rag"


def test_receipt_tier_rag_pass_fail_exempt_and_schema(tmp_path):
    scan = {"agents": [
        _row("a1", "coordinator:research-scout", rag=3, grep=1, errors=1),
        _row("a2", "coordinator:research-scout", rag=0, grep=5),
        _row("a3", "coordinator:executor", rag=0, grep=9),
    ]}
    out = rp.rag_use_receipt(scan, repo_root=tmp_path, roles=ROLES, tier="rag", family="project-rag")
    by = {a["agent_id"]: a for a in out["agents"]}
    assert by["a1"]["verdict"] == "pass" and by["a1"]["rag_errors"] == 1
    assert by["a2"]["verdict"] == "fail"
    assert by["a3"]["verdict"] == "exempt" and by["a3"]["gated"] is False
    assert out["totals"] == {"rag_calls": 3, "grep_calls": 15}
    assert out["tier"] == "rag" and out["installed"] is True
    jsonschema.validate(out, RAG_USE_SCHEMA)


def test_receipt_failed_call_counts_as_attempt(tmp_path):
    scan = {"agents": [_row("a1", "x:research", rag=1, errors=1)]}
    out = rp.rag_use_receipt(scan, repo_root=tmp_path, roles=ROLES, tier="rag", family="project-rag")
    assert out["agents"][0]["verdict"] == "pass"


def test_receipt_tier_none_everyone_exempt(tmp_path):
    scan = {"agents": [_row("a1", "x:research", rag=0, grep=2)]}
    out = rp.rag_use_receipt(scan, repo_root=tmp_path, roles=ROLES, tier="none", family="context7")
    assert out["agents"][0]["verdict"] == "exempt"
    assert out["installed"] is False and out["tool_family"] == "context7"
    jsonschema.validate(out, RAG_USE_SCHEMA)


def test_receipt_tier_ue_cadence_fails_sparse_agent(tmp_path):
    dense = _row("d", "x:research", rag=3, positions=[0, 2, 4], tool_calls=6)
    sparse = _row("s", "x:research", rag=1, positions=[0], tool_calls=10)
    out = rp.rag_use_receipt({"agents": [dense, sparse]}, repo_root=tmp_path, roles=ROLES, tier="ue",
                             family="project-rag")
    by = {a["agent_id"]: a["verdict"] for a in out["agents"]}
    assert by == {"d": "pass", "s": "fail"}
    jsonschema.validate(out, RAG_USE_SCHEMA)


def test_receipt_same_sparse_agent_passes_at_tier_rag(tmp_path):
    sparse = _row("s", "x:research", rag=1, positions=[0], tool_calls=10)
    out = rp.rag_use_receipt({"agents": [sparse]}, repo_root=tmp_path, roles=ROLES, tier="rag",
                             family="project-rag")
    assert out["agents"][0]["verdict"] == "pass"


def test_receipt_empty_scan_validates(tmp_path):
    out = rp.rag_use_receipt({}, repo_root=tmp_path, roles=ROLES, tier="none", family="none")
    assert out["agents"] == [] and out["totals"] == {"rag_calls": 0, "grep_calls": 0}
    jsonschema.validate(out, RAG_USE_SCHEMA)


def test_receipt_ue_per_agent_verdict_ignores_fleet_empty_answers(tmp_path):
    dense = {**_row("d", "x:research", rag=3, positions=[0, 2, 4], tool_calls=6), "rag_empty": 3}
    out = rp.rag_use_receipt({"agents": [dense]}, repo_root=tmp_path, roles=ROLES, tier="ue", family="project-rag")
    assert out["agents"][0]["verdict"] == "pass"


def test_prefetch_applies_logs_when_serves_repo_raises(tmp_path, monkeypatch, caplog):
    import coordinator_core.bash_guards._rag_serving as serving

    def boom(_root):
        raise RuntimeError("broken")

    monkeypatch.setattr(serving, "serves_repo", boom)
    with caplog.at_level("WARNING"):
        assert rp.prefetch_applies(tmp_path) is False
    assert "serves_repo failed" in caplog.text
