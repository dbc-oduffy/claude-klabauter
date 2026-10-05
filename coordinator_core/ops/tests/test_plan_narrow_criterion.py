"""coordinator_core/ops/tests/test_plan_narrow_criterion.py — "plan.narrow_criterion", the mint waiver, the approval hint."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.frontmatter.primitives import approval_body_sha
from coordinator_core.frontmatter.schema_validate import compute_grouping_digest
from coordinator_core.ops import plan_narrow_criterion as mod
from coordinator_core.ops import review_stamp as rs
from coordinator_core.ops.ceremony import git_native

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OLD = "Guard fails any session leaving entries outside the repo; the fleet and DoE sweeps find zero offenders"
NEW = "Guard fails any DoE session leaving entries outside the repo; the DoE sweep finds zero offenders"
ROWS = [{"id": "C3", "disposition": "spun_off"}, {"id": "C6", "disposition": "spun_off"}]
DIGEST = compute_grouping_digest(ROWS, "spun_off")

TEMPLATE = """---
title: t
status: executing
plan_id: pln-x-111111
prime_exit_criterion:
  statement: "{statement}"
  derived_from: "state/sizings/x.yaml"
  falsifier:
    how: "run it"
grouping_approvals:
  spun_off:
    status: {status}
    approver: PM
    pm_utterance: "{utterance}"
    digest: "{digest}"
---

## Tasks

```yaml plan-tasks
- id: C3
  title: a
  disposition: spun_off
  criterion: "{leg}"
- id: C6
  title: b
  disposition: {c6}
```
"""


def _plan(statement=OLD, status="approved", utterance="yes spun off", digest=DIGEST, c6="spun_off", leg="n/a"):
    return TEMPLATE.format(statement=statement, status=status, utterance=utterance, digest=digest, c6=c6, leg=leg)


def _git(root, *a):
    subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs/plans").mkdir(parents=True)
    commits = []

    def fake_commit(rel, content, msg, cwd, **kw):
        commits.append(rel)
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_native, "commit_authored_content", fake_commit)
    monkeypatch.setattr(mod, "main_worktree_root", lambda r: r)
    return tmp_path, commits


def _run(root, text, statement=NEW, grouping="spun_off"):
    (root / "docs/plans/p.md").write_text(text)
    return mod._handler({"plan": "docs/plans/p.md", "grouping": grouping, "statement": statement}, root)


def test_narrows_writes_narrowed_by_and_commits(repo):
    root, commits = repo
    res = _run(root, _plan())
    assert res["committed"] and commits == ["docs/plans/p.md"]
    new = (root / "docs/plans/p.md").read_text()
    assert mod.narrowing_problem(new) is None
    assert approval_body_sha(new) == approval_body_sha(_plan())
    assert "  falsifier:\n    how: \"run it\"" in new
    again = _run(root, new)
    assert not again["committed"] and (root / "docs/plans/p.md").read_text() == new


@pytest.mark.parametrize(
    "text,match",
    [
        (_plan(status="pending"), "not approved"),
        (_plan(utterance=""), "no pm_utterance"),
        (_plan(digest="sha256:" + "0" * 64), "digest does not match"),
        (_plan(c6="open"), "digest does not match"),
        (_plan().replace("grouping_approvals:\n  spun_off:", "grouping_approvals:\n  defer:"), "not approved"),
    ],
)
def test_forged_or_missing_grouping_refuses(repo, text, match):
    root, commits = repo
    with pytest.raises(ValueError, match=match):
        _run(root, text)
    assert commits == [] and (root / "docs/plans/p.md").read_text() == text


def test_other_grouping_refuses(repo):
    with pytest.raises(ValueError, match="only 'spun_off'"):
        _run(repo[0], _plan(), grouping="defer")


def _narrowed(repo):
    _run(repo[0], _plan())
    return (repo[0] / "docs/plans/p.md").read_text()


def test_forged_narrowed_by_does_not_validate(repo):
    text = _narrowed(repo)
    assert mod.narrowing_problem(_plan()) == "plan carries no structured narrowed_by"
    assert "digest" in mod.narrowing_problem(text.replace(DIGEST, "sha256:" + "1" * 64, 1))
    assert "grouping is missing" in mod.narrowing_problem(text.replace("grouping: \"spun_off\"", "grouping: 7"))


def test_waiver_only_for_pre_narrowing_verdict(repo):
    text = _narrowed(repo)
    nat = text.split('narrowed_at: "')[1].split('"')[0]
    nm = {"status": "not_met", "observation": "fleet leg false"}
    assert "narrowed by spun_off; judge verdict pre-dates the narrowing" in mod.criterion_waiver(text, nm, "2000-01-01T00:00:00Z")
    assert mod.criterion_waiver(text, nm, "2999-01-01T00:00:00Z") is None
    assert mod.criterion_waiver(text, nm, None) is None
    assert mod.criterion_waiver(text, {**nm, "statement": OLD}, None)
    assert mod.criterion_waiver(text, {**nm, "statement": NEW}, "2000-01-01T00:00:00Z") is None
    assert mod.criterion_waiver(_plan(), nm, "2000-01-01T00:00:00Z") is None
    assert nat


def test_hint_when_row_leg_names_prime_text():
    named = _plan(leg=OLD)
    assert "plan.narrow_criterion" in mod.narrow_call_hint(named, ["C3"])
    assert mod.narrow_call_hint(named, ["C6"]) is None
    assert mod.narrow_call_hint(_plan(), ["C3"]) is None


def _mint(tmp_path, plan_text, criterion, delivery="PASS", tests="pass"):
    repo = tmp_path / "mrepo"
    (repo / "docs/plans").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b")
    _git(repo, "config", "user.name", "a")
    plan = repo / "docs/plans/p.md"
    plan.write_text(plan_text)
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "i")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, **no_console_creationflags()).stdout.strip()
    rec = repo / "rec.md"
    rec.write_text("x")
    data = {
        "prep": {"run_base_sha": head, "product_files": 1, "slice_files": ["a.txt"], "foreign_claims": []},
        "delivery": {"verdict": delivery}, "criterion": criterion,
        "tests": {"status": tests, "run": 1, "failed": 0, "sidecar": None},
        "unresolved": [], "slices": 1, "recorded_at": "2000-01-01T00:00:00Z",
    }
    return rs.mint(plan, repo, build_test_path=None, resolved=(head, rec, data)), plan


NM = {"status": "not_met", "observation": "fleet leg false", "sidecar": None}


def test_mint_accepts_pre_narrowing_not_met(repo, tmp_path):
    stamp, plan = _mint(tmp_path, _narrowed(repo), NM)
    assert stamp["criterion"]["observation"] == "criterion narrowed by spun_off; judge verdict pre-dates the narrowing"
    assert stamp["criterion"]["status"] == "not_run"


@pytest.mark.parametrize(
    "criterion,delivery,tests",
    [
        ({**NM, "statement": NEW}, "PASS", "pass"),
        (NM, "FAIL", "pass"),
        (NM, "PASS", "fail"),
    ],
)
def test_mint_still_refuses(repo, tmp_path, criterion, delivery, tests):
    with pytest.raises(rs.MintRefusal):
        _mint(tmp_path, _narrowed(repo), criterion, delivery, tests)


def test_mint_refuses_not_met_on_plan_without_narrowing(tmp_path):
    with pytest.raises(rs.MintRefusal, match="exit criterion is not_met"):
        _mint(tmp_path, _plan(), NM)
