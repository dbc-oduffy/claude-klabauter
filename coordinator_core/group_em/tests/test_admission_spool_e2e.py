"""Falsifier: a holderless START spools on the managed box, the managing G-EM drains it over the grant transport."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import (
    admission_drain,
    admission_post,
    admission_spool,
    box_registry,
    box_transport,
    nomination,
    remote_roster,
)
from coordinator_core.ops import group_em_admission_spool_read as spool_op

ENV = "COORDINATOR_SETTINGS_HOME"
GEM_SID, RUN_SID, RUN_KEY = "sid-gem", "sid-run", "grok-workflow-1"
NOW = 1_800_000_000.0
RELAY_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "unimatrix-relay.py"
OP_ROW = {"kind": "ssh", "host": "mini", "invoke": "/opt/klabauter/bin/coordinator-invoke"}
RELAY_ROW = {"kind": "ssh", "host": "mini", "relay_key": "/keys/unimatrix"}


@contextlib.contextmanager
def _at(home: Path):
    old = os.environ.get(ENV)
    os.environ[ENV] = str(home)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def _load_relay():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_under_test", RELAY_BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


@pytest.fixture(params=["op", "relay"])
def fleet(request, tmp_path, monkeypatch):
    """Two tmp homes: the managed box (no holder, no snapshot) and the G-EM box (live holder, ssh row for the managed box)."""
    managed, gem = tmp_path / "managed", tmp_path / "gem"
    managed_id, gem_id = box_registry.ensure_identity(managed), box_registry.ensure_identity(gem)
    managed_dir, gem_dir = managed / "state" / "group-em", gem / "state" / "group-em"
    box_registry.register(
        managed, {"os": "linux"}, directory=gem_dir, transport=dict(RELAY_ROW if request.param == "relay" else OP_ROW)
    )
    holder = gem_dir / "box" / "holder.json"
    holder.parent.mkdir(parents=True)
    holder.write_text(json.dumps({"session_id": GEM_SID, "repo_root": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(nomination, "is_live", lambda rec: nomination.LivenessResult(True, "live"))
    monkeypatch.setenv(ENV, str(managed))
    monkeypatch.setattr(box_transport.shutil, "which", lambda *a, **k: "/usr/bin/ssh")
    box_transport._reset_limits()
    calls: list = []
    relay = _load_relay()

    def serve_op(argv, timeout, stdin_bytes=None):
        _, op, text = shlex.split(argv[-1])
        assert op == "groupem.admission_spool_read"
        calls.append(argv)
        with _at(managed):
            reply = spool_op._groupem_admission_spool_read(json.loads(text))
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply) + "\n", "")

    def serve_relay(argv, timeout, stdin_text=""):
        mode, offset = relay.parse_mode(argv[-1])
        assert mode == "admission-spool"
        calls.append(argv)
        with _at(managed):
            reply = relay.admission_spool(offset)
        return subprocess.CompletedProcess(argv, 0 if reply.get("ok") is True else 1, json.dumps(reply) + "\n", "")

    monkeypatch.setattr(box_transport, "_run", serve_op)
    monkeypatch.setattr(box_transport, "_run_relay", serve_relay)
    return {
        "managed": managed, "gem": gem, "managed_id": managed_id, "gem_id": gem_id,
        "managed_dir": managed_dir, "gem_dir": gem_dir, "calls": calls, "mode": request.param,
    }


def _headless_start(artifact_dir: Path, post=admission_post.post) -> dict:
    """The headless_agents START step: post, then record_undelivered, then the run proceeds."""
    posted = post("START", RUN_SID, pids=[os.getpid()], run=RUN_KEY, declared_peak_mb=1024.0)
    admission_post.record_undelivered(artifact_dir, {"phase": "START", **posted})
    return posted


def _observe(fleet, artifact_dir: Path, post=admission_post.post) -> dict:
    proceeded = False
    with _at(fleet["managed"]):
        posted = _headless_start(artifact_dir, post)
        proceeded = True
    spooled = _lines(admission_spool.spool_path(fleet["managed_dir"]))
    with _at(fleet["gem"]):
        first = admission_drain.drain_managed(directory=fleet["gem_dir"], now=NOW, home=fleet["gem"])
        second = admission_drain.drain_managed(
            directory=fleet["gem_dir"], now=NOW + remote_roster.REFRESH_INTERVAL_S + 1, home=fleet["gem"]
        )
    drained = _lines(admission_post.admission_path(fleet["gem_dir"]))
    record = artifact_dir / "admission-undelivered.json"
    return {
        "posted": posted, "proceeded": proceeded, "spooled": spooled, "first": first, "second": second,
        "drained": drained, "record": json.loads(record.read_text(encoding="utf-8")) if record.exists() else None,
    }


def _assert_expected_when_true(fleet, obs: dict) -> None:
    mid = fleet["managed_id"]
    assert obs["proceeded"] is True
    posted = obs["posted"]
    assert posted.get("delivered") is False and posted.get("spooled") is True
    assert posted.get("degraded") is True and posted.get("reason") == "no_box_holder"
    assert [(x["phase"], x["session_id"], x["run"]) for x in obs["spooled"]] == [("START", RUN_SID, RUN_KEY)]
    assert not admission_post.admission_path(fleet["managed_dir"]).exists()
    assert obs["record"] is not None and obs["record"]["degraded"] is True and obs["record"]["spooled"] is True
    assert obs["first"][mid]["ok"] is True and obs["first"][mid]["appended"] == 1
    assert obs["second"][mid]["ok"] is True and obs["second"][mid]["appended"] == 0
    assert len(obs["drained"]) == 1
    line = obs["drained"][0]
    assert (line["phase"], line["session_id"], line["run"]) == ("START", RUN_SID, RUN_KEY)
    assert line["drained_from"] == mid and line["box_id"] == mid and line["holder_session"] is None


def test_holderless_start_spools_then_drains_once_into_the_gem_channel(fleet, tmp_path, capsys):
    (tmp_path / "artifacts").mkdir()
    obs = _observe(fleet, tmp_path / "artifacts")
    _assert_expected_when_true(fleet, obs)
    assert "START degraded (spooled), run proceeds: no_box_holder" in capsys.readouterr().err
    assert len(fleet["calls"]) == 2
    if fleet["mode"] == "relay":
        cursor = obs["first"][fleet["managed_id"]]["cursor"]
        assert [c[-1] for c in fleet["calls"]] == ["admission-spool 0", f"admission-spool {cursor}"]
    else:
        assert all("groupem.admission_spool_read" in c[-1] for c in fleet["calls"])


def test_the_pre_spool_reply_leaves_the_falsifier_red(fleet, tmp_path):
    """Baseline de21827ada57: post() returned only {posted: False, reason: no_box_holder} and wrote nothing."""
    (tmp_path / "artifacts").mkdir()
    obs = _observe(fleet, tmp_path / "artifacts", post=lambda *a, **k: {"posted": False, "reason": "no_box_holder"})
    assert obs["spooled"] == [] and obs["drained"] == []
    with pytest.raises(AssertionError):
        _assert_expected_when_true(fleet, obs)


def test_an_unreachable_managed_box_leaves_the_cursor_and_the_gem_channel_unchanged(fleet, tmp_path, monkeypatch):
    (tmp_path / "artifacts").mkdir()
    with _at(fleet["managed"]):
        _headless_start(tmp_path / "artifacts")
    monkeypatch.setattr(
        box_transport, "_run", lambda argv, timeout, stdin_bytes=None: subprocess.CompletedProcess(argv, 255, "", "down")
    )
    monkeypatch.setattr(
        box_transport, "_run_relay", lambda argv, timeout, stdin_text="": subprocess.CompletedProcess(argv, 255, "", "down")
    )
    with _at(fleet["gem"]):
        out = admission_drain.drain_managed(directory=fleet["gem_dir"], now=NOW, home=fleet["gem"])
    assert out[fleet["managed_id"]]["ok"] is False
    assert not admission_spool.cursor_path(fleet["managed_id"], fleet["gem_dir"]).exists()
    assert _lines(admission_post.admission_path(fleet["gem_dir"])) == []
