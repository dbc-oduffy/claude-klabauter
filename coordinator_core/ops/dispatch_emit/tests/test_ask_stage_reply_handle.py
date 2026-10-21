"""dispatch.ask_stage replies a verified sidecar handle, so a large plan's manifest never rides an agent's text."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.grind_commit_contract import RELAY_CORRUPT_HEAD
from coordinator_core.ops.dispatch_emit.ask_stage import _handler, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN_REL

ROWS = 40
RUN = "big"

_ROW = """- id: C{n}
  title: row {n}
  change_kind: code-edit
  surface: pkg/f{n}.py
  writes:
    - pkg/f{n}.py
"""


@pytest.fixture
def big_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    body = "".join(_ROW.format(n=n) for n in range(1, ROWS + 1))
    plan = f"---\ndeliverable_id: dlv-big-abc123\n---\n# Big plan\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return tmp_path


def test_forty_row_reply_is_a_short_handle_and_the_manifest_reads_back_intact(big_repo):
    reply = _handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=big_repo)
    # Interim: the manifest rides inline beside the handle until the script reads the handle (plan C4/C5).
    assert len(str(reply["manifest_handle"]).encode("ascii")) < 300
    handle = reply["manifest_handle"]
    assert handle["path"] == f"{RECORD_DIR_ROOT}/{RUN}/manifest.json"

    manifest = reply_sidecar.read_reply(big_repo, handle)
    assert len(manifest["rows"]) == ROWS
    staged = stage(big_repo, run_id=RUN, plan_rel=PLAN_REL).to_json()
    for key in ("review_declared_paths", "marker_path", "test_targets"):
        assert manifest[key] == staged[key]
    assert manifest["review_declared_paths"] and manifest["marker_path"]
    assert "head_sha" in manifest


def test_a_tampered_manifest_is_refused(big_repo):
    handle = _handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=big_repo)["manifest_handle"]
    target = big_repo / handle["path"]
    raw = bytearray(target.read_bytes())
    raw[raw.index(b"C7")] ^= 0x01
    target.write_bytes(bytes(raw))
    with pytest.raises(reply_sidecar.ReplySidecarError) as exc:
        reply_sidecar.read_reply(big_repo, handle)
    assert str(exc.value).startswith(RELAY_CORRUPT_HEAD)


def test_a_refusal_reply_stays_an_error_object(big_repo):
    reply = _handler({"run_id": RUN, "plan_path": "docs/plans/missing.md"}, repo_root=big_repo)
    assert "manifest_handle" not in reply and "error" in reply
