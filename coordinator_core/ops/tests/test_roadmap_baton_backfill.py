"""roadmap.baton_backfill: dry run by default, met_by credit proven before any write, one commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import git_ancestry
from coordinator_core.ops import roadmap_baton_backfill as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

_ROW = "- id: {id}\n  title: t\n  change_kind: code-edit\n  surface: a.py\n  disposition: {disp}\n  deferred: false\n"
PLAN = "docs/plans/p.md"
BATON = "state/handoffs/a.md"


class _Repo(type(Path())):
    """A tmp repo root that also carries the two fixture commit shas."""


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _write_plan(root, dispositions, rel=PLAN):
    rows = "".join(_ROW.format(id=f"C{i}", disp=d) for i, d in enumerate(dispositions))
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: executing\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )


def _write_baton(root, name="a", rel_dir="state/handoffs", **extra):
    fields = {"stub_id": name, "baton_role": "work", "status": "open",
              "deployment_state": "ready_to_fire", "governing_plan": PLAN, **extra}
    path = root / rel_dir / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\nbody\n", encoding="utf-8")


def _coded(sha):
    return f"coded\n  disposition_ref: {sha}"


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("1")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    first = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "f.txt").write_text("2")
    _git(tmp_path, "commit", "-qam", "second")
    second = _git(tmp_path, "rev-parse", "HEAD")
    root = _Repo(tmp_path)
    root.first, root.second = first, second
    return root


def _track(root):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "records")


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git/" not in p.as_posix() and ".coordinator-local/" not in p.as_posix()}


def _call(root, **params):
    return op._handler(params, repo_root=root)



def _setup_partial(repo):
    _write_plan(repo, [_coded(repo.first), "open"])
    _write_baton(repo)
    _track(repo)


def test_default_is_a_dry_run_that_leaves_the_tree_unchanged(repo):
    _setup_partial(repo)
    before, head = _snapshot(repo), _git(repo, "rev-parse", "HEAD")
    out = _call(repo, met_by={PLAN: {"C1": repo.second[:10]}})
    assert out["dry_run"] is True and out["refused"] == {} and out["stamped"] == []
    assert out["proposals"] == [
        {"baton": BATON, "plan": PLAN, "shipped_in": repo.second, "rows_coded": ["C1"]}
    ]
    assert _snapshot(repo) == before and _git(repo, "rev-parse", "HEAD") == head


def test_an_open_row_without_credit_proposes_nothing(repo):
    _setup_partial(repo)
    out = _call(repo)
    assert out["proposals"] == [] and out["refused"] == {}


def test_already_landed_plan_is_proposed_without_met_by(repo):
    _write_plan(repo, [_coded(repo.first), _coded(repo.second)])
    _write_baton(repo)
    _track(repo)
    out = _call(repo)
    assert [(p["baton"], p["shipped_in"], p["rows_coded"]) for p in out["proposals"]] == [(BATON, repo.second, [])]


def test_apply_codes_credited_rows_stamps_the_baton_and_lands_one_commit(repo):
    _setup_partial(repo)
    out = _call(repo, dry_run=False, met_by={PLAN: {"C1": repo.second[:10]}})
    assert out["refused"] == {} and out["stamped"] == [BATON]
    assert out["commit_sha"] == _git(repo, "rev-parse", "HEAD")
    baton = (repo / BATON).read_text(encoding="utf-8")
    assert "deployment_state: shipped" in baton
    assert f"shipped_in: {repo.second}" in baton and "shipped_in_kind: ship-commit" in baton
    plan = (repo / PLAN).read_text(encoding="utf-8")
    assert f"disposition_ref: {repo.second}" in plan and "disposition: open" not in plan
    assert sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").split()) == [PLAN, BATON]


def test_a_non_ancestor_sha_refuses_naming_the_row_and_writes_nothing(repo):
    _setup_partial(repo)
    _git(repo, "checkout", "-q", "-b", "side", repo.first)
    (repo / "g.txt").write_text("x")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    before = _snapshot(repo)
    out = _call(repo, dry_run=False, met_by={PLAN: {"C1": side}})
    assert list(out["refused"]) == [f"{PLAN}:C1"] and out["stamped"] == [] and out["commit_sha"] is None
    assert _snapshot(repo) == before


def test_an_unresolvable_sha_refuses_the_call(repo):
    _setup_partial(repo)
    before = _snapshot(repo)
    out = _call(repo, dry_run=False, met_by={PLAN: {"C1": "deadbeef0"}})
    assert "met_by" in out["refused"] and out["proposals"] == [] and _snapshot(repo) == before


def test_a_failed_ancestry_read_refuses_as_unverifiable(repo, monkeypatch):
    _setup_partial(repo)
    monkeypatch.setattr(git_ancestry, "ancestors_of", lambda *a, **k: (False, None))
    out = _call(repo, dry_run=False, met_by={PLAN: {"C1": repo.second}})
    assert "could not be read" in out["refused"]["met_by"] and out["stamped"] == []
    assert "disposition: open" in (repo / PLAN).read_text(encoding="utf-8")


def test_met_by_naming_a_row_that_is_not_open_refuses(repo):
    _setup_partial(repo)
    out = _call(repo, dry_run=False, met_by={PLAN: {"C0": repo.second, "C1": repo.second}})
    assert list(out["refused"]) == [f"{PLAN}:C0"] and out["stamped"] == []


def test_met_by_naming_a_plan_no_baton_links_refuses(repo):
    _setup_partial(repo)
    out = _call(repo, met_by={"docs/plans/other.md": {"C0": repo.second}})
    assert list(out["refused"]) == ["docs/plans/other.md"] and out["proposals"] == []


def test_an_archived_baton_and_a_terminal_baton_are_never_touched(repo):
    _write_plan(repo, [_coded(repo.first)])
    _write_baton(repo, "arch", rel_dir="archive/handoffs")
    _write_baton(repo, "done", deployment_state="shipped", shipped_in=repo.first)
    _track(repo)
    before = _snapshot(repo)
    out = _call(repo, dry_run=False)
    assert out["proposals"] == [] and out["stamped"] == [] and _snapshot(repo) == before


def test_batons_filter_narrows_the_selection(repo):
    _write_plan(repo, [_coded(repo.first)])
    _write_baton(repo, "a")
    _write_baton(repo, "b")
    _track(repo)
    out = _call(repo, batons=["b"])
    assert [p["baton"] for p in out["proposals"]] == ["state/handoffs/b.md"]


def test_a_landed_sha_that_is_not_reachable_is_a_per_baton_refusal(repo):
    _write_plan(repo, [_coded("1" * 40)])
    _write_baton(repo)
    _track(repo)
    out = _call(repo, dry_run=False)
    assert out["stamped"] == [] and list(out["refused"]) == [BATON]
    assert "deployment_state: ready_to_fire" in (repo / BATON).read_text(encoding="utf-8")


def test_a_failed_commit_restores_every_written_file(repo, monkeypatch):
    from coordinator_core.roadmap import baton_backfill

    _setup_partial(repo)
    before = _snapshot(repo)
    monkeypatch.setattr(baton_backfill, "_commit", lambda params, root: {"committed": False, "error": "nope"})
    out = _call(repo, dry_run=False, met_by={PLAN: {"C1": repo.second}})
    assert "commit" in out["refused"] and out["stamped"] == []
    assert _snapshot(repo) == before


@pytest.mark.parametrize("params", [
    {"dry_run": "no"}, {"batons": "a"}, {"met_by": []}, {"met_by": {PLAN: {"C1": "zz"}}},
])
def test_malformed_params_raise(repo, params):
    with pytest.raises(ValueError):
        _call(repo, **params)
