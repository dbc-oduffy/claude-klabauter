"""A refire never cites a test leg measured on older work: the verify call carries the judged work's identity and the
review_blocked halt drops a failing list the freshness check flags."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import record_path
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose_halt_self_correction import _HARNESS, _OWN, _OWN_TEST, _OWNED, _SIZING, _done, _red

RUN_ID = "run-fresh"
HEAD = "a" * 40
OTHER = "b" * 40


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "refs" / "heads" / "main").write_text(HEAD + "\n", encoding="utf-8")
    return root


def _leg(root: Path, **fields) -> None:
    path = measured_rev_record_path(root, RUN_ID).parent / "test-leg.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def _own(root: Path, rel: str = "src/a.py", body: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    old = datetime.now(timezone.utc).timestamp() - 3600
    os.utime(path, (old, old))


def _iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def _identity(root: Path, writes=("src/a.py",)) -> dict:
    return ask_compose._judged_work_identity(str(root), RUN_ID, writes)


def test_a_leg_with_no_stamp_is_flagged_stale_in_the_identity(repo):
    _own(repo)
    _leg(repo, status="fail")

    assert _identity(repo)["stale"]


def test_a_leg_measured_on_the_current_head_after_the_own_diff_is_not_stale(repo):
    _own(repo)
    _leg(repo, status="pass", measured_at=_iso(-60), head_sha=HEAD)

    assert _identity(repo)["stale"] is None


def test_a_head_that_moved_past_the_leg_is_stale_and_changes_the_identity(repo):
    _own(repo)
    _leg(repo, status="pass", measured_at=_iso(-60), head_sha=OTHER)
    stale = _identity(repo)
    _leg(repo, status="pass", measured_at=_iso(-60), head_sha=HEAD)
    fresh = _identity(repo)

    assert stale["stale"] and fresh["stale"] is None
    assert stale != fresh


def test_an_own_diff_edit_changes_the_fingerprint_and_flags_the_leg(repo):
    _own(repo)
    _leg(repo, status="pass", measured_at=_iso(-7200), head_sha=HEAD)
    before = _identity(repo)
    (repo / "src/a.py").write_text("edited more", encoding="utf-8")
    after = _identity(repo)

    assert before["own_diff"] != after["own_diff"]
    assert after["stale"] and "written after" in after["stale"]
    assert before["head"] == after["head"] == HEAD


def test_an_absolute_or_escaping_write_never_enters_the_fingerprint(repo):
    _own(repo)
    bare = _identity(repo, writes=("src/a.py",))
    odd = _identity(repo, writes=("src/a.py", "../outside.py", str(repo / "src/a.py")))

    assert bare["own_diff"] == odd["own_diff"]


def _script(monkeypatch, identity: dict) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    monkeypatch.setattr(ask_compose, "_judged_work_identity", lambda *a, **k: identity)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=PurePath(record_path("", "sizings", "x.yaml")).as_posix(),
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _verify_host_call(script: str) -> str:
    at = script.index("engineOp('verify.host_test_leg'")
    return script[at : script.index("targets:", at)]


def test_the_verify_call_key_differs_for_a_stale_leg_and_holds_for_a_fresh_one(monkeypatch):
    fresh = {"head": HEAD, "own_diff": "f" * 16, "stale": None}
    stale = {"head": OTHER, "own_diff": "f" * 16, "stale": f"test leg measured {HEAD[:12]}, HEAD is {OTHER[:12]}"}

    fresh_call = _verify_host_call(_script(monkeypatch, fresh))
    again = _verify_host_call(_script(monkeypatch, dict(fresh)))
    stale_call = _verify_host_call(_script(monkeypatch, stale))

    assert fresh_call == again
    assert stale_call != fresh_call
    assert json.dumps(fresh) in fresh_call and "HEAD is" in stale_call


def test_the_composed_script_declares_the_stale_reason_the_halt_reads(monkeypatch):
    reason = "test leg carries no measured_at/head_sha stamp"
    script = _script(monkeypatch, {"head": HEAD, "own_diff": "0" * 16, "stale": reason})
    fresh = _script(monkeypatch, {"head": HEAD, "own_diff": "0" * 16, "stale": None})

    assert f"let _legStale = {json.dumps(reason)};" in script
    assert "let _legStale = null;" in fresh


def _drive(*, reverify, replies, stale, wave=None, reused=True) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = _HARNESS.replace(
        "const _reviewReused = false;",
        f"const _reviewReused = {json.dumps(reused)}; let _legStale = {json.dumps(stale)};",
    )
    program = harness % {
        "sizing": json.dumps(_SIZING),
        "arm": json.dumps("s"),
        "own": ", ".join(json.dumps(p) for p in (_OWN, _OWN_TEST)),
        "wave": json.dumps(wave or []),
        "reverify": json.dumps(reverify),
        "replies": json.dumps(replies),
        "refire": ask_compose._refire_next_action_helper_js(_SIZING),
        "recover": ask_compose._review_recover_js(
            head="H", anchor="A", session_tail="", record_dir="state/warp/r1", run_id="r1"
        ),
        "halt": ask_compose._review_blocked_halt_js(_SIZING),
    }
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.spawns_process
def test_a_reused_stale_leg_never_gets_its_failing_list_cited():
    out = _drive(reverify=_red(_OWNED), replies={}, stale="test leg carries no measured_at/head_sha stamp")

    assert out["calls"] == []
    assert out["halted"] is None or _OWNED not in json.dumps(out["halted"])


@pytest.mark.spawns_process
def test_a_reused_fresh_leg_still_halts_on_its_failing_list():
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [_done()]}, stale=None)

    assert [c["label"] for c in out["calls"]] == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert out["halted"] and _OWNED in json.dumps(out["halted"])


@pytest.mark.spawns_process
def test_a_fresh_leg_this_run_measured_is_cited_even_when_the_stale_flag_was_set():
    out = _drive(
        reverify=_red(_OWNED), replies={}, stale="old", reused=False
    )

    assert out["halted"] and _OWNED in json.dumps(out["halted"])


@pytest.mark.spawns_process
def test_a_fix_leg_reverify_clears_the_stale_flag_so_the_new_failing_list_is_cited():
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 0}]
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _red(_OWNED), "slices": []}]},
        stale="old",
        wave=wave,
    )

    assert HALT_FIX_LABEL in [c["label"] for c in out["calls"]]
    assert out["halted"] and _OWNED in json.dumps(out["halted"])
