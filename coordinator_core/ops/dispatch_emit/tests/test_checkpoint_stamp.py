"""checkpoint_stamp parses a checkpoint message and stamps its open, non-gate-owed rows coded."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import ref_cas_retry
from coordinator_core.ops.dispatch_emit import checkpoint_stamp
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
    is_stamp_commit,
    parse_checkpoint,
    stamp_checkpoint,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/p.md"
SUBJECT = "checkpoint(wave 0): 2 rows — C1, C2"


def _message(subject=SUBJECT, owed=None, plan=PLAN, stopped=None) -> str:
    out = f"{subject}\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: abc1234\nSession-Id: s-1"
    out += f"\nCheckpoint-Gate-Owed: {owed}" if owed else ""
    return out + (f"\n{checkpoint_stamp.STOP_RULE_TRAILER}: {stopped}" if stopped else "")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _plan_text(rows=(("C1", "open"), ("C2", "open"), ("C3", "open"))) -> str:
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n  writes:\n    - some/{i}.py\n"
        for i, d in rows
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _repo(tmp_path: Path, text: str | None = None, *, ignore_plan: bool = False, newline="\n") -> tuple:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    plan = root / PLAN
    plan.parent.mkdir(parents=True)
    with plan.open("w", encoding="utf-8", newline=newline) as fh:
        fh.write(text if text is not None else _plan_text())
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    if ignore_plan:
        (root / ".gitignore").write_text(PLAN + "\n", encoding="utf-8")
        _git(root, "add", ".gitignore", "seed.txt")
    else:
        _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    (root / "seed.txt").write_text("checkpoint\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", _message())
    return root, _git(root, "rev-parse", "HEAD")


def _rows(root: Path) -> dict:
    import yaml

    text = (root / PLAN).read_text(encoding="utf-8")
    return {r["id"]: r for r in yaml.safe_load(text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0])}


def test_parse_checkpoint_shapes():
    cp = parse_checkpoint(_message())
    assert cp.plan_rel == PLAN and cp.row_ids == {"C1", "C2"} and cp.gate_owed == frozenset()
    owed = parse_checkpoint(_message(owed="C2"))
    assert owed.gate_owed == {"C2"} and owed.row_ids == {"C1", "C2"}
    assert parse_checkpoint(_message(subject="checkpoint(wave 3): 1 rows — C9")).row_ids == {"C9"}


def test_parse_checkpoint_reads_base_and_not_landed_marker():
    cp = parse_checkpoint(_message())
    assert (cp.base, cp.review_base, cp.not_landed) == ("abc1234", None, False)
    marked = _message() + "\nCheckpoint-Review-Base: def5678\nCheckpoint-Not-Landed: run-open"
    cp = parse_checkpoint(marked)
    assert (cp.base, cp.review_base, cp.not_landed) == ("abc1234", "def5678", True)


def test_parse_checkpoint_refuses_non_checkpoints():
    assert parse_checkpoint(f"fix: thing\n\nCheckpoint-Plan: {PLAN}") is None
    assert parse_checkpoint(SUBJECT) is None


def test_is_stamp_commit():
    assert is_stamp_commit("mark 1 rows coded (abc1234)\n\nCheckpoint-Stamp: " + "a" * 40)
    assert not is_stamp_commit(_message())


def test_open_rows_coded_and_follow_up_commit_carries_the_trailer(tmp_path):
    root, sha = _repo(tmp_path)
    c3_before = _rows(root)["C3"]

    out = stamp_checkpoint(root, _message(), sha, session_id="s-1")

    assert out["rows_coded"] == {PLAN: ["C1", "C2"]} and out["stamp_sha"]
    rows = _rows(root)
    assert rows["C1"]["disposition"] == "coded" and rows["C1"]["disposition_ref"] == sha
    assert rows["C2"]["disposition"] == "coded" and rows["C2"]["disposition_ref"] == sha
    assert rows["C3"] == c3_before
    head = _git(root, "rev-parse", "HEAD")
    assert head == out["stamp_sha"] and _git(root, "rev-parse", "HEAD~1") == sha
    body = _git(root, "log", "-1", "--format=%B")
    assert body.startswith("mark 2 rows coded (" + sha[:7] + ")")
    assert f"Checkpoint-Stamp: {sha}" in body
    assert is_stamp_commit(body)
    assert _git(root, "diff", "--name-only", "HEAD~1", "HEAD") == PLAN
    assert _git(root, "status", "--porcelain", "--", PLAN) == ""


def test_gate_owed_and_already_coded_rows_are_untouched(tmp_path):
    text = _plan_text((("C1", "coded"), ("C2", "open"), ("C3", "open")))
    root, sha = _repo(tmp_path, text)

    out = stamp_checkpoint(root, _message(subject="checkpoint(wave 0): 3 rows — C1, C2, C3", owed="C2"), sha)

    assert out["rows_coded"] == {PLAN: ["C3"]}
    rows = _rows(root)
    assert rows["C2"]["disposition"] == "open" and "disposition_ref" not in rows["C2"]
    assert rows["C1"]["disposition"] == "coded" and "disposition_ref" not in rows["C1"]
    assert rows["C3"]["disposition_ref"] == sha


def test_nothing_open_returns_empty_and_no_commit(tmp_path):
    root, sha = _repo(tmp_path, _plan_text((("C1", "coded"), ("C2", "coded"))))
    assert stamp_checkpoint(root, _message(), sha) == {}
    assert _git(root, "rev-parse", "HEAD") == sha


def test_schema_refused_stamp_restores_the_bytes(tmp_path, monkeypatch):
    from coordinator_core.frontmatter import schema_validate

    root, sha = _repo(tmp_path)
    before = (root / PLAN).read_bytes()
    monkeypatch.setattr(
        schema_validate, "check_plan_tasks_source", lambda text: "bad" if "coded" in text else None
    )

    out = stamp_checkpoint(root, _message(), sha)

    assert "stamp_error" in out and "bad" in out["stamp_error"]
    assert (root / PLAN).read_bytes() == before
    assert _git(root, "rev-parse", "HEAD") == sha


def test_refused_follow_up_commit_restores_the_bytes(tmp_path, monkeypatch):
    from coordinator_core.git import commit

    root, sha = _repo(tmp_path)
    before = (root / PLAN).read_bytes()

    def boom(*a, **k):
        raise commit.CommitRefused("no")

    monkeypatch.setattr(commit, "commit_paths", boom)

    out = stamp_checkpoint(root, _message(), sha)

    assert "stamp_error" in out
    assert (root / PLAN).read_bytes() == before


def _cas_loser(root, real, losses, exc_factory=None):
    """commit_paths stand-in: a peer commits, then this call loses the ref CAS, `losses` times."""
    from coordinator_core.git import commit

    calls = {"n": 0}

    def wrapper(*a, **k):
        calls["n"] += 1
        if calls["n"] <= losses:
            (root / "peer.txt").write_text(f"peer {calls['n']}\n", encoding="utf-8")
            _git(root, "add", "peer.txt")
            _git(root, "commit", "-q", "-m", f"peer {calls['n']}")
            raise (exc_factory or (lambda: commit.CommitRefused(
                "compare-and-swap failed -- refs/heads/x moved since abc was captured.")))()
        return real(*a, **k)

    return wrapper, calls


def test_peer_commit_between_head_read_and_cas_is_retried_and_lands(tmp_path, monkeypatch):
    from coordinator_core.git import commit

    root, sha = _repo(tmp_path)
    wrapper, calls = _cas_loser(root, commit.commit_paths, 1)
    monkeypatch.setattr(commit, "commit_paths", wrapper)

    out = stamp_checkpoint(root, _message(), sha)

    assert "stamp_error" not in out and out["stamp_sha"]
    assert calls["n"] == 2
    assert _git(root, "rev-parse", "HEAD") == out["stamp_sha"]
    assert _git(root, "log", "-1", "--format=%P") == _git(root, "rev-parse", "HEAD~1")
    assert _git(root, "log", "-2", "--format=%s").splitlines()[1] == "peer 1"
    assert _rows(root)["C1"]["disposition"] == "coded"


def test_cas_retries_are_bounded_then_stamp_error_and_restore(tmp_path, monkeypatch):
    from coordinator_core.git import commit

    root, sha = _repo(tmp_path)
    before = (root / PLAN).read_bytes()
    wrapper, calls = _cas_loser(root, commit.commit_paths, 99)
    monkeypatch.setattr(commit, "commit_paths", wrapper)

    out = stamp_checkpoint(root, _message(), sha)

    assert "stamp_error" in out
    assert calls["n"] == ref_cas_retry.REF_CAS_ATTEMPTS
    assert (root / PLAN).read_bytes() == before


@pytest.mark.parametrize("which", ["BlobDriftRefused", "NothingToCommit"])
def test_drift_and_nothing_to_commit_are_not_retried(tmp_path, monkeypatch, which):
    from coordinator_core.git import commit

    root, sha = _repo(tmp_path)
    cls = getattr(commit, which)
    wrapper, calls = _cas_loser(
        root, commit.commit_paths, 99, lambda: cls("compare-and-swap failed -- moved")
    )
    monkeypatch.setattr(commit, "commit_paths", wrapper)

    out = stamp_checkpoint(root, _message(), sha)

    assert "stamp_error" in out and calls["n"] == 1


def test_gitignored_plan_is_stamped_on_disk_only(tmp_path):
    root, sha = _repo(tmp_path, ignore_plan=True)

    out = stamp_checkpoint(root, _message(), sha)

    assert out["stamp_sha"] is None and out["stamp_skipped"] == "plan_untracked"
    assert _rows(root)["C1"]["disposition_ref"] == sha
    assert _git(root, "rev-parse", "HEAD") == sha


def test_dirty_tracked_plan_is_stamped_on_disk_only(tmp_path):
    root, sha = _repo(tmp_path)
    with (root / PLAN).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\npeer edit\n")

    out = stamp_checkpoint(root, _message(), sha)

    assert out["stamp_sha"] is None and out["stamp_skipped"] == "plan_dirty"
    assert _rows(root)["C1"]["disposition"] == "coded"
    assert "peer edit" in (root / PLAN).read_text(encoding="utf-8")
    assert _git(root, "rev-parse", "HEAD") == sha


def test_crlf_plan_is_stamped_and_stays_crlf(tmp_path):
    root, sha = _repo(tmp_path, newline="\r\n")

    out = stamp_checkpoint(root, _message(), sha)

    assert out["rows_coded"] == {PLAN: ["C1", "C2"]} and out["stamp_sha"]
    raw = (root / PLAN).read_bytes()
    assert b"\r\r" not in raw and raw.count(b"\n") == raw.count(b"\r\n")
    assert _rows(root)["C1"]["disposition_ref"] == sha


def test_non_checkpoint_message_stamps_nothing(tmp_path):
    root, sha = _repo(tmp_path)
    before = (root / PLAN).read_bytes()
    assert stamp_checkpoint(root, "plain commit", sha) == {}
    assert (root / PLAN).read_bytes() == before


def test_parse_checkpoint_reads_stop_rule_line():
    sub = "checkpoint(wave 0): 2 rows — L6, L7"
    assert parse_checkpoint(_message(subject=sub, stopped="L7")).stop_rule == {"L7"}
    assert parse_checkpoint(_message(subject=sub)).stop_rule == frozenset()
    both = parse_checkpoint(_message(subject=sub, owed="L6", stopped="L7"))
    assert both.gate_owed == {"L6"} and both.stop_rule == {"L7"}


def test_stop_rule_row_stays_open_and_sibling_is_coded(tmp_path):
    text = _plan_text((("L6", "open"), ("L7", "open"), ("C3", "open")))
    root, sha = _repo(tmp_path, text)

    out = stamp_checkpoint(
        root, _message(subject="checkpoint(wave 0): 2 rows — L6, L7", stopped="L7"), sha
    )

    assert out["rows_coded"] == {PLAN: ["L6"]}
    rows = _rows(root)
    assert rows["L6"]["disposition"] == "coded" and rows["L6"]["disposition_ref"] == sha
    assert rows["L7"]["disposition"] == "open" and "disposition_ref" not in rows["L7"]


def test_checkpoint_rows_landed_omits_stop_rule_row_until_a_bare_checkpoint(tmp_path):
    root, _ = _repo(tmp_path)
    sub = "checkpoint(wave 0): 2 rows — L6, L7"
    (root / "seed.txt").write_text("a\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", _message(subject=sub, stopped="L7"))
    first = _git(root, "rev-parse", "HEAD")
    wanted = frozenset({"L6", "L7"})

    walk = checkpoint_stamp.checkpoint_rows_landed(root, PLAN, first, wanted)
    assert walk.landed == {first: frozenset({"L6"})}

    (root / "seed.txt").write_text("b\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", _message(subject="checkpoint(wave 1): 1 rows — L7"))
    second = _git(root, "rev-parse", "HEAD")
    walk = checkpoint_stamp.checkpoint_rows_landed(root, PLAN, second, wanted)
    assert walk.landed == {second: frozenset({"L7"}), first: frozenset({"L6"})}


def test_module_top_imports_stdlib_only():
    src = Path(checkpoint_stamp.__file__).read_text(encoding="utf-8")
    top = src.split("\n\n\n", 1)[0]
    assert "coordinator_core" not in top
