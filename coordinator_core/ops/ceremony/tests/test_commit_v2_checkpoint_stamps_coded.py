"""`ceremony.commit_v2` stamps a landed checkpoint's rows coded at the checkpoint's own sha."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PLAN = "docs/plans/p.md"
_ROW_FILE = "src/row_a.txt"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _plan_text() -> str:
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n"
        for i in ("C1", "C2")
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _rows(repo) -> dict:
    text = (repo / _PLAN).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r for r in yaml.safe_load(block)}


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _w(r / "seed.txt", "x\n")
    _w(r / _PLAN, _plan_text())
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _checkpoint(repo, subject="checkpoint(wave 0): 1 rows — C1", extra=""):
    base = _git(repo, "rev-parse", "HEAD")
    _w(repo / _ROW_FILE, "row\n")
    message = f"{subject}\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {base}\n{extra}"
    return commit_v2._handler(
        {"paths": [_ROW_FILE], "message": message}, repo_root=repo / ".git"
    )


def test_a_checkpoint_stamps_its_row_coded_at_its_own_sha(repo):
    c2_before = _rows(repo)["C2"]

    out = _checkpoint(repo)

    assert out["committed"] is True, out
    sha = out["sha"]
    assert _git(repo, "show", "--name-only", "--format=", sha).split() == [_ROW_FILE]
    rows = _rows(repo)
    assert rows["C1"]["disposition"] == "coded" and rows["C1"]["disposition_ref"] == sha
    assert rows["C2"] == c2_before
    assert out["checkpoint_stamp"]["rows_coded"] == {_PLAN: ["C1"]}
    stamp = out["checkpoint_stamp"]["stamp_sha"]
    assert _git(repo, "rev-parse", "HEAD") == stamp and _git(repo, "rev-parse", "HEAD~1") == sha
    assert f"Checkpoint-Stamp: {sha}" in _git(repo, "log", "-1", "--format=%B")
    assert _git(repo, "status", "--porcelain", "--", _PLAN) == ""


def test_a_gate_owed_row_is_not_stamped(repo):
    out = _checkpoint(repo, extra="Checkpoint-Gate-Owed: C1\n")

    assert out["committed"] is True, out
    assert _rows(repo)["C1"]["disposition"] == "open"
    assert "checkpoint_stamp" not in out


def test_a_stamp_failure_never_fails_the_landed_checkpoint(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import checkpoint_stamp

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(checkpoint_stamp, "stamp_checkpoint", boom)

    out = _checkpoint(repo)

    assert out["committed"] is True and out["sha"] == _git(repo, "rev-parse", "HEAD")
    assert "boom" in out["checkpoint_stamp"]["stamp_error"]


def test_a_non_checkpoint_commit_carries_no_checkpoint_stamp_key(repo):
    _w(repo / _ROW_FILE, "row\n")
    out = commit_v2._handler({"paths": [_ROW_FILE], "message": "edit"}, repo_root=repo / ".git")
    assert out["committed"] is True and "checkpoint_stamp" not in out


def test_a_checkpoint_call_spawns_exactly_what_a_plain_call_does(repo, monkeypatch):
    calls: list = []
    real = commit_v2.run_git

    def counting(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(commit_v2, "run_git", counting)
    import coordinator_core.git.run as git_run

    real_run = git_run.run_git
    monkeypatch.setattr(
        git_run, "run_git", lambda argv, *a, **k: (calls.append(list(argv)), real_run(argv, *a, **k))[1]
    )

    _w(repo / "plain.txt", "p\n")
    plain = commit_v2._handler({"paths": ["plain.txt"], "message": "plain"}, repo_root=repo / ".git")
    plain_calls, calls[:] = list(calls), []
    out = _checkpoint(repo)

    assert plain["committed"] is True and out["checkpoint_stamp"]["stamp_sha"]
    assert len(calls) == len(plain_calls), (calls, plain_calls)
