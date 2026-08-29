"""The commit-route execution ledger: one row per `run_commit_pipeline` entry.

Purpose: pin the observation the nine-instance false-failure row
(`state/bug-backlog/2026-08-27-the-sanctioned-commit-route-reports-success-as-
failure-six-ways.yaml`) could not make. Its instance 9 closed the cause to a
SECOND EXECUTION on a token mismatch, and then could go no further, because
nothing on disk recorded that a commit route had executed at all -- the
pipeline's only rows were its two push-leg spans, which had produced zero rows
across the whole live ledger.

Negative-spec: does NOT assert a rate, a cause, or which caller re-executes.
It asserts the row exists, carries the pid/ppid pair that discriminates
"one process called twice" from "the command ran twice", and does not enter
the `kind: "composition"` population DR-325's fleet elapsed budget is armed
from.
"""

import json

import pytest

from coordinator_core.telemetry import op_latency


def _rows(sink):
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def sink(tmp_path, monkeypatch):
    """Pin the sink to a fake common dir, as `test_op_latency.py` already does --
    `tmp_path` is not a git repo, so the real upward walk would land the row in
    whatever repo the test runner happens to sit in."""
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    return fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def test_entry_row_carries_the_pid_ppid_discriminator(tmp_path, monkeypatch, sink):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    op_latency.record_commit_pipeline_entry(
        invocation_id="abc123",
        t_start=1000.0,
        repo_root=tmp_path,
        sid="sid-1",
    )
    rows = [r for r in _rows(sink) if r.get("kind") == "commit_pipeline_entry"]
    assert len(rows) == 1
    row = rows[0]
    assert row["invocation_id"] == "abc123"
    assert row["t_start"] == 1000.0
    assert row["sid"] == "sid-1"
    assert isinstance(row["pid"], int)
    assert isinstance(row["ppid"], int)


def test_two_entries_are_two_rows_joinable_by_pid(tmp_path, monkeypatch, sink):
    """The double execution is visible AS two rows -- the whole point."""
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    for invocation_id in ("first", "second"):
        op_latency.record_commit_pipeline_entry(
            invocation_id=invocation_id, t_start=1000.0, repo_root=tmp_path, sid=None
        )
    rows = [r for r in _rows(sink) if r.get("kind") == "commit_pipeline_entry"]
    assert [r["invocation_id"] for r in rows] == ["first", "second"]
    assert rows[0]["pid"] == rows[1]["pid"]


def test_row_stays_out_of_the_composition_population(tmp_path, monkeypatch, sink):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    op_latency.record_commit_pipeline_entry(
        invocation_id="abc123", t_start=1000.0, repo_root=tmp_path, sid=None
    )
    kinds = {r.get("kind") for r in _rows(sink)}
    assert "composition" not in kinds
    assert "started" not in kinds
    assert "complete" not in kinds


def test_disable_env_suppresses_the_row(tmp_path, monkeypatch, sink):
    monkeypatch.setenv("COORDINATOR_OP_LATENCY_DISABLE", "1")
    op_latency.record_commit_pipeline_entry(
        invocation_id="abc123", t_start=1000.0, repo_root=tmp_path, sid=None
    )
    assert not sink.exists() or not [
        r for r in _rows(sink) if r.get("kind") == "commit_pipeline_entry"
    ]


def test_pipeline_entry_is_recorded_before_any_work(monkeypatch):
    """The call sits at the top of `run_commit_pipeline`, ahead of the stale-lock
    reap -- an execution that dies mid-pipeline still leaves its row."""
    from coordinator_core.ops.ceremony import commit_pipeline

    calls = []
    monkeypatch.setattr(
        commit_pipeline,
        "record_commit_pipeline_entry",
        lambda **kw: calls.append(kw),
    )

    def _boom(_root):
        raise RuntimeError("stop here")

    monkeypatch.setattr(commit_pipeline, "_preflight_reap_stale_lock", _boom)
    try:
        commit_pipeline.run_commit_pipeline(".", session_id="s", subject="x")
    except RuntimeError:
        pass
    assert len(calls) == 1
    assert calls[0]["invocation_id"]


def test_landed_commit_names_the_entry_row_that_made_it(monkeypatch):
    """The join the row was built for, made executable.

    The entry row's `invocation_id` is `run_commit_pipeline`'s own
    `_composition_id`; on the dispatched-committer route it joined to nothing
    (the push spans carrying that id have never fired there, and the
    `Commit-Token:` trailer was an unrelated `uuid4`). Passing it as the token
    makes a landed commit name its entry, so two entries against one commit
    resolve without inference: the trailer names the committer, the other
    entry names the re-entry.
    """
    from coordinator_core.ops.ceremony import commit_pipeline

    entries = []
    monkeypatch.setattr(
        commit_pipeline,
        "record_commit_pipeline_entry",
        lambda **kw: entries.append(kw),
    )
    tokens = []

    def _capture(*_a, **kw):
        tokens.append(kw.get("token"))
        raise RuntimeError("stop after the token is chosen")

    monkeypatch.setattr(commit_pipeline, "commit", _capture)
    try:
        commit_pipeline.run_commit_pipeline(".", session_id="s", subject="x")
    except RuntimeError:
        pass
    import inspect

    assert "token=_composition_id" in inspect.getsource(
        commit_pipeline.run_commit_pipeline
    ), "the call site stopped passing the entry row's id as the commit token"
    if tokens:
        assert tokens[0] == entries[0]["invocation_id"]


def test_commit_mints_its_own_token_when_none_is_passed():
    """`token=None` (every non-pipeline caller, every test) is unchanged."""
    import inspect

    from coordinator_core.ops.ceremony import commit_pipeline

    sig = inspect.signature(commit_pipeline.commit)
    assert sig.parameters["token"].default is None
    src = inspect.getsource(commit_pipeline.commit)
    assert "token if token is not None else uuid.uuid4().hex" in src
