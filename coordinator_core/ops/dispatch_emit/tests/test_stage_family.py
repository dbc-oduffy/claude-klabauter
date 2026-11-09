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


def test_brief_refuses_bad_stage_family_and_scratch():
    with pytest.raises(ValueError, match="size|plan|review|write|other"):
        sf.runner_brief("codex", "build", "p.md", "scratch/x")
    with pytest.raises(ValueError, match="claude"):
        sf.runner_brief("gpt", "size", "p.md", "scratch/x")
    with pytest.raises(ValueError, match="scratch/"):
        sf.runner_brief("codex", "size", "p.md", "/tmp/x")
