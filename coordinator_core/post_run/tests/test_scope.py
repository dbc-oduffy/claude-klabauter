"""Contract tests for coordinator_core.post_run.scope."""

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from coordinator_core.post_run import PostRunReport, PostRunScope, StepResult


def test_scope_field_names():
    assert [f.name for f in fields(PostRunScope)] == [
        "pipeline", "repo_root", "wiki_roots", "touched", "evidence",
    ]
    assert [f.name for f in fields(StepResult)][:3] == ["name", "changed", "residual"]


def test_for_run_joins_wiki_roots_and_normalises_to_tuples():
    s = PostRunScope.for_run("lessons", "/r", ["docs/wiki"], ["a.md"], ["e.json"])
    assert s.wiki_roots == (Path("/r/docs/wiki"),)
    assert s.touched == ("a.md",) and s.evidence == ("e.json",)


def test_unknown_pipeline_refused_known_accepted():
    with pytest.raises(ValueError):
        PostRunScope("other", Path("/r"), (), ())
    assert PostRunScope("distill", Path("/r"), (), ()).pipeline == "distill"


def test_frozen():
    s = PostRunScope("distill", Path("/r"), (), ("a.md",))
    with pytest.raises(FrozenInstanceError):
        s.touched = ()  # type: ignore[misc]


def test_commit_paths_dedupes_in_order():
    s = PostRunScope("lessons", Path("/r"), (), ["a", "b"], ["b", "c"])
    assert s.commit_paths == ("a", "b", "c")


def test_report_aggregates_and_clean_flag():
    clean = PostRunReport("distill", [StepResult("h1", ["a"]), StepResult("nav", ["a", "b"])])
    assert clean.changed == ("a", "b") and clean.clean and clean.residual == ()
    dirty = PostRunReport("distill", [StepResult("linkfix", (), ["x.md -> gone"])])
    assert not dirty.clean and dirty.residual == ("linkfix: x.md -> gone",)
