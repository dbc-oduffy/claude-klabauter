"""primary repo mode: a lone repo target is keyed; gap briefs rank code over web and bind through segments_for."""

from coordinator_core.ops.dispatch_emit import research_emit as re_

SCRATCH = "scratch/run"
BRIEF = "scratch/run/ask.md"
SYN = [f"{SCRATCH}/repo-1-engine/synthesis.md"]


def _targets(*refs):
    return [{"source": "repo", "ref": r} for r in refs]


def test_primary_keys_a_lone_target_without_compare(tmp_path):
    segs, compare = re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/engine"), primary=True)
    assert [k for k, _ in segs] == ["repo-1-engine"] and compare == ""
    assert (tmp_path / segs[0][1]).is_file()


def test_lone_target_without_primary_still_folds(tmp_path):
    assert re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/engine")) == ([], "")


def test_primary_with_no_repo_target_writes_nothing(tmp_path):
    assert re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, [], primary=True) == ([], "")
    assert list(tmp_path.rglob("*")) == []


def test_primary_two_targets_still_compare(tmp_path):
    segs, compare = re_.bind_repo_targets(tmp_path, SCRATCH, BRIEF, _targets("/a/e", "/b/t"), primary=True)
    assert len(segs) == 2 and compare == f"{SCRATCH}/repo-compare/brief.md"


def test_gap_briefs_cover_late_pipelines_only(tmp_path):
    out = re_.bind_gap_briefs(
        tmp_path, SCRATCH, BRIEF, ["repo", "repo-compare", "nlm-preflight", "web", "notebooklm", "unblock"], SYN,
    )
    assert set(out) == {"web", "notebooklm", "unblock"}
    for pipeline, rel in out.items():
        assert rel == f"{SCRATCH}/{pipeline}-after-repo/brief.md"
        text = (tmp_path / rel).read_text(encoding="utf-8")
        assert BRIEF in text and SYN[0] in text and "## Open Questions" in text
        assert "Rank code claims above web claims" in text and "the repo claim stands" in text
        assert "\r" not in text


def test_gap_briefs_empty_when_only_repo_pipelines(tmp_path):
    assert re_.bind_gap_briefs(tmp_path, SCRATCH, BRIEF, ["repo", "nlm-preflight"], SYN) == {}
    assert list(tmp_path.rglob("*")) == []


def test_segments_for_binds_gap_brief_over_seed_brief():
    shape = {"pipelines": ["repo", "web"], "flags": {}}
    segs = re_.segments_for(
        shape, brief_rel=BRIEF, scratch_rel=SCRATCH,
        seed_briefs={"web": "seed.md"}, gap_briefs={"web": "gap.md"},
    )
    by = dict(segs)
    assert by["web"].brief == "gap.md" and by["repo"].brief == BRIEF
    plain = dict(re_.segments_for(shape, brief_rel=BRIEF, scratch_rel=SCRATCH))
    assert plain["web"].brief == BRIEF
