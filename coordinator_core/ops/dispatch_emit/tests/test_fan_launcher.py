"""write_launcher: the lazy-emit launcher script and its emission receipt (textual, no JS runtime)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import fan_launcher as fl
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path
from coordinator_core.ops.dispatch_emit.nested_run_contract import (
    STAGE_PENDING_KEY,
    staged_twin_rel,
)
from coordinator_core.ops.dispatch_emit.reply_sidecar import READ_STAGE_SIDECAR_JS, VERIFY_REPLY_JS


def _children(*ids: str) -> list:
    return [
        fl.LaunchChild(
            run_id=i,
            sizing_rel=f"state/sizings/{i}.yaml",
            emit_params={"ask": True, "sizing_path": f"state/sizings/{i}.yaml", "reserved_run_id": i},
        )
        for i in ids
    ]


def _write(tmp_path: Path, parent: str = "ask-parent", ids=("ask-r1", "ask-r2")) -> tuple:
    path = fl.write_launcher(tmp_path, parent, _children(*ids), "sess-1")
    return path, path.read_text(encoding="utf-8")


def test_meta_is_the_first_statement(tmp_path):
    _, script = _write(tmp_path)
    assert script.startswith("export const meta = {")


def test_every_run_id_and_emit_params_appear_exactly_once(tmp_path):
    _, script = _write(tmp_path)
    for run_id in ("ask-r1", "ask-r2"):
        assert script.count(f'{{"run_id": {json.dumps(run_id)}, ') == 1
        params = json.dumps(
            {
                "ask": True,
                "sizing_path": f"state/sizings/{run_id}.yaml",
                "reserved_run_id": run_id,
                "launcher_child": True,
            }
        )
        assert script.count(params) == 1


def test_rendered_emit_params_force_launcher_child_true(tmp_path):
    kids = _children("ask-r1", "ask-r2")
    kids[1].emit_params["launcher_child"] = False
    script = fl._render(tmp_path, "ask-parent", kids)
    assert script.count('"launcher_child"') == 2
    assert script.count('"launcher_child": true') == 2
    assert '"launcher_child": false' not in script


def test_zero_children_refused(tmp_path):
    with pytest.raises(ValueError, match="no children"):
        fl.write_launcher(tmp_path, "ask-parent", [], None)
    assert not (tmp_path / fl.launcher_rel("ask-parent")).exists()


def test_duplicate_run_ids_refused(tmp_path):
    with pytest.raises(ValueError, match="duplicate run_id"):
        fl.write_launcher(tmp_path, "ask-parent", _children("ask-r1", "ask-r1"), None)


@pytest.mark.parametrize("parent", ["", "../escape", "a/b", "a\\b"])
def test_unsafe_parent_id_refused(tmp_path, parent):
    with pytest.raises(ValueError, match="parent_id"):
        fl.write_launcher(tmp_path, parent, _children("ask-r1"), None)


def test_repo_root_is_a_posix_literal(tmp_path):
    _, script = _write(tmp_path)
    assert f"const REPO_ROOT = {json.dumps(tmp_path.as_posix())};" in script
    root_line = next(ln for ln in script.splitlines() if ln.startswith("const REPO_ROOT"))
    assert "\\" not in root_line


def test_no_run_token_literal_in_the_script(tmp_path):
    _, script = _write(tmp_path)
    assert not re.search(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", script)


def test_every_start_passes_script_path_only(tmp_path):
    _, script = _write(tmp_path)
    assert script.count("workflow({ scriptPath: scriptPath })") == 1  # one start site, run for every child
    assert script.count("workflow(") == 1
    assert not re.search(r"\bargs\s*:", script)
    assert "launcherChild" not in script and "stagedManifest" not in script
    assert STAGE_PENDING_KEY in script


def test_children_run_under_parallel_with_isolated_failure(tmp_path):
    _, script = _write(tmp_path)
    assert "await parallel(CHILDREN.map(" in script
    assert "status: 'emit_refused'" in script  # a refused emit is that child's entry, never a throw


def test_emit_leg_contract(tmp_path):
    _, script = _write(tmp_path)
    assert "model: 'haiku'" in script
    assert "dispatch.emit" in script
    assert "'path', 'run_id', 'fire_args'" in script or "required: ['path', 'run_id', 'fire_args']" in script
    assert "e.run_id !== child.run_id" in script
    assert "WARP_PREFIX" in script and "scratch/warp/" in script


def _refusal(script: str, child_id: str, reply: dict):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    m = re.search(r"function _emitRefusal\(child, e\) \{.*?\n\}\n", script, re.S)
    assert m
    prog = (
        "const WARP_PREFIX='/r/scratch/warp/';"
        + m.group(0)
        + f"console.log(JSON.stringify(_emitRefusal({{run_id:{json.dumps(child_id)}}}, {json.dumps(reply)})));"
    )
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(out.stdout)


@pytest.mark.spawns_process
def test_failed_emit_surfaces_the_op_error_not_a_run_id_mismatch(tmp_path):
    _, script = _write(tmp_path)
    assert "error: { type: 'string' }" in script
    failed = {"path": "", "run_id": "", "fire_args": {}, "error": "SizingFireRefused: sizing-object not found on disk"}
    got = _refusal(script, "ask-r1", failed)
    assert got == "emit failed: SizingFireRefused: sizing-object not found on disk"
    assert "run_id" not in got


@pytest.mark.spawns_process
def test_genuine_run_id_mismatch_still_refused(tmp_path):
    _, script = _write(tmp_path)
    ok_shape = {"path": "/r/scratch/warp/x/s.js", "run_id": "ask-other", "fire_args": {}}
    assert _refusal(script, "ask-r1", ok_shape) == "emit reply run_id ask-other is not ask-r1"
    good = dict(ok_shape, run_id="ask-r1")
    assert _refusal(script, "ask-r1", good) is None


def test_launcher_no_longer_reads_the_sidecar(tmp_path):
    _, script = _write(tmp_path)
    assert READ_STAGE_SIDECAR_JS not in script and VERIFY_REPLY_JS not in script
    assert "_readStageSidecar" not in script and "_makeStageReader" not in script
    assert "legMemo" not in script
    assert "stage_pending repeated for a handle already supplied" in script


def _run_child_stage(script: str, sha, fire_args=None, repeat=False) -> list:
    """Run _runChild in node with stubs; returns [result, paths workflow() was started with]."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    body = script.split("\n", 1)[1].split("const _settled", 1)[0].replace("export const meta", "const meta")
    pending = {"stage_pending": {"handle": {"sha256": sha}}}
    root = re.search(r"const REPO_ROOT = (\".*?\");", body).group(1)
    code = (
        f"const PENDING = {json.dumps(pending)};\n"
        "const calls = [];\n"
        "const labels = [];\n"
        f"const agent = async (p, o) => {{ labels.push(o.label); "
        f"return {{ path: {root} + '/scratch/warp/x/s.mjs', run_id: 'ask-r1', fire_args: {json.dumps(fire_args or {})} }}; }};\n"
        f"const workflow = async (o) => {{ calls.push(o); return {'true' if repeat else 'calls.length === 1'} ? PENDING : {{ done: true }}; }};\n"
        "const parallel = async () => [];\n"
        + body
        + "\n_runChild(CHILDREN[0]).then((r) => console.log(JSON.stringify([r, calls, labels])));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
def test_stage_pending_with_a_good_handle_starts_the_twin_path(tmp_path):
    _, script = _write(tmp_path, ids=("ask-r1",))
    sha = "ab" * 32
    result, calls, _ = _run_child_stage(script, sha)
    assert result["status"] == "ok"
    root = tmp_path.as_posix()
    assert calls[1] == {"scriptPath": f"{root}/{staged_twin_rel('ask-r1', sha)}"}
    assert all(set(c) == {"scriptPath"} for c in calls)


@pytest.mark.spawns_process
@pytest.mark.parametrize("sha", ["AB" * 32, "ab" * 31, None, "../" + "a" * 61])
def test_malformed_sha_refuses_by_name(tmp_path, sha):
    _, script = _write(tmp_path, ids=("ask-r1",))
    result, calls, _ = _run_child_stage(script, sha)
    assert result["status"] == "failed"
    assert result["error"] == "stage_pending handle sha256 is not 64 lowercase hex"
    assert len(calls) == 1


def test_placeholder_shaped_emit_params_stay_literal(tmp_path):
    child = fl.LaunchChild("ask-r1", "s.yaml", {"note": "__REPO_ROOT__"})
    path = fl.write_launcher(tmp_path, "ask-parent", [child], None)
    expected = json.dumps({"note": "__REPO_ROOT__", "launcher_child": True})
    assert expected in path.read_text(encoding="utf-8")


def test_receipt_sha_matches_the_file(tmp_path):
    path, _ = _write(tmp_path)
    receipt = json.loads(emission_receipt_path(path).read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt["session_id"] == "sess-1"
    assert path == tmp_path / fl.launcher_rel("ask-parent")


def test_second_write_for_the_same_parent_is_refused(tmp_path):
    path, script = _write(tmp_path)
    with pytest.raises(FileExistsError):
        fl.write_launcher(tmp_path, "ask-parent", _children("ask-r3"), "sess-1")
    assert path.read_text(encoding="utf-8") == script


_TOKEN = "cd" * 16


@pytest.mark.spawns_process
def test_a_runtoken_child_starts_the_twin_with_only_the_emit_agent(tmp_path):
    _, script = _write(tmp_path, ids=("ask-r1",))
    sha = "ab" * 32
    result, calls, labels = _run_child_stage(script, sha, {"runToken": _TOKEN})
    assert result["status"] == "ok"
    assert labels == ["emit-ask-r1"]
    assert calls[1] == {"scriptPath": f"{tmp_path.as_posix()}/{staged_twin_rel('ask-r1', sha)}"}


def test_no_twin_token_leg_remains_in_the_script(tmp_path):
    _, script = _write(tmp_path)
    assert "stage_twin_token" not in script and "_twinTokenLeg" not in script and "twin-token-" not in script


@pytest.mark.spawns_process
def test_a_repeated_handle_still_fails_by_name(tmp_path):
    _, script = _write(tmp_path, ids=("ask-r1",))
    result, calls, _ = _run_child_stage(script, "ab" * 32, repeat=True)
    assert result["status"] == "failed"
    assert result["error"] == "stage_pending repeated for a handle already supplied"
    assert len(calls) == 2
