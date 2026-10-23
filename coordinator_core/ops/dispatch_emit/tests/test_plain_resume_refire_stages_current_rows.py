"""A plain-resume refire stages the current ledger's resume rows, never the prior attempt's."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, reply_sidecar, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo

PLAN_REL = "docs/plans/2026-10-23-refire-fixture.md"
RUN = "ask-20261023T000000-aaaaaa"


@pytest.fixture
def repo(_bare_repo, monkeypatch):
    (_bare_repo / ".git").mkdir()
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    return _bare_repo


def _plan(repo, dispositions):
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n  change_kind: code-edit\n"
        f"  surface: pkg/{i}.py\n  writes:\n    - pkg/{i}.py\n"
        for i, d in dispositions.items()
    )
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(
        f"---\nsizing_object: {REL}\n---\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n",
        encoding="utf-8",
        newline="\n",
    )


def _fire(repo):
    reply = _handler({"sizing_path": REL, "run_id": RUN, "writes": ["pkg/C1.py"]}, repo)
    assert reply["halt"] is None, reply
    handle = reply.get("manifest_handle")
    assert handle, reply
    manifest = reply_sidecar.read_reply(repo, handle)
    return reply, sorted(r["id"] for r in manifest["rows"])


def _start(repo):
    _put(repo, tshirt="S", route="spec-dispatch", name="refire-fixture", plan=PLAN_REL)


def test_refire_after_a_row_lands_stages_only_the_rows_still_owed(repo):
    _start(repo)
    _plan(repo, {"C1": "open", "C2": "open", "C3": "open"})
    first, rows = _fire(repo)
    assert first["resume_rows"] == ["C1", "C2", "C3"] and rows == ["C1", "C2", "C3"]

    _plan(repo, {"C1": "coded", "C2": "open", "C3": "open"})
    second, rows = _fire(repo)
    assert second["resume_rows"] == ["C2", "C3"]
    assert rows == ["C2", "C3"]


def _script_stage(repo, only_rows):
    from coordinator_core.ops.dispatch_emit import ask_stage

    reply = ask_stage._handler(
        {"run_id": RUN, "plan_path": PLAN_REL, "only_rows": only_rows, "commit_sizing_path": REL}, repo
    )
    assert "manifest_handle" in reply, reply
    return sorted(r["id"] for r in reply_sidecar.read_reply(repo, reply["manifest_handle"])["rows"])


def test_script_stage_after_a_refire_gate_stages_current_rows(repo):
    _start(repo)
    _plan(repo, {"C1": "open", "C2": "open", "C3": "open"})
    first, _ = _fire(repo)
    assert _script_stage(repo, first["resume_rows"]) == ["C1", "C2", "C3"]

    _plan(repo, {"C1": "coded", "C2": "open", "C3": "open"})
    second, _ = _fire(repo)
    assert _script_stage(repo, second["resume_rows"]) == ["C2", "C3"]


def test_stale_prior_attempt_only_rows_never_stage_a_landed_row(repo):
    _start(repo)
    _plan(repo, {"C1": "open", "C2": "open", "C3": "open"})
    first, _ = _fire(repo)
    _plan(repo, {"C1": "coded", "C2": "open", "C3": "open"})
    second, _ = _fire(repo)
    assert _script_stage(repo, first["resume_rows"]) == ["C2", "C3"]
    assert _script_stage(repo, second["resume_rows"]) == ["C2", "C3"]


def test_refused_refire_gate_leaves_no_prior_attempt_snapshot(repo):
    _start(repo)
    _plan(repo, {"C1": "coded", "C2": "open", "C3": "open"})
    _fire(repo)
    assert resume_snapshot.read(repo, RUN, PLAN_REL) is not None

    _plan(repo, {"C1": "open", "C2": "coded", "C3": "open"})
    refused = _handler({"sizing_path": REL, "run_id": RUN, "require_live_token": True}, repo)
    assert refused["halt"]["kind"] == "refusal"
    assert resume_snapshot.read(repo, RUN, PLAN_REL) is None
    assert _script_stage(repo, ["C1", "C3"]) == ["C1", "C3"]


def test_live_refire_still_writes_its_own_snapshot(repo):
    _start(repo)
    _plan(repo, {"C1": "coded", "C2": "open"})
    _fire(repo)
    assert resume_snapshot.read(repo, RUN, PLAN_REL) == resume_snapshot.Snapshot(frozenset({"C1"}), frozenset({"C2"}))


def test_refire_snapshot_names_the_current_ledger(repo):
    _start(repo)
    _plan(repo, {"C1": "open", "C2": "open"})
    _fire(repo)
    _plan(repo, {"C1": "coded", "C2": "open"})
    _fire(repo)
    snap = resume_snapshot.read(repo, RUN, PLAN_REL)
    assert snap == resume_snapshot.Snapshot(frozenset({"C1"}), frozenset({"C2"}))
    assert json.loads((repo / "scratch" / "warp" / RUN / "resume-snapshot.json").read_text())["resume_rows"] == ["C2"]
