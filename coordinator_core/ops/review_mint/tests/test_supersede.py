"""Tests for review_mint.supersede in a scratch git repo."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.review_mint import supersede
from coordinator_core.ops.review_mint.supersede import (
    SupersedeRefused,
    record_superseding_review,
)
from coordinator_core.win_portability import no_console_creationflags


pytestmark = pytest.mark.spawns_process


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    shas = []
    for i, trailer in enumerate(["", "\n\nSession-Id: other-session", ""]):
        (tmp_path / f"f{i}.txt").write_text(str(i))
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-q", "-m", f"c{i}{trailer}")
        shas.append(_git(tmp_path, "rev-parse", "HEAD"))
    return tmp_path, shas


def _call(root, base, head, **kw):
    return record_superseding_review(
        repo_root=root, plan="pln-x-123456",
        commit_range={"base": base, "head": head},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1", **kw,
    )


def test_record_written_with_pinned_keys(repo):
    root, s = repo
    res = _call(root, s[0], s[2], supersedes=s[1])
    fm = yaml.safe_load((root / res["record_path"]).read_text().split("---\n")[1])
    assert fm["kind"] == "superseding-review"
    assert fm["plan_id"] == "pln-x-123456"
    assert fm["commit_range"] == {"base": s[0], "head": s[2]}
    assert fm["supersedes"] == s[1]
    assert "integrated_from" in fm and "fixes_applied" in fm


def test_second_write_refused(repo):
    root, s = repo
    _call(root, s[0], s[2])
    with pytest.raises(SupersedeRefused):
        _call(root, s[0], s[2])


def test_non_ancestor_head_refused(repo):
    root, s = repo
    _git(root, "checkout", "-q", "-b", "side", s[0])
    (root / "side.txt").write_text("x")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "side")
    side = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-")
    with pytest.raises(SupersedeRefused):
        _call(root, s[0], side)


def test_base_equals_head_and_reversed_refused(repo):
    root, s = repo
    with pytest.raises(SupersedeRefused):
        _call(root, s[1], s[1])
    with pytest.raises(SupersedeRefused):
        _call(root, s[2], s[0])


def test_foreign_session_trailer_in_range_accepted(repo):
    root, s = repo
    assert "Session-Id: other-session" in _git(root, "log", "-1", "--format=%B", s[1])
    assert _call(root, s[0], s[2])["record_path"].endswith(".superseding.md")


def test_spawn_count_at_most_two(repo, monkeypatch):
    root, s = repo
    calls = []
    real = subprocess.Popen

    class counting(real):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting)
    _call(root, s[0], s[2])
    assert len(calls) <= 2


def test_plan_path_resolves_to_frontmatter_plan_id(repo):
    root, s = repo
    plan = root / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-from-fm-abcdef\n---\nbody\n", encoding="utf-8")
    res = record_superseding_review(
        repo_root=root, plan="docs/plans/p.md",
        commit_range={"base": s[0], "head": s[2]},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1",
    )
    assert res["record"]["plan_id"] == "pln-from-fm-abcdef"
    assert "docs/plans" not in res["record_path"]


def test_plan_path_without_plan_id_refused(repo):
    root, s = repo
    (root / "p.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")
    with pytest.raises(SupersedeRefused):
        record_superseding_review(
            repo_root=root, plan="p.md",
            commit_range={"base": s[0], "head": s[2]},
            wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
            session_id="sess-1",
        )


PLAN = "pln-x-123456"


def _sc(share, name, **fm):
    share.mkdir(parents=True, exist_ok=True)
    body = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\nobserved: the criterion holds\n"
    (share / name).write_text(body, encoding="utf-8")
    return share / name


def _full_share(root, plan=PLAN, suffix=""):
    share = root / ".coordinator-local" / "subagent-share" / "sess-1"
    _sc(share, f"coordinator-code-reviewer.a1{suffix}.md", agent_type="coordinator:code-reviewer", target_plan=plan)
    _sc(share, f"coordinator-test-runner.p1{suffix}.md", agent_type="coordinator:test-runner",
        target_plan=plan, status="open", run_base_sha="abc1234", product_files=2,
        whole_diff_sidecars={"kira": "k", "personas": [], "delivery": "d"})
    _sc(share, f"coordinator-test-runner.t1{suffix}.md", agent_type="coordinator:test-runner",
        target_plan=plan, status="pass", run=4, failed=0)
    _sc(share, f"coordinator-delivery-verifier.d1{suffix}.md", agent_type="coordinator:delivery-verifier",
        plan=plan, verdict="PASS")
    _sc(share, f"coordinator-exit-criterion-judge.j1{suffix}.md", agent_type="coordinator:exit-criterion-judge",
        plan=plan, status="met")
    return share


def _share_call(root, s, **kw):
    return record_superseding_review(
        repo_root=root, plan=PLAN, commit_range={"base": s[0], "head": s[2]},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1", from_share=True, **kw,
    )


def test_from_share_full_set_assembles_and_records(repo):
    root, s = repo
    _full_share(root)
    res = _share_call(root, s)
    fm = yaml.safe_load((root / res["record_path"]).read_text().split("---\n")[1])
    assert fm["criterion"]["status"] == "met"
    assert fm["criterion"]["observation"] == "observed: the criterion holds"
    assert fm["delivery"]["verdict"] == "PASS"
    assert fm["tests"] == {"status": "pass", "sidecar": ".coordinator-local/subagent-share/sess-1/coordinator-test-runner.t1.md", "run": 4, "failed": 0}
    assert fm["prep_sidecar"].endswith("coordinator-test-runner.p1.md")
    assert res["used"]["criterion"] == [".coordinator-local/subagent-share/sess-1/coordinator-exit-criterion-judge.j1.md"]
    assert res["used"]["reviewer"] == [".coordinator-local/subagent-share/sess-1/coordinator-code-reviewer.a1.md"]


def test_from_share_missing_judge_refuses_and_names_it(repo):
    root, s = repo
    share = _full_share(root)
    (share / "coordinator-exit-criterion-judge.j1.md").unlink()
    with pytest.raises(SupersedeRefused, match=r"no criterion sidecar.*coordinator:exit-criterion-judge"):
        _share_call(root, s)
    assert not (root / "state" / "superseding-reviews").exists()


def test_from_share_judge_older_than_fix_commit_refuses(repo):
    import os
    root, s = repo
    share = _full_share(root)
    os.utime(share / "coordinator-exit-criterion-judge.j1.md", (1, 1))
    with pytest.raises(SupersedeRefused, match="predates HEAD commit"):
        _share_call(root, s)


def test_from_share_ignores_other_plans_sidecars(repo):
    root, s = repo
    share = _full_share(root)
    (share / "coordinator-exit-criterion-judge.j1.md").unlink()
    _sc(share, "coordinator-exit-criterion-judge.jother.md",
        agent_type="coordinator:exit-criterion-judge", plan="pln-other-654321", status="met")
    _sc(share, "coordinator-code-reviewer.aother.md",
        agent_type="coordinator:code-reviewer", target_plan="pln-other-654321")
    with pytest.raises(SupersedeRefused, match="no criterion sidecar"):
        _share_call(root, s)
    _sc(share, "coordinator-exit-criterion-judge.j1.md",
        agent_type="coordinator:exit-criterion-judge", plan=PLAN, status="met")
    res = _share_call(root, s)
    assert all("other" not in p for ps in res["used"].values() for p in ps)


def test_from_share_not_met_judge_refuses(repo):
    root, s = repo
    share = _full_share(root)
    _sc(share, "coordinator-exit-criterion-judge.j1.md",
        agent_type="coordinator:exit-criterion-judge", plan=PLAN, status="not_met")
    with pytest.raises(SupersedeRefused, match="not_met"):
        _share_call(root, s)


def test_from_share_takes_the_judge_verdict_from_its_returned_json(repo):
    """The judge is provisioned no sidecar (DoE 2026-10-02); its returned
    terminal-judge-result, with `sidecar_path: ""`, is the criterion stage."""
    root, s = repo
    share = _full_share(root)
    (share / "coordinator-exit-criterion-judge.j1.md").unlink()
    res = _share_call(
        root, s, judge_result={"status": "met", "observation": "suite green at head", "sidecar_path": ""}
    )
    fm = yaml.safe_load((root / res["record_path"]).read_text().split("---\n")[1])
    assert fm["criterion"]["status"] == "met" and fm["criterion"]["observation"] == "suite green at head"
    assert res["used"]["criterion"] == ["(judge result JSON)"]
    with pytest.raises(SupersedeRefused, match="not met"):
        _share_call(root, s, judge_result={"status": "not_met", "observation": "x", "sidecar_path": ""})
