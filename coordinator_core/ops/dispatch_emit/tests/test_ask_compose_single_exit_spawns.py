"""The composed M+ ask script has exactly one `return` outside function bodies: the terminal one."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import PurePath

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, shared_decls
from coordinator_core.session.record_homes import record_path
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _m_plus_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=PurePath(record_path("", "sizings", "x.yaml")).as_posix(),
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _merge_and_guard_fragment(script: str) -> str:
    start = script.index("if (!_halted) _reviewPrep = (() => {")
    end_marker = f"halted: '{NO_SLICES_HALT}'"
    end = script.index("\n", script.index(end_marker, start))
    return shared_decls(script) + "\n" + script[start:end]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize(
    ("slice_id", "halts"),
    [("...b90a92-9c1affcf4e0d-prep-1+...-prep-2", False), ("", True)],
)
def test_ask_wave_review_naming_slices_prevents_the_no_slices_halt(monkeypatch: pytest.MonkeyPatch, slice_id, halts) -> None:
    fragment = _merge_and_guard_fragment(_m_plus_script(monkeypatch))
    harness = (
        "let _halted = null;\n"
        "let _reviewPrep = null;\n"
        "const _prepReplies = [{ verdict: 'partition', slices: [], product_files: 3 }];\n"
        f"const _waveRaw = [{{ prep: _prepReplies[0], review: {{ slice_id: {json.dumps(slice_id)}, sidecar_path: 'x.md' }} }}];\n"
        "const _busyPrefix = 'review.prep busy:';\n"
        "const _busyReplies = [];\n"
        "const _malformed = [];\n"
        "const _foreignAdvisories = [];\n"
        f"{fragment}\n"
        "process.stdout.write(JSON.stringify({ halted: _halted && _halted.halted, n: _reviewPrep.slices.length }));\n"
    )
    out = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    result = json.loads(out.stdout)
    assert bool(result["halted"] and result["halted"].startswith(NO_SLICES_HALT)) is halts
    if not halts:
        assert result["n"] == 2


_DOCS_ONLY_PATHS = ["docs/plans/p.evidence.yaml"]
_PRODUCT_PATHS = ["docs/plans/p.evidence.yaml", "pkg/mod.py"]
_NO_PRODUCT_REPLIES = {
    "null relay reply": [],
    "degraded reply": [{"verdict": None, "basis": "", "error": None, "product_files": 0, "slices": [], "excluded": []}],
    "errored reply": [{"verdict": None, "basis": "", "error": "freeze failed", "product_files": 0, "slices": []}],
}
_ZERO_FILE_REPLY = [{"verdict": None, "basis": "worktree", "error": None, "product_files": 0, "slices": [], "foreign_claims": []}]


def _run_ask_guard(monkeypatch, replies, review_paths):
    fragment = _merge_and_guard_fragment(_m_plus_script(monkeypatch))
    harness = (
        "let _halted = null;\n"
        "let _reviewPrep = null;\n"
        f"const _reviewPaths = {json.dumps(review_paths)};\n"
        f"const _prepReplies = {json.dumps(replies)};\n"
        "const _waveRaw = _prepReplies.map(p => ({ prep: p, review: null }));\n"
        "const _busyPrefix = 'review.prep busy:';\n"
        "const _busyReplies = [];\n"
        "const _malformed = [];\n"
        "const _foreignAdvisories = [];\n"
        f"{fragment}\n"
        "process.stdout.write(JSON.stringify({ halted: _halted && _halted.halted, verifyOnly: _verifyOnly }));\n"
    )
    out = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize("shape", sorted(_NO_PRODUCT_REPLIES))
def test_ask_verification_wave_with_no_product_path_passes_review_empty_whatever_the_prep_reply(monkeypatch: pytest.MonkeyPatch, shape) -> None:
    result = _run_ask_guard(monkeypatch, _NO_PRODUCT_REPLIES[shape], _DOCS_ONLY_PATHS)
    assert result == {"halted": None, "verifyOnly": True}


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize("shape", sorted(_NO_PRODUCT_REPLIES))
def test_ask_wave_with_a_declared_product_write_and_a_failed_prep_still_halts(monkeypatch: pytest.MonkeyPatch, shape) -> None:
    result = _run_ask_guard(monkeypatch, _NO_PRODUCT_REPLIES[shape], _PRODUCT_PATHS)
    assert result["halted"].startswith(NO_SLICES_HALT)
    assert result["verifyOnly"] is False


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_ask_docs_only_wave_whose_prep_reports_product_or_a_partition_still_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    for reply in (
        {"verdict": "PARTITION-MANDATORY", "basis": "", "error": "x", "product_files": 0, "slices": []},
        {"verdict": None, "basis": "", "error": "x", "product_files": 0, "slices": [], "foreign_claims": ["peer.py"]},
    ):
        result = _run_ask_guard(monkeypatch, [reply], _DOCS_ONLY_PATHS)
        assert result["halted"].startswith(NO_SLICES_HALT)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_ask_zero_file_reply_keeps_its_no_op_and_verify_only_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clean zero-file reply keeps its no-op over docs-only paths and verify-only over a declared product write."""
    assert _run_ask_guard(monkeypatch, _ZERO_FILE_REPLY, _DOCS_ONLY_PATHS)["halted"] == "no-op"
    assert _run_ask_guard(monkeypatch, _ZERO_FILE_REPLY, _PRODUCT_PATHS) == {"halted": None, "verifyOnly": True}
