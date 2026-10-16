"""bind_repo_targets / segments_for / compare_output: N repo targets fan out to keyed segments plus a compare segment."""

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "pipeline_research_multi_repo"
SCRATCH = "scratch/run"
BRIEF = "scratch/run/ask.md"


def _targets(*refs):
    return [{"source": "repo", "ref": r} for r in refs]


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# abs-path-ok: a backslash ref is the input under test, never opened
@pytest.mark.parametrize("refs", [("/a/engine", "/b/Tools"), ("/a/engine", "C:\\b\\Tools\\", "/c/third/")])
def test_n_targets_get_ordered_keys_and_briefs(tmp_path, refs):
    segs, compare = re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets(*refs))
    assert [k for k, _ in segs] == [f"repo-{i}-{s}" for i, s in enumerate(["engine", "tools", "third"][: len(refs)], 1)]
    for (key, rel), ref in zip(segs, refs):
        assert rel == f"{SCRATCH}/{key}/brief.md"
        text = (tmp_path / rel).read_text(encoding="utf-8")
        assert BRIEF in text and ref in text
        assert "alone" in text and "output path in the brief does not apply" in text
        assert "\r" not in text
    assert compare == f"{SCRATCH}/repo-compare/brief.md"
    ctext = (tmp_path / compare).read_text(encoding="utf-8")
    for key, _ in segs:
        assert f"{SCRATCH}/{key}/synthesis.md" in ctext
        assert f"{SCRATCH}/{key}/synthesis-pass-2.md" in ctext


@pytest.mark.parametrize("targets", [[], _targets("/a/one"), [{"source": "web", "ref": "x"}, {"source": "repo", "ref": "/a"}]])
def test_under_two_repo_targets_write_nothing(tmp_path, targets):
    assert re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, targets) == ([], "")
    assert _files(tmp_path) == []


def test_same_basename_gets_distinct_keys(tmp_path):
    segs, _ = re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/engine", "/b/engine"))
    assert [k for k, _ in segs] == ["repo-1-engine", "repo-2-engine"]


def test_separator_only_ref_is_refused(tmp_path):
    with pytest.raises(PipelineEmitRefused) as exc:
        re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/ok", "//"))
    assert "target 2" in str(exc.value.args[0])


def test_long_ref_key_is_cut_to_the_limit(tmp_path):
    segs, _ = re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/" + "x" * 80, "/b/y"))
    assert len(segs[0][0]) <= re_.MAX_SLUG_LEN


_SHAPE = {"pipelines": ["repo"], "flags": {}}


def test_segments_for_expands_repo_then_compare(tmp_path):
    segs, compare = re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/one", "/b/two"))
    out = re_.segments_for(_SHAPE, brief_rel=BRIEF, scratch_rel=SCRATCH, repo_segments=segs, compare_brief=compare)
    assert [p for p, _ in out] == ["repo", "repo", "repo-compare"]
    assert [i.segment_key for _, i in out] == ["repo-1-one", "repo-2-two", None]
    assert [i.brief for _, i in out] == [segs[0][1], segs[1][1], compare]
    assert all(i.scratch_dir == SCRATCH for _, i in out)


def test_segments_for_without_repo_segments_is_unchanged():
    out = re_.segments_for(_SHAPE, brief_rel=BRIEF, scratch_rel=SCRATCH)
    assert len(out) == 1 and out[0][0] == "repo" and out[0][1].segment_key is None and out[0][1].brief == BRIEF


def test_compare_output_on_fixture_manifest():
    assert re_.compare_output(load_manifest(FIXTURE_ROOT, re_.REPO_COMPARE_PIPELINE)) == "comparison.md"
