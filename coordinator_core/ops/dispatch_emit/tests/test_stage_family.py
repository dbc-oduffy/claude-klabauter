"""Pins the stage-family resolver: closed enum, absent means claude."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import stage_family as sf


def test_absent_is_claude():
    assert sf.resolve(None) == "claude"
    assert sf.resolve("") == "claude"


@pytest.mark.parametrize("fam", ["claude", "codex", "grok", "agy"])
def test_known_families_resolve(fam):
    assert sf.resolve(fam) == fam


@pytest.mark.parametrize("bad", ["gpt", "Codex", " codex", 3])
def test_unknown_family_refused_naming_set(bad):
    with pytest.raises(ValueError, match=r"claude\|codex\|grok\|agy"):
        sf.resolve(bad)


def test_runner_agent_only_for_non_claude():
    assert sf.runner_agent("claude") is None
    assert sf.runner_agent(None) is None
    for fam in ("codex", "grok", "agy"):
        assert sf.runner_agent(fam) == "coordinator:family-stage-runner"
    with pytest.raises(ValueError):
        sf.runner_agent("gpt")


def test_brief_names_runner_fields():
    b = sf.runner_brief("codex", "size", "scratch/x/p.md", "scratch/x")
    assert "family: codex" in b and "stage: size" in b
    assert "prompt: scratch/x/p.md" in b and "scratch: scratch/x" in b
    assert "writes:" not in b


def test_write_stage_lists_paths_and_requires_them():
    b = sf.runner_brief("agy", "write", "p.md", "scratch\\x", ["a.py", "b.py"])
    assert "scratch: scratch/x" in b and "  - a.py" in b and "  - b.py" in b
    with pytest.raises(ValueError, match="write paths"):
        sf.runner_brief("agy", "write", "p.md", "scratch/x", [])


def _res(family, verdict="OK", stage="research", **extra):
    return {
        "family": family, "stage": stage, "exit_code": 0 if verdict == "OK" else 1,
        "output_path": "scratch/x/out.md", "verdict": verdict, **extra,
    }


def test_scout_and_research_are_stages_and_roles():
    assert {"scout", "research"} <= set(sf.STAGES)
    assert sf.ROLES == ("scout", "research", "review")
    assert sf.FALLBACK == "claude" and sf.FALLBACK_VERDICTS == ("BLOCKED", "FAILED")
    assert "stage: scout" in sf.runner_brief("grok", "scout", "p.md", "scratch/x")


def test_record_keeps_failed_family_then_fallback():
    agy = _res("agy", "FAILED", blocked_reason="vendor 429")
    rec = sf.stage_record("research", agy, _res("claude"))
    assert rec["stage_role"] == "research"
    assert [a["family"] for a in rec["attempts"]] == ["agy", "claude"]
    assert rec["attempts"][0]["blocked_reason"] == "vendor 429"
    assert "fallback_for" not in rec["attempts"][0]
    assert rec["attempts"][1]["fallback_for"] == "agy"
    assert rec["reported_family"] == "claude"


def test_record_without_fallback_reports_family():
    rec = sf.stage_record("review", _res("codex", stage="review"))
    assert rec["attempts"] == [_res("codex", stage="review")]
    assert rec["reported_family"] == "codex"
    blocked = sf.stage_record("review", _res("codex", "BLOCKED", blocked_reason="auth"))
    assert blocked["reported_family"] == "codex"


def test_record_refusals():
    with pytest.raises(ValueError, match="blocked_reason"):
        sf.stage_record("research", _res("agy", "FAILED"))
    with pytest.raises(ValueError, match="fallback result given"):
        sf.stage_record("research", _res("agy", "OK"), _res("claude"))
    with pytest.raises(ValueError, match="fallback result given"):
        sf.stage_record("research", _res("agy", "WARN"), _res("claude"))
    bad = _res("agy", "FAILED", blocked_reason="x")
    del bad["exit_code"]
    with pytest.raises(ValueError, match="exit_code"):
        sf.stage_record("research", bad)
    fb = _res("claude")
    del fb["output_path"]
    with pytest.raises(ValueError, match="output_path"):
        sf.stage_record("research", _res("agy", "FAILED", blocked_reason="x"), fb)
    with pytest.raises(ValueError, match="stage role"):
        sf.stage_record("size", _res("agy"))


def test_brief_refuses_bad_stage_family_and_scratch():
    with pytest.raises(ValueError, match="size|plan|review|write|other"):
        sf.runner_brief("codex", "build", "p.md", "scratch/x")
    with pytest.raises(ValueError, match="claude"):
        sf.runner_brief("gpt", "size", "p.md", "scratch/x")
    with pytest.raises(ValueError, match="scratch/"):
        sf.runner_brief("codex", "size", "p.md", "/tmp/x")
