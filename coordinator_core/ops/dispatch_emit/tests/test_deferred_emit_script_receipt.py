"""write_deferred_script leaves `<script>.emitted.json` beside the script."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des


def test_deferred_script_has_receipt_beside_it(tmp_path):
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    receipt = script.with_name(script.name + ".emitted.json")
    assert receipt.is_file()
    assert json.loads(receipt.read_text(encoding="utf-8"))["sha256"]


def _run_stage_loop(pending: dict, first: str = "/r/scratch/warp/ask-r1.workflow.mjs") -> list:
    """Run the deferred script's `_startLoop` in node; returns [outcome, scriptPaths workflow() was started with]."""
    import re
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    values = des._stage_loop_values()
    loop = re.sub("|".join(values), lambda m: values[m.group(0)], des._STAGE_LOOP_JS)
    code = (
        "const REPO_ROOT = '/r';\n"
        "const calls = [];\n"
        f"const PENDING = {json.dumps({'stage_pending': pending})};\n"
        "const workflow = async (o) => { calls.push(o.scriptPath); return calls.length === 1 ? PENDING : { done: true }; };\n"
        + loop
        + f"_startLoop({json.dumps(first)}).then((r) => console.log(JSON.stringify([r, calls])));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_restaged_twin_named_by_the_reply_is_started_not_the_stale_handle_twin():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    stale, fresh = "3c" * 32, "2d" * 32
    out, calls = _run_stage_loop({"handle": {"sha256": stale}, "twin_path": staged_twin_rel("ask-r1", fresh)})
    assert out["ok"] is True
    assert calls[1] == "/r/" + staged_twin_rel("ask-r1", fresh)


def test_a_reply_that_names_no_twin_still_resumes_from_the_handle_sha():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    sha = "ab" * 32
    out, calls = _run_stage_loop({"handle": {"sha256": sha}})
    assert out["ok"] is True and calls[1] == "/r/" + staged_twin_rel("ask-r1", sha)


def test_a_named_twin_of_another_run_or_shape_is_refused_by_name():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    sha = "2d" * 32
    for bad in (staged_twin_rel("ask-other", sha), "scratch/warp/../x.staged-" + sha + ".workflow.mjs", 7):
        out, calls = _run_stage_loop({"handle": {"sha256": sha}, "twin_path": bad})
        assert out["ok"] is False and "twin_path is not a staged twin of this run" in out["error"]
        assert len(calls) == 1


def test_a_refire_twin_staged_under_an_earlier_stamp_is_started_and_another_warp_run_is_refused():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    sha = "2d" * 32
    first = "/r/scratch/warp/warp-plan-20261031T120809-5a6216.workflow.mjs"
    own = staged_twin_rel("warp-plan-20261030T010101-aaaaaa", sha)
    out, calls = _run_stage_loop({"handle": {"sha256": sha}, "twin_path": own}, first=first)
    assert out["ok"] is True and calls[1] == "/r/" + own
    other = staged_twin_rel("warp-other-20261030T010101-aaaaaa", sha)
    out, calls = _run_stage_loop({"handle": {"sha256": sha}, "twin_path": other}, first=first)
    assert out["ok"] is False and "twin_path is not a staged twin of this run" in out["error"] and len(calls) == 1


def test_receipt_failure_never_fails_the_emit(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import emission_receipt

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(emission_receipt, "_write_emission_receipt", boom)
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    assert script.is_file()
    assert not script.with_name(script.name + ".emitted.json").exists()
