"""Phase 2: an allowlisted mutating op is served on the far box and leaves the caller's state/ untouched."""

import hashlib
import json
import subprocess
from pathlib import Path

from coordinator_core.remote_engine import fallback, policy

SSH = {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}
BOXES = [
    {"box_id": "caller", "root": "/r", "capability": {}, "transport": SSH},
    {"box_id": "serving", "root": "/r", "capability": {}, "transport": SSH},
]


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class Runner:
    def __init__(self, reply):
        self.calls = []
        self.reply = reply

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": self.reply}), "")


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(fallback.box_registry, "read_boxes", lambda *a, **k: BOXES)
    fallback.box_transport._reset_limits()
    monkeypatch.delenv(fallback.HOOK_ENV, raising=False)
    monkeypatch.setattr(fallback, "telemetry_path", lambda: tmp_path / "telemetry" / "t.jsonl")


def test_mutating_op_served_remotely_leaves_caller_state_untouched(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    state = tmp_path / "caller" / "state"
    (state / "runs").mkdir(parents=True)
    (state / "runs" / "r1.json").write_text('{"run": 1}')
    (state / "governor.json").write_text('{"view": "local"}')
    before = _snapshot(state)

    far_reply = {"run_id": "warp-x", "branch": "work/serving/2026-11-04"}
    runner = Runner(far_reply)
    out = fallback.try_remote(
        "dispatch.emit", {"run": "warp-x"}, runner=runner, boxes=BOXES, local_id="caller"
    )

    assert len(runner.calls) == 1
    assert out["served_by"] == "serving" and out["remote"] is True
    assert out["reply"] == far_reply
    placed = policy.placement("dispatch.emit", out["served_by"], out["reply"])
    assert placed == {
        "placement": "serving-box-daily-branch",
        "serving_box": "serving",
        "branch": "work/serving/2026-11-04",
    }
    assert _snapshot(state) == before


def test_every_allowlisted_op_routes_and_names_placement(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    for op in sorted(policy.PHASE2_MUTATING_ALLOWLIST):
        out = fallback.try_remote(
            op, {}, runner=Runner({"branch": "b"}), boxes=BOXES, local_id="caller"
        )
        assert out is not None and out["served_by"] == "serving"
        assert policy.placement(op, "serving", out["reply"])["branch"] == "b"


def test_mutating_op_not_on_allowlist_never_routes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    runner = Runner({})
    assert (
        fallback.try_remote("ceremony.commit_v2", {}, runner=runner, boxes=BOXES, local_id="caller")
        is None
    )
    assert runner.calls == []


def test_trim_never_routes_even_when_allowlisted(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(policy, "PHASE2_MUTATING_ALLOWLIST", frozenset({"trim-file-cache"}))
    runner = Runner({})
    assert (
        fallback.try_remote("trim-file-cache", {}, runner=runner, boxes=BOXES, local_id="caller")
        is None
    )
    assert runner.calls == []


def test_hook_context_never_routes_mutating(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv(fallback.HOOK_ENV, "0")
    runner = Runner({})
    assert (
        fallback.try_remote("hooks.submit_engine_ask", {}, runner=runner, boxes=BOXES, local_id="caller")
        is None
    )
    assert runner.calls == []


def test_placement_empty_for_read_only_op():
    assert policy.placement("ping", "serving", {"branch": "b"}) == {}
