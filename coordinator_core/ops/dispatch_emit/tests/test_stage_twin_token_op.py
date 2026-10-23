"""Tests for dispatch.stage_twin_token: the run token reaches the staged twin and nowhere else."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import nested_run_contract as nrc
from coordinator_core.ops.dispatch_emit import op as emit_op
from coordinator_core.ops.dispatch_emit import reply_sidecar as rs
from coordinator_core.ops.dispatch_emit import run_token

RUN = "run1"
SCRIPT = f"export const meta = {{\n  name: 'x',\n}}\n{nrc.STAGED_INLINE_SLOT}\nreturn 1;\n"


@pytest.fixture
def staged(tmp_path):
    (tmp_path / ".git").mkdir()
    script = tmp_path / "scratch" / "warp" / f"{RUN}.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT, encoding="utf-8")
    token = run_token.mint(tmp_path, RUN, f"scratch/warp/{RUN}.workflow.mjs")
    handle = rs.write_reply(tmp_path, tmp_path / "state" / "warp" / RUN / "manifest.json", {"rows": [{"id": "C1"}]})
    return tmp_path, token, handle, script


def _call(root, token, handle, run_id=RUN):
    return emit_op._stage_twin_token({"run_id": run_id, "handle": handle, "run_token": token}, root)


def _twin(root, handle):
    return root / nrc.staged_twin_rel(RUN, handle["sha256"])


def _tree_bytes(root):
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts)


def test_rewrites_twin_with_run_token_and_leaks_nowhere_else(staged):
    root, token, handle, script = staged
    before = _twin(root, handle).read_bytes()
    reply = _call(root, token, handle)
    assert reply == {"twin": nrc.staged_twin_rel(RUN, handle["sha256"])}
    twin = _twin(root, handle).read_text(encoding="utf-8")
    assert json.dumps(token) in twin and json.dumps(token).encode() not in before
    assert token not in json.dumps(reply)
    assert token not in script.read_text(encoding="utf-8")
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts and p != _twin(root, handle):
            assert token.encode() not in p.read_bytes(), p


def test_rerun_is_idempotent(staged):
    root, token, handle, _ = staged
    assert _call(root, token, handle) == _call(root, token, handle)


def test_unadmitted_token_is_refused_and_writes_nothing(staged):
    root, _, handle, _ = staged
    before = _tree_bytes(root)
    bad = "0" * 32
    reply = _call(root, bad, handle)
    assert "error" in reply and bad not in reply["error"]
    assert _tree_bytes(root) == before


def test_malformed_token_is_refused(staged):
    root, _, handle, _ = staged
    assert "error" in _call(root, "not-a-token", handle)


def test_handle_sha_mismatch_is_refused(staged):
    root, token, handle, _ = staged
    before = _tree_bytes(root)
    reply = _call(root, token, {**handle, "sha256": "f" * 64})
    assert "error" in reply and token not in reply["error"]
    assert _tree_bytes(root) == before


def test_slotless_script_is_refused_by_name(staged):
    root, token, handle, script = staged
    script.write_text("return 1;\n", encoding="utf-8")
    reply = _call(root, token, handle)
    assert "error" in reply and RUN in reply["error"] and "slot" in reply["error"]
    assert token not in reply["error"]


def test_handle_of_another_run_is_refused_and_leaks_no_token(staged):
    root, token, _, _ = staged
    other = rs.write_reply(root, root / "state" / "warp" / "run2" / "manifest.json", {"rows": [{"id": "C2"}]})
    reply = _call(root, token, other)
    assert "error" in reply and token not in reply["error"]
    assert not list(root.glob("scratch/warp/run2.staged-*")) or all(
        token not in p.read_text(encoding="utf-8") for p in root.glob("scratch/warp/run2.staged-*")
    )


def test_missing_param_is_refused(staged):
    root, token, handle, _ = staged
    assert "error" in emit_op._stage_twin_token({"run_id": RUN, "handle": handle}, root)


def test_arm_agnostic_script_without_xs_marker_is_tokened(staged):
    root, token, handle, script = staged
    assert "XS" not in script.read_text(encoding="utf-8")
    assert "twin" in _call(root, token, handle)


def test_write_reply_and_handle_for_leave_a_tokenless_twin(staged):
    root, token, handle, _ = staged
    rs.handle_for(root, root / handle["path"])
    assert token not in _twin(root, handle).read_text(encoding="utf-8")
    assert "runToken" not in _twin(root, handle).read_text(encoding="utf-8")


def test_process_time_with_a_115kb_manifest(staged):
    root, token, _, _ = staged
    big = {"rows": [{"id": f"C{i}", "t": "x" * 1000} for i in range(110)]}
    handle = rs.write_reply(root, root / "state" / "warp" / RUN / "manifest.json", big)
    start = time.process_time()
    reply = _call(root, token, handle)
    elapsed = time.process_time() - start
    assert "twin" in reply
    assert elapsed < 0.2
