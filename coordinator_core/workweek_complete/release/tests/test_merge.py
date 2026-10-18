"""merge_pinned: order of children, the pin, and the refusals, against a bare origin and a fake gh."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.workweek_complete.release import contract, merge
from coordinator_core.workweek_complete.release.merge import ARGV, Child, MergeRefused, merge_pinned


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "a.txt").write_text("a")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "one")
    candidate = _git(work, "rev-parse", "HEAD")
    return origin, work, candidate


class Recorder:
    """Real git for the push, scripted answers for gh; records every argv."""

    def __init__(self, prs=None, merge_rc=0, view=None, create_out="https://github.com/o/r/pull/7\n"):
        self.calls = []
        self.prs = prs or []
        self.merge_rc = merge_rc
        self.view = view
        self.create_out = create_out

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        if argv[0] == "git":
            return merge._default_run(argv, cwd)
        sub = argv[1:3]
        if sub == ("pr", "list"):
            return Child(0, json.dumps(self.prs), "")
        if sub == ("pr", "create"):
            return Child(0, self.create_out, "")
        if sub == ("pr", "merge"):
            return Child(self.merge_rc, "", "refused by gh" if self.merge_rc else "")
        if sub == ("pr", "view"):
            return Child(0, json.dumps(self.view), "")
        raise AssertionError(argv)

    def names(self):
        return [" ".join(a[:3]) if a[0] == "gh" else a[3] for a in self.calls]


def _merged_view(candidate, oid="m" * 40):
    return {"state": "MERGED", "headRefOid": candidate, "mergeCommit": {"oid": oid}}


def _call(repo, rec, tmp_path, **kw):
    _origin, work, candidate = repo
    return merge_pinned(
        str(work), candidate=candidate, branch="release/x", title="t", body="b",
        run_dir=str(tmp_path / "run"), post_status=None, run=rec, **kw,
    )


def test_already_merged_head_starts_one_child(repo, tmp_path):
    _o, _w, cand = repo
    rec = Recorder(prs=[{"number": 3, "state": "MERGED", "headRefOid": cand, "mergeCommit": {"oid": "f" * 40}}])
    res = _call(repo, rec, tmp_path)
    assert (res.merge_sha, res.pr_number, res.merged_now) == ("f" * 40, 3, False)
    assert len(rec.calls) == 1


def test_open_pull_request_is_reused(repo, tmp_path):
    _o, _w, cand = repo
    rec = Recorder(prs=[{"number": 5, "state": "OPEN", "headRefOid": "0" * 40}], view=_merged_view(cand))
    res = _call(repo, rec, tmp_path)
    assert res.pr_number == 5 and res.merged_now
    assert not any(c[1:3] == ("pr", "create") for c in rec.calls)


def test_merge_argv_pins_the_candidate_with_a_merge_commit(repo, tmp_path):
    _o, _w, cand = repo
    rec = Recorder(view=_merged_view(cand))
    res = _call(repo, rec, tmp_path)
    assert res.pr_number == 7
    argv = next(c for c in rec.calls if c[1:3] == ("pr", "merge"))
    assert "--merge" in argv
    assert argv[argv.index("--match-head-commit") + 1] == cand
    create = next(c for c in rec.calls if c[1:3] == ("pr", "create"))
    assert (tmp_path / "run" / create[create.index("--body-file") + 1].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]).read_text() == "b"


def test_rejected_push_raises_before_any_gh_write(repo, tmp_path):
    origin, work, cand = repo
    _git(work, "push", "origin", "HEAD:refs/heads/release/x")
    (work / "b.txt").write_text("b")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "two")
    later = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "HEAD:refs/heads/release/x")
    rec = Recorder(view=_merged_view(cand))
    with pytest.raises(MergeRefused) as exc:
        _call(repo, rec, tmp_path)
    assert exc.value.reason == "push-rejected"
    assert not any(c[1:3] in (("pr", "create"), ("pr", "merge")) for c in rec.calls)
    assert _git(origin, "rev-parse", "refs/heads/release/x") == later


def test_unconfirmed_merge_raises(repo, tmp_path):
    _o, _w, cand = repo
    rec = Recorder(view={"state": "OPEN", "headRefOid": cand, "mergeCommit": None})
    with pytest.raises(MergeRefused) as exc:
        _call(repo, rec, tmp_path)
    assert exc.value.reason == "merge-not-confirmed"


def test_view_of_another_head_is_not_confirmed(repo, tmp_path):
    rec = Recorder(view=_merged_view("9" * 40))
    with pytest.raises(MergeRefused) as exc:
        _call(repo, rec, tmp_path)
    assert exc.value.reason == "merge-not-confirmed"


def test_merge_refusal_carries_gh_stderr(repo, tmp_path):
    rec = Recorder(merge_rc=1)
    with pytest.raises(MergeRefused) as exc:
        _call(repo, rec, tmp_path)
    assert exc.value.reason == "merge-refused" and "refused by gh" in exc.value.detail


def test_local_branch_past_the_candidate_sends_only_the_candidate(repo, tmp_path):
    origin, work, cand = repo
    for name in ("b", "c"):
        (work / f"{name}.txt").write_text(name)
        _git(work, "add", f"{name}.txt")
        _git(work, "commit", "-m", name)
    rec = Recorder(view=_merged_view(cand))
    _call(repo, rec, tmp_path)
    assert _git(origin, "rev-parse", "refs/heads/release/x") == cand


def test_origin_branch_past_the_candidate_is_rejected_and_unchanged(repo, tmp_path):
    origin, work, cand = repo
    (work / "b.txt").write_text("b")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "two")
    later = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "HEAD:refs/heads/release/x")
    rec = Recorder(view=_merged_view(cand))
    with pytest.raises(MergeRefused) as exc:
        _call(repo, rec, tmp_path)
    assert exc.value.reason == "push-rejected"
    assert _git(origin, "rev-parse", "refs/heads/release/x") == later


def test_no_forbidden_token_force_refspec_or_main_destination(repo, tmp_path):
    _o, _w, cand = repo
    rec = Recorder(view=_merged_view(cand))
    _call(repo, rec, tmp_path)
    argvs = [list(a) for a in ARGV.values()] + [list(c) for c in rec.calls]
    for argv in argvs:
        assert not set(argv) & set(contract.FORBIDDEN_ARGV_TOKENS), argv
        for part in argv:
            assert not part.startswith("+")
        if "push" in argv:
            dest = argv[-1].split(":", 1)[-1]
            assert dest.rsplit("/", 1)[-1] != "main"
