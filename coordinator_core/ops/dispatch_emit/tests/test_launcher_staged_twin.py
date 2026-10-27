"""Tests for the engine-side staged twin writer."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from coordinator_core.ops.dispatch_emit import launcher_staged_twin as twin
from coordinator_core.ops.dispatch_emit import nested_run_contract as nrc

RAW = b'{"rows":[{"id":"C1"}]}'
SHA = hashlib.sha256(RAW).hexdigest()
HANDLE = {"path": "state/warp/run1/manifest.json", "sha256": SHA, "bytes": len(RAW)}
SCRIPT = f"export const meta = {{ name: 'x' }}\n{nrc.STAGED_INLINE_SLOT}\nreturn 1;\n"
CAP = 524288


def _call(root, rel="state/warp/run1/manifest.json", raw=RAW, handle=HANDLE, cap=CAP):
    twin.ensure_twin(root, rel, raw, handle, max_bytes=cap, refuse=ValueError)


def _script(root, text=SCRIPT, run_id="run1"):
    p = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _twin_path(root):
    return root / nrc.staged_twin_rel("run1", SHA)


@pytest.mark.parametrize("rel", ["state/warp/run1/manifest.json", "scratch/warp/run1/manifest.json"])
def test_slot_present_writes_exact_twin_for_each_record_root(tmp_path, rel):
    _script(tmp_path)
    _call(tmp_path, rel=rel)
    inline = json.dumps({"handle": HANDLE, "text": RAW.decode()}, ensure_ascii=True)
    expected = SCRIPT.replace(nrc.STAGED_INLINE_SLOT, f"const _STAGED_INLINE = {inline};")
    assert _twin_path(tmp_path).read_text(encoding="utf-8") == expected


def test_identical_rerun_returns_path_without_token(tmp_path):
    _script(tmp_path)
    first = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
    assert first == twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
    assert "runToken" not in _twin_path(tmp_path).read_text(encoding="utf-8")


def test_no_script_writes_nothing(tmp_path):
    _call(tmp_path)
    assert not _twin_path(tmp_path).exists()


@pytest.mark.parametrize("text", ["return 1;\n", SCRIPT + nrc.STAGED_INLINE_SLOT + "\n"])
def test_zero_or_two_slot_lines_write_nothing(tmp_path, text):
    _script(tmp_path, text)
    _call(tmp_path)
    assert not _twin_path(tmp_path).exists()


@pytest.mark.parametrize("rel", ["state/warp/run1/other.json", "state/other/run1/manifest.json", "state/warp/../x/manifest.json"])
def test_other_sidecar_paths_write_nothing(tmp_path, rel):
    _script(tmp_path)
    _call(tmp_path, rel=rel)
    assert not _twin_path(tmp_path).exists()


def test_over_cap_refuses_and_writes_nothing(tmp_path):
    _script(tmp_path)
    with pytest.raises(ValueError, match="over the Workflow runner"):
        _call(tmp_path, cap=50)
    assert not _twin_path(tmp_path).exists()


def test_identical_rerun_does_not_rewrite(tmp_path):
    _script(tmp_path)
    _call(tmp_path)
    p = _twin_path(tmp_path)
    os.utime(p, (1000, 1000))
    _call(tmp_path)
    assert p.stat().st_mtime == 1000
    assert not list(p.parent.glob("*.tmp"))


def test_remove_twins_deletes_this_runs_twins_only(tmp_path):
    d = tmp_path / "scratch" / "warp"
    d.mkdir(parents=True)
    other_sha = "b" * 64
    mine = [d / f"run1.staged-{SHA}.workflow.mjs", d / f"run1.staged-{other_sha}.workflow.mjs"]
    keep = [
        d / f"run10.staged-{SHA}.workflow.mjs",
        d / f"run1.x.staged-{SHA}.workflow.mjs",
        d / "run1.workflow.mjs",
        d / "run1.staged-short.workflow.mjs",
    ]
    for p in mine + keep:
        p.write_text("x")
    assert twin.remove_twins(tmp_path, "run1") == 2
    assert not any(p.exists() for p in mine)
    assert all(p.exists() for p in keep)
    assert twin.remove_twins(tmp_path, "run1") == 0


@pytest.mark.parametrize("run_id", [None, "", "../x", "a/b", 5])
def test_remove_twins_invalid_run_id_removes_nothing(tmp_path, run_id):
    d = tmp_path / "scratch" / "warp"
    d.mkdir(parents=True)
    p = d / f"x.staged-{SHA}.workflow.mjs"
    p.write_text("x")
    assert twin.remove_twins(tmp_path, run_id) == 0
    assert p.exists()


def test_remove_twins_no_directory_is_zero(tmp_path):
    assert twin.remove_twins(tmp_path, "run1") == 0


def test_sweep_removes_over_ttl_keeps_under_ttl_and_spawns_nothing(tmp_path, monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    d = tmp_path / "scratch" / "warp"
    d.mkdir(parents=True)
    old = d / f"runA.staged-{SHA}.workflow.mjs"
    fresh = d / f"runB.staged-{SHA}.workflow.mjs"
    old.write_text("x")
    fresh.write_text("x")
    from coordinator_core.ops.dispatch_emit import run_token

    now = 10_000_000.0
    os.utime(old, (now - run_token.TTL_SECONDS - 5,) * 2)
    os.utime(fresh, (now - run_token.TTL_SECONDS + 5,) * 2)
    assert twin.sweep_expired_twins(tmp_path, now=now) == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_leaves_non_twin_names_even_when_old(tmp_path):
    d = tmp_path / "scratch" / "warp"
    d.mkdir(parents=True)
    keep = [
        d / "run1.workflow.mjs",
        d / "run1.staged-short.workflow.mjs",
        d / f".staged-{SHA}.workflow.mjs",
        d / f"run1.staged-{SHA}.workflow.mjs.bak",
    ]
    for p in keep:
        p.write_text("x")
        os.utime(p, (1000, 1000))
    assert twin.sweep_expired_twins(tmp_path, now=10_000_000.0) == 0
    assert all(p.exists() for p in keep)


def test_sweep_no_directory_is_zero(tmp_path):
    assert twin.sweep_expired_twins(tmp_path) == 0


def _inline(root):
    text = _twin_path(root).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("const _STAGED_INLINE = "))
    return json.loads(line[len("const _STAGED_INLINE = "):-1])


def _run(root, sizing_text, rel="state/sizings/s1.yaml"):
    (root / "state" / "warp" / "run1").mkdir(parents=True, exist_ok=True)
    (root / "state" / "warp" / "run1" / "run.json").write_text(json.dumps({"sizing_rel": rel}), encoding="utf-8")
    if sizing_text is not None:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(sizing_text, encoding="utf-8")


def test_resume_block_for_plan_edged_s_sizing(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_fire import resolve_arm

    _script(tmp_path)
    _run(tmp_path, "estimate:\n  tshirt: S\nplan: docs/plans/p.md\n")
    _call(tmp_path)
    assert _inline(tmp_path)[nrc.STAGED_RESUME_KEY] == {
        "arm": resolve_arm({"estimate": {"tshirt": "S"}}),
        "sizing_rel": "state/sizings/s1.yaml",
        "plan_rel": "docs/plans/p.md",
    }


def test_resume_plan_rel_null_for_xs_sizing_without_edge(tmp_path):
    _script(tmp_path)
    _run(tmp_path, "estimate:\n  tshirt: XS\n")
    _call(tmp_path)
    assert _inline(tmp_path)[nrc.STAGED_RESUME_KEY]["plan_rel"] is None


@pytest.mark.parametrize("sizing_text", [None, "estimate: {}\n", "- not a mapping\n"])
def test_unreadable_sizing_omits_resume(tmp_path, sizing_text):
    _script(tmp_path)
    _run(tmp_path, sizing_text)
    _call(tmp_path)
    assert nrc.STAGED_RESUME_KEY not in _inline(tmp_path)


def test_no_run_json_omits_resume(tmp_path):
    _script(tmp_path)
    _call(tmp_path)
    assert nrc.STAGED_RESUME_KEY not in _inline(tmp_path)


def _escrowed_run(root):
    from coordinator_core.ops.dispatch_emit import run_token

    (root / ".git").mkdir(exist_ok=True)
    token = run_token.mint(root, "run1", "scratch/warp/run1.workflow.mjs")
    assert run_token.escrow(root, "run1", token)
    return token


def test_escrowed_token_and_resume_block_share_one_twin(tmp_path):
    _script(tmp_path)
    _run(tmp_path, "estimate:\n  tshirt: S\nplan: docs/plans/p.md\n")
    token = _escrowed_run(tmp_path)
    _call(tmp_path)
    inline = _inline(tmp_path)
    assert inline[nrc.STAGED_RESUME_KEY]["plan_rel"] == "docs/plans/p.md"
    assert inline["runToken"] == token


def _stage(root, **kw):
    return twin.ensure_twin(root, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError, **kw)


def test_escrowed_token_is_inlined_without_explicit_token_and_repeat_writes_nothing(tmp_path):
    _script(tmp_path)
    token = _escrowed_run(tmp_path)
    assert _stage(tmp_path) == nrc.staged_twin_rel("run1", SHA)
    path = _twin_path(tmp_path)
    assert f'"runToken": "{token}"' in path.read_text(encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    _stage(tmp_path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_no_escrow_keeps_tokenless_twin(tmp_path):
    _script(tmp_path)
    (tmp_path / ".git").mkdir()
    _stage(tmp_path)
    assert "runToken" not in _twin_path(tmp_path).read_text(encoding="utf-8")


def test_after_consume_next_call_writes_tokenless_twin(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_token

    _script(tmp_path)
    _escrowed_run(tmp_path)
    _stage(tmp_path)
    run_token.consume(tmp_path, "run1")
    _stage(tmp_path)
    assert "runToken" not in _twin_path(tmp_path).read_text(encoding="utf-8")


def test_escrow_on_noop_branch_returns_none_quietly(tmp_path):
    _escrowed_run(tmp_path)
    assert twin.ensure_twin(tmp_path, "state/other/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError) is None
    got = _stage(tmp_path)
    assert got is None


def test_sweep_expired_twins_also_sweeps_aged_escrows(tmp_path):
    import time

    from coordinator_core.ops.dispatch_emit import run_token

    _escrowed_run(tmp_path)
    escrow_file = tmp_path / ".git" / "coordinator-warp-run-tokens" / "escrow" / "run1.json"
    assert escrow_file.exists()
    twin.sweep_expired_twins(tmp_path, now=time.time() + run_token.TTL_SECONDS + 60)
    assert not escrow_file.exists()
