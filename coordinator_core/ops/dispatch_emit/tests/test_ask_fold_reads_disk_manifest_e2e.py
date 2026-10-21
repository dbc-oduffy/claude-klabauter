"""The composed ask script adopts a stage reply only from sidecar pages that match its handle and are whole.

The fold segment of the composed script runs under node with the reply adopted through the real
`_adoptStageReply`; the page leg is answered from the engine op's page shape, and any other agent() the
segment reaches is a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import PAGE_FAKE_JS, pages_cfg, stage_reply
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_SHA = "a" * 40
_WHOLE = {"run_dir": "d", "plan_id": "p", "rows": [_ROW], "review_declared_paths": ["a.py"],
          "test_targets": ["t.py"], "marker_path": "m", "head_sha": _SHA}
#: What a trimming relay hands back: rows without brief_path, no review_declared_paths.
_TRIMMED = {"rows": [{"id": "C1", "wave": 0, "deps": []}]}
_PAGE = "stage-reply-page"


def _fold(reply, *, pages=None) -> dict:
    """Run the fold segment: adopt the stage `reply`, return its outcome and every agent label it reached."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    start = script.index("  let _stageHalt")
    end = script.index("\n", script.index("'stage staged no rows'"))
    program = (
        PAGE_FAKE_JS
        + "const cfg = JSON.parse(process.env.CFG);\n"
        "const _seen = [];\n"
        "const agent = async (p, opts) => { _seen.push(opts.label); "
        "if (opts.label === 'stage-reply-page') return fakePage(cfg, String(p)); "
        "throw new Error('unstubbed ' + opts.label); };\n"
        "const parallel = (xs) => Promise.all(xs.map((f) => f()));\n"
        "const _agentDefsClause = () => '';\n"
        "(async () => {\n"
        "let _manifest = null, _stagedManifest = null, _halted = null, _allLanded = false, _gate = { arm: 'm' };\n"
        "let _incompleteChunks = [], _crossRepoRows = [], _sizingRel = null, _planRel = null, _runId = 'run-1';\n"
        + ask_compose._ADMIT_MANIFEST_JS
        + "\n"
        + ask_compose._stage_reply_js(head="", anchor="", run_id="run-1")
        + f"\nawait _adoptStageReply(cfg.reply);\n{script[start:end]}\n"
        "console.log(JSON.stringify({ seen: _seen, manifest: _manifest, halted: _halted }));\n"
        "})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )
    cfg = {"reply": reply, "pages": pages if pages is not None else pages_cfg()}
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def fold_manifest(manifest) -> dict:
    """`_fold` over the stage reply the engine op would return for `manifest`."""
    return _fold(stage_reply(manifest))


def test_a_whole_stage_reply_is_adopted_from_its_pages_with_no_other_agent_behind_it():
    out = fold_manifest(_WHOLE)
    assert out["halted"] is None
    assert out["manifest"] == _WHOLE
    assert out["seen"] == [_PAGE]


def test_a_trimmed_manifest_halts_naming_the_missing_field():
    out = fold_manifest(_TRIMMED)
    assert set(out["seen"]) == {_PAGE}
    assert out["halted"]["kind"] == "refusal"
    assert out["halted"]["reason"] == "stage manifest relay is not whole: review_declared_paths"


def test_a_manifest_without_a_head_sha_halts_naming_it():
    out = fold_manifest({k: v for k, v in _WHOLE.items() if k != "head_sha"})
    assert out["halted"]["reason"] == "stage manifest relay is not whole: head_sha"


def test_a_manifest_with_a_malformed_head_sha_halts_naming_it():
    out = fold_manifest({**_WHOLE, "head_sha": "HEAD"})
    assert out["halted"]["reason"] == "stage manifest relay is not whole: head_sha"


def test_a_stage_error_halts_without_a_page_leg():
    out = _fold({"error": "boom"})
    assert out["seen"] == []
    assert out["halted"]["reason"] == "stage refused: boom"


def test_no_reply_at_all_keeps_the_no_folded_manifest_halt():
    out = _fold(None)
    assert out["seen"] == []
    assert out["halted"]["reason"].startswith("no folded stage manifest")


def test_a_page_set_that_does_not_match_its_handle_halts_naming_the_sidecar():
    reply = stage_reply(_WHOLE)
    pages = pages_cfg()
    pages[reply["manifest_handle"]["sha256"]][0]["chunk"] = "{}"
    out = _fold(reply, pages=pages)
    assert out["halted"]["reason"].startswith("stage reply sidecar does not match its handle:")
