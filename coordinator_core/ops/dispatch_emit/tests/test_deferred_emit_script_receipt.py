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


def _verify(reply: dict, receipts: dict) -> dict:
    """Run the deferred script's `_verifiedScriptPath` in node against a stub relay; `receipts` maps a script path to
    its `[receipt_sha256, script_sha256]` (absent: no receipt). Returns [verdict, candidates the relay was asked about]."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    block = des._RECEIPT_PATH_JS.replace("__AUTHORITY__", json.dumps("AUTH"))
    code = (
        "const REPO_ROOT = '/r';\n"
        f"const RECEIPTS = {json.dumps(receipts)};\n"
        "const asked = [];\n"
        "const agent = async (prompt) => {\n"
        "  const cand = /\" \"([^\"]+)\"` \\(/.exec(prompt)[1];\n"
        "  asked.push(cand);\n"
        "  const r = RECEIPTS[cand];\n"
        "  if (!r) return { found: false, error: 'No such file' };\n"
        "  return r[0] === r[1] ? { found: true, verdict: 'MATCH' } : { found: true, verdict: 'MISMATCH', detail: 'MISMATCH receipt sha256 ' + r[0] + ' != script sha256 ' + r[1] };\n"
        "};\n"
        + block
        + f"_verifiedScriptPath({json.dumps(reply)}).then((v) => console.log(JSON.stringify([v, asked])));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_SHA = "ab" * 32
_REAL = "/r/scratch/warp/ask-r1.workflow.mjs"


def test_a_wrong_relay_path_still_starts_the_script_the_receipt_names():
    verdict, asked = _verify({"path": "/other/tmp/none", "run_id": "ask-r1"}, {_REAL: [_SHA, _SHA]})
    assert verdict == {"path": _REAL}
    assert asked == [_REAL]


def test_a_wrong_but_script_shaped_relay_path_is_skipped_when_it_has_no_receipt():
    verdict, asked = _verify({"path": "/elsewhere/ask-r1.workflow.mjs", "run_id": "ask-r1"}, {_REAL: [_SHA, _SHA]})
    assert verdict == {"path": _REAL}
    assert asked == ["/elsewhere/ask-r1.workflow.mjs", _REAL]


def test_a_windows_style_relay_path_with_doubled_backslashes_is_normalised_and_verified():
    win = "E:/claude/scratch/warp/ask-r1.workflow.mjs"  # abs-path-ok: carve-out=test-input-data
    verdict, asked = _verify({"path": win.replace("/", "\\\\"), "run_id": "ask-r1"}, {win: [_SHA, _SHA]})
    assert verdict == {"path": win}
    assert asked == [win]


def test_a_receipt_whose_sha_disagrees_with_the_script_bytes_is_refused_by_name():
    verdict, _ = _verify({"path": _REAL, "run_id": "ask-r1"}, {_REAL: [_SHA, "cd" * 32]})
    assert "error" in verdict and "emit receipt sha mismatch" in verdict["error"] and "path" not in verdict


def test_a_match_verdict_starts_without_the_relay_copying_any_digest():
    """The relay returns only the command's MATCH token; no sha256 string crosses the relay."""
    block = des._RECEIPT_PATH_JS
    assert "receipt_sha256" not in block and "script_sha256" not in block
    verdict, _ = _verify({"path": _REAL, "run_id": "ask-r1"}, {_REAL: [_SHA, _SHA]})
    assert verdict == {"path": _REAL}


def test_a_missing_receipt_is_refused_by_name():
    verdict, _ = _verify({"path": _REAL, "run_id": "ask-r1"}, {})
    assert "no emit receipt for " + _REAL in verdict["error"] and "path" not in verdict


def test_a_reply_naming_neither_path_nor_run_id_is_refused():
    verdict, asked = _verify({"path": "/other/none", "run_id": "bad id!"}, {_REAL: [_SHA, _SHA]})
    assert "error" in verdict and asked == []


def test_rendered_script_starts_only_the_verified_path(tmp_path):
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    body = script.read_text(encoding="utf-8")
    assert "_verifiedScriptPath(reply)" in body and "reply.path.replace" not in body


def test_receipt_failure_never_fails_the_emit(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import emission_receipt

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(emission_receipt, "_write_emission_receipt", boom)
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    assert script.is_file()
    assert not script.with_name(script.name + ".emitted.json").exists()


def test_the_receipt_check_command_reads_the_top_level_sha_not_em_brief(tmp_path):
    """The relay copies what the command prints, so the key path must live in the command: a receipt whose
    em_brief.sha256 precedes the top-level sha256 once made a haiku relay report the brief's digest."""
    import hashlib
    import re
    import subprocess
    import sys

    script = tmp_path / "ask-r1.workflow.mjs"
    script.write_bytes(b"export const meta = {}\n")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    (tmp_path / "ask-r1.workflow.mjs.emitted.json").write_text(
        json.dumps({"em_brief": {"sha256": "7b" * 32}, "sha256": digest}), encoding="utf-8")
    cmd = re.search(r"Run `(python3 -c .+?)` \(", des._RECEIPT_PATH_JS).group(1)
    # The JS literal escapes its single quotes; the relay's shell sees them unescaped.
    argv_code = re.match(r'python3 -c "(.+)" "', cmd).group(1).replace("\\'", "'")
    out = subprocess.run([sys.executable, "-c", argv_code, str(script)], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "MATCH"
    (tmp_path / "ask-r1.workflow.mjs.emitted.json").write_text(
        json.dumps({"em_brief": {"sha256": digest}, "sha256": "7b" * 32}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", argv_code, str(script)], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.stdout.strip() == f"MISMATCH receipt sha256 {'7b' * 32} != script sha256 {digest}"
