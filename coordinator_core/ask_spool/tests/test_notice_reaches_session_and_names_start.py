"""A drain-minted run's notice reaches its session and names the call that starts it.

`hooks.postuse_advisory_dispatch` is keyed scope "none", so dispatch hands it repo_root=None; the
notice leg consumed nothing in any repo, and a drain-minted resume run sat unstarted until the
stale-minted reaper halted it `abandoned_at_minted` (example-game-repo 61c86a, 2026-11-11).
"""

from __future__ import annotations

import asyncio
import json
import os

from coordinator_core.ask_spool import notice
from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, RUN_STAGE_FILE, record_dir

_SID = "sess-notice-probe"
_RUN = "warp-2026-11-11-probe-20261111T043953-abc123"


def _write_notice(root, **fields):
    os.makedirs(notice.notices_dir(str(root)), exist_ok=True)
    with open(notice.notice_path(str(root), _SID), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"spool_id": "sp1", "run_id": _RUN, **fields}) + "\n")


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    return root


def _stage(root, stage):
    d = record_dir(root, _RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_STAGE_FILE).write_text(json.dumps({"stage": stage}), encoding="utf-8")


def _script(root):
    script = root / RUN_DIR_ROOT / (_RUN + ".workflow.mjs")
    script.parent.mkdir(parents=True)
    script.write_text("// probe\n", encoding="utf-8")
    return script


def _advise(cwd):
    out = pad._handler({"session_id": _SID, "tool_name": "Bash", "cwd": str(cwd)}, repo_root=None)
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")


def test_scope_none_dispatch_still_consumes_the_session_notice_from_cwd(tmp_path):
    root = _repo(tmp_path)
    _write_notice(root, status="minted")

    text = _advise(root / "sub")

    assert f"minted run {_RUN}" in text
    assert not os.path.exists(notice.notice_path(str(root), _SID))


def test_unstarted_mint_names_its_workflow_script(tmp_path):
    root = _repo(tmp_path)
    script = _script(root)
    _stage(root, "minted")
    _write_notice(root, status="minted", unstarted=True)

    text = _advise(root)

    assert "not started. Start it: Workflow({scriptPath: " in text
    assert script.as_posix() in text.replace("\\\\", "/")


def test_a_found_run_gets_no_start_pointer(tmp_path):
    root = _repo(tmp_path)
    _script(root)
    _stage(root, "minted")
    _write_notice(root, status="minted")

    text = _advise(root)

    assert f"minted run {_RUN}." in text
    assert "Start it" not in text


def test_a_backlog_notice_for_a_since_halted_run_offers_nothing_to_start(tmp_path):
    root = _repo(tmp_path)
    _script(root)
    _stage(root, "halted")
    _write_notice(root, status="minted", unstarted=True)

    text = _advise(root)

    assert "it is now halted, nothing to start" in text
    assert "Start it" not in text
