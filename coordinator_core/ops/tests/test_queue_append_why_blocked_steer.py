"""queue.append replies with an engine-ask steer for an unnamed deferral; the row is always written."""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops import queue_append
from coordinator_core.telemetry import op_latency


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(queue_append._QUEUE_APPEND_OUTPUT_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)


_REQUIRED = {
    "improvement-queue": {"surface": "s", "proposed_action": "p", "change_kind": "code-edit"},
    "debt-backlog": {"source": "s", "risk": "r", "proposed_action": "p"},
    "bug-backlog": {"surface": "s", "severity": "P2"},
    "lessons": {"scope": "project"},
}


def _call(schema, **extra):
    params = {"schema": schema, "title": "t item", "body": "b", "status": "open", **_REQUIRED[schema], **extra}
    return queue_append._queue_append_handler(params, None)


@pytest.mark.parametrize("schema", ["improvement-queue", "debt-backlog", "bug-backlog"])
@pytest.mark.parametrize("why", [None, "not now", "plan-sized"])
def test_unnamed_reason_steers_and_still_writes(schema, why):
    kw = {} if why is None else {"why_blocked": why}
    result = _call(schema, **kw)
    assert os.path.isfile(result["out_path"])
    assert 'Workflow({name: "coordinator:engine-ask"' in result["steer"]


@pytest.mark.parametrize(
    "why",
    [
        "tradeoff: speed vs. safety",
        "other-repo: DoE owns it",
        "irreversible publish",
        "plan-sized: docs/plans/2026-10-30-x.md",
    ],
)
def test_named_reason_returns_no_steer(why):
    result = _call("improvement-queue", why_blocked=why)
    assert os.path.isfile(result["out_path"])
    assert "steer" not in result


def test_other_schema_never_steers():
    result = _call("lessons")
    assert "steer" not in result


def test_worker_caller_is_not_steered():
    result = _call("improvement-queue", created_by_agent="coordinator:executor")
    assert "steer" not in result
