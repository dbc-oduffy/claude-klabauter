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
    LAUNCHER_CHILD_ARG,
    STAGE_PENDING_KEY,
    STAGED_MANIFEST_ARG,
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
        params = json.dumps({"ask": True, "sizing_path": f"state/sizings/{run_id}.yaml", "reserved_run_id": run_id})
        assert script.count(params) == 1


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


def test_launcher_child_is_set_on_every_start(tmp_path):
    _, script = _write(tmp_path)
    assert script.count("workflow({ scriptPath: scriptPath") == 1  # one start site, run for every child
    assert f"{{ {LAUNCHER_CHILD_ARG}: true }}" in script
    assert STAGE_PENDING_KEY in script and STAGED_MANIFEST_ARG in script


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


def test_stage_hand_back_reuses_the_reader_blocks_verbatim(tmp_path):
    _, script = _write(tmp_path)
    assert VERIFY_REPLY_JS in script
    assert READ_STAGE_SIDECAR_JS in script
    assert "stage_pending repeated for a handle already supplied" in script


def test_placeholder_shaped_emit_params_stay_literal(tmp_path):
    child = fl.LaunchChild("ask-r1", "s.yaml", {"note": "__REPO_ROOT__"})
    path = fl.write_launcher(tmp_path, "ask-parent", [child], None)
    assert json.dumps({"note": "__REPO_ROOT__"}) in path.read_text(encoding="utf-8")


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
