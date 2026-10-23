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


def test_run_token_given_is_inlined_and_path_returned(tmp_path):
    _script(tmp_path)
    got = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError, run_token="a" * 32)
    assert got == nrc.staged_twin_rel("run1", SHA)
    inline = json.dumps({"handle": HANDLE, "text": RAW.decode(), "runToken": "a" * 32}, ensure_ascii=True)
    assert f"const _STAGED_INLINE = {inline};" in _twin_path(tmp_path).read_text(encoding="utf-8")


def test_identical_rerun_returns_path_without_token(tmp_path):
    _script(tmp_path)
    first = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
    assert first == twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
    assert "runToken" not in _twin_path(tmp_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("text", [None, "return 1;\n"])
def test_run_token_with_no_twin_to_write_raises_not_none(tmp_path, text):
    if text is not None:
        _script(tmp_path, text)
    with pytest.raises(ValueError, match="run1") as exc:
        twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError, run_token="b" * 32)
    assert "b" * 32 not in str(exc.value)
    assert not _twin_path(tmp_path).exists()


def test_run_token_with_foreign_sidecar_raises(tmp_path):
    with pytest.raises(ValueError):
        twin.ensure_twin(tmp_path, "state/other/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError, run_token="b" * 32)


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
