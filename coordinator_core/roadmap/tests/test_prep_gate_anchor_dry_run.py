"""ANCHOR class of plan.prep_gate: every declared anchor check is dry-run at the tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.tests.test_plan_prep_gate import _FM, _gate, _plan, _repo
from coordinator_core.roadmap import prep_gate as pg

_TEXT = '{"source_type": "plugin_buildcs"}\nplugin_buildcs_chunks: x --plugins-source-dir\n'


def _commit(root: Path, *paths: str) -> None:
    """Anchor checks read the base ref, so the checked file must be committed at HEAD."""
    for args in (("init", "-q"), ("add", *paths), ("commit", "-q", "-m", "base")):
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _anchored(root: Path, command: str, expect: str = "1") -> str:
    (root / "cfg.json").write_text(_TEXT, encoding="utf-8")
    _commit(root, "cfg.json")
    fm = _FM + (
        "anchor_premises:\n"
        "  - id: AP1\n"
        "    claim: the config names the plugin\n"
        f"    check: {{command: \"{command}\", expect: \"{expect}\"}}\n"
    )
    return _plan(root, frontmatter=fm)


def _anchor(tmp_path: Path, command: str, expect: str = "1") -> dict:
    common = _repo(tmp_path)
    return _gate({"plan": _anchored(tmp_path, command, expect)}, common)


def test_anchor_sits_after_capabilities_before_schema():
    order = pg.CLASS_ORDER
    assert order[order.index("CAPABILITIES") + 1 : order.index("CAPABILITIES") + 3] == ("ANCHOR", "SCHEMA")


def test_elided_pattern_against_true_text_is_malformed_and_mechanical(tmp_path):
    result = _anchor(tmp_path, "grep -c source_type...plugin_buildcs cfg.json")
    cls = result["classes"]["ANCHOR"]
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "anchor-check-malformed"
    assert cls["mechanical"] is True
    assert "AP1: source_type...plugin_buildcs" in cls["detail"]
    assert result["verdict"] == pg.NOT_PREPPED


def test_literal_true_check_passes(tmp_path):
    cls = _anchor(tmp_path, "grep -c -F plugin_buildcs_chunks cfg.json")["classes"]["ANCHOR"]
    assert cls["status"] == "PASS"


def test_fixed_string_elision_is_a_literal_and_passes_when_true(tmp_path):
    (tmp_path / "x.txt").write_text("a...b\n", encoding="utf-8")
    common = _repo(tmp_path)
    _commit(tmp_path, "x.txt")
    fm = _FM + (
        "anchor_premises:\n"
        "  - id: AP1\n"
        "    claim: literal dots\n"
        "    check: {command: \"grep -c -F a...b x.txt\", expect: \"1\"}\n"
    )
    cls = _gate({"plan": _plan(tmp_path, frontmatter=fm)}, common)["classes"]["ANCHOR"]
    assert cls["status"] == "PASS"


def test_non_elided_zero_count_is_a_mismatch_defect(tmp_path):
    cls = _anchor(tmp_path, "grep -c -F absent_text cfg.json")["classes"]["ANCHOR"]
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "anchor-check-mismatch"
    assert cls["mechanical"] is True
    assert "AP1: observed 0, expected 1" in cls["detail"]


def test_count_off_expect_is_a_mismatch_defect(tmp_path):
    cls = _anchor(tmp_path, "grep -c -F plugin_buildcs cfg.json", expect="5")["classes"]["ANCHOR"]
    assert cls["kind"] == "anchor-check-mismatch"
    assert "expected 5" in cls["detail"]


def test_a_check_the_in_process_runner_cannot_run_is_a_defect_not_a_pass(tmp_path):
    cls = _anchor(tmp_path, "grep -c -F plugin_buildcs cfg.json | wc -l")["classes"]["ANCHOR"]
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "anchor-check-unevaluable"
    assert cls["mechanical"] is True
    assert "AP1: " in cls["detail"] and "unsupported shell operator '|'" in cls["detail"]


def test_undeclared_anchor_premises_is_not_this_class(tmp_path):
    common = _repo(tmp_path)
    cls = _gate({"plan": _plan(tmp_path)}, common)["classes"]["ANCHOR"]
    assert cls["status"] == "PASS"


def test_empty_anchor_premises_passes(tmp_path):
    common = _repo(tmp_path)
    cls = _gate({"plan": _plan(tmp_path, frontmatter=_FM + "anchor_premises: []\n")}, common)
    assert cls["classes"]["ANCHOR"]["status"] == "PASS"
