"""Tests for the engine-side staged twin writer."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
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


_UNRECOMPOSABLE = (
    "staged twin write failed: run script 'run1.workflow.mjs' is absent and cannot be recomposed "
    r".*start a fresh engine-ask.*state/warp/run1/requirements\.md"
)


@pytest.mark.twin_needs_script
def test_no_script_refuses_naming_a_fresh_ask_and_writes_nothing(tmp_path):
    with pytest.raises(ValueError, match=_UNRECOMPOSABLE) as caught:
        _call(tmp_path)
    assert "re-arm" in str(caught.value) and "re-arm refusal halt" not in str(caught.value)
    assert not _twin_path(tmp_path).exists()


@pytest.mark.twin_needs_script
def test_missing_script_of_an_emitted_run_without_halt_marker_refuses_with_remedy(tmp_path):
    (tmp_path / "state" / "warp" / "run1").mkdir(parents=True)
    (tmp_path / "state" / "warp" / "run1" / "run.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match=_UNRECOMPOSABLE):
        _call(tmp_path)
    assert not _twin_path(tmp_path).exists()


@pytest.mark.twin_needs_script
def test_absent_script_is_recomposed_and_the_twin_staged_from_it(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halt_resume_build as hrb

    calls = []

    def fake_recompose(root, run_id):
        calls.append(run_id)
        return SCRIPT, ""

    monkeypatch.setattr(hrb, "recompose_script", fake_recompose)
    rel = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
    assert calls == ["run1"] and rel == nrc.staged_twin_rel("run1", SHA)
    inline = json.dumps({"handle": HANDLE, "text": RAW.decode()}, ensure_ascii=True)
    assert _twin_path(tmp_path).read_text(encoding="utf-8") == SCRIPT.replace(
        nrc.STAGED_INLINE_SLOT, f"const _STAGED_INLINE = {inline};"
    )


@pytest.mark.twin_needs_script
def test_absent_scratch_script_stages_from_the_run_dirs_kept_composed_script(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halt_resume_build as hrb

    monkeypatch.setattr(hrb, "build", lambda *a: pytest.fail("build ran though a kept script exists"))
    kept = tmp_path / "state" / "warp" / "run1" / hrb.COMPOSED_SCRIPT_FILE
    kept.parent.mkdir(parents=True)
    kept.write_text(SCRIPT, encoding="utf-8")
    hrr.write(kept.parent, {"schema": hrr.SCHEMA, "fingerprint": {"engine_sha": hrr.engine_sha()}})
    rel = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
    assert rel == nrc.staged_twin_rel("run1", SHA)
    assert "const _STAGED_INLINE = " in _twin_path(tmp_path).read_text(encoding="utf-8")


@pytest.mark.twin_needs_script
def test_kept_script_composed_by_another_engine_is_rebuilt(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halt_resume_build as hrb

    kept = tmp_path / "state" / "warp" / "run1" / hrb.COMPOSED_SCRIPT_FILE
    kept.parent.mkdir(parents=True)
    kept.write_text(SCRIPT, encoding="utf-8")
    hrr.write(kept.parent, {"schema": hrr.SCHEMA, "fingerprint": {"engine_sha": "0" * 40}})
    built = []
    monkeypatch.setattr(hrb, "build", lambda root, run_id: built.append(run_id))
    hrb.recompose_script(tmp_path, "run1")
    assert built == ["run1"]


@pytest.mark.twin_needs_script
def test_present_script_never_recomposes(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halt_resume_build as hrb

    monkeypatch.setattr(hrb, "recompose_script", lambda *a: pytest.fail("recompose on the warm path"))
    _script(tmp_path)
    _call(tmp_path)
    assert _twin_path(tmp_path).is_file()


@pytest.mark.twin_needs_script
def test_recompose_script_reads_the_built_script_and_names_a_gate_refusal(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halt_resume_build as hrb

    built = tmp_path / "state" / "warp" / "run1" / hrb.COMPOSED_SCRIPT_FILE
    built.parent.mkdir(parents=True)

    def fake_build(root, run_id):
        built.write_text(SCRIPT, encoding="utf-8")
        return {"composed_script_rel": built.relative_to(tmp_path).as_posix()}

    monkeypatch.setattr(hrb, "build", fake_build)
    assert hrb.recompose_script(tmp_path, "run1") == (SCRIPT, "")
    built.unlink()
    monkeypatch.setattr(
        hrb, "build", lambda root, run_id: {"composed_script_rel": None, "gate_verdict": {"halt": {"reason": "sizing gate said no"}}}
    )
    assert hrb.recompose_script(tmp_path, "run1") == (None, "sizing gate said no")
    monkeypatch.setattr(hrb, "build", lambda root, run_id: None)
    assert hrb.recompose_script(tmp_path, "run1")[0] is None


def test_zero_slot_script_is_a_standalone_run_and_writes_nothing(tmp_path):
    _script(tmp_path, "return 1;\n")
    assert twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError) is None
    assert not _twin_path(tmp_path).exists()


def test_two_slot_lines_refuse_by_name_and_write_nothing(tmp_path):
    _script(tmp_path, SCRIPT + nrc.STAGED_INLINE_SLOT + "\n")
    with pytest.raises(ValueError, match="staged twin write failed.*2 inline slots"):
        _call(tmp_path)
    assert not _twin_path(tmp_path).exists()


def test_write_and_identical_paths_return_the_on_disk_twin(tmp_path):
    _script(tmp_path)
    rel = nrc.staged_twin_rel("run1", SHA)
    for _ in range(2):
        got = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError)
        assert got == rel
        assert "const _STAGED_INLINE = " in (tmp_path / got).read_text(encoding="utf-8")


def test_unreadable_script_refuses_by_name(tmp_path):
    p = tmp_path / "scratch" / "warp" / "run1.workflow.mjs"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ValueError, match="staged twin write failed.*unreadable"):
        _call(tmp_path)


def test_write_oserror_refuses_by_name(tmp_path):
    _script(tmp_path)
    _twin_path(tmp_path).mkdir(parents=True)
    with pytest.raises(ValueError, match="staged twin write failed"):
        _call(tmp_path)
    assert not list(_twin_path(tmp_path).parent.glob("*.tmp"))


def test_uncontained_target_refuses_by_name(tmp_path, monkeypatch):
    _script(tmp_path)
    monkeypatch.setattr(twin._path_guard, "contained_path", lambda *a, **k: None)
    with pytest.raises(ValueError, match="staged twin write failed.*outside"):
        _call(tmp_path)


@pytest.mark.parametrize("rel", ["state/warp/run1/other.json", "state/other/run1/manifest.json", "state/warp/../x/manifest.json"])
def test_other_sidecar_paths_write_nothing(tmp_path, rel):
    _script(tmp_path)
    _call(tmp_path, rel=rel)
    assert not _twin_path(tmp_path).exists()


def test_over_cap_refuses_and_writes_nothing(tmp_path):
    _script(tmp_path)
    with pytest.raises(ValueError, match=f"^{nrc.STAGED_TWIN_OVER_CAP_HEAD}: twin is \\d+ bytes, over the 50-byte cap"):
        _call(tmp_path, cap=50)
    assert not _twin_path(tmp_path).exists()
    assert not (tmp_path / nrc.staged_spill_rel("run1", SHA)).exists()


def test_over_cap_uses_the_distinct_over_cap_builder(tmp_path):
    class OverCap(ValueError):
        pass

    _script(tmp_path)
    with pytest.raises(OverCap, match=nrc.STAGED_TWIN_OVER_CAP_HEAD):
        twin.ensure_twin(
            tmp_path, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=50, refuse=ValueError, refuse_over_cap=OverCap
        )


def _big_manifest(writes_per_row: int, rows: int = 2) -> tuple[bytes, dict]:
    manifest = {
        "head_sha": "a" * 40,
        "rows": [
            {
                "id": f"C{i}",
                "brief_path": f"scratch/b{i}.md",
                "agent_type": "coordinator:executor",
                "title": "t" * 200,
                "writes": [f"pkg/mod{i}/file_{j}.py" for j in range(writes_per_row)],
            }
            for i in range(rows)
        ],
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    handle = {"path": "state/warp/run1/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return raw, handle


SMALL_CAP = 8000


def test_small_manifest_inlines_whole_with_no_spill(tmp_path):
    _script(tmp_path)
    raw, handle = _big_manifest(4)
    rel = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=SMALL_CAP, refuse=ValueError)
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "pkg/mod0/file_3.py" in text and "full_handle" not in text
    assert not (tmp_path / nrc.staged_spill_rel("run1", handle["sha256"])).exists()


def test_400_writes_over_budget_slims_the_twin_and_spills_the_full_manifest(tmp_path):
    _script(tmp_path)
    raw, handle = _big_manifest(400)
    assert len(raw) > SMALL_CAP
    rel = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=SMALL_CAP, refuse=ValueError)
    assert (tmp_path / rel).stat().st_size < SMALL_CAP
    spill = tmp_path / nrc.staged_spill_rel("run1", handle["sha256"])
    assert spill.read_bytes() == raw
    assert hashlib.sha256(spill.read_bytes()).hexdigest() == handle["sha256"]


def test_slim_twin_size_is_independent_of_writes_count(tmp_path):
    sizes = {}
    for count in (400, 4000):
        root = tmp_path / f"r{count}"
        _script(root)
        raw, handle = _big_manifest(count)
        rel = twin.ensure_twin(root, "state/warp/run1/manifest.json", raw, handle, max_bytes=SMALL_CAP, refuse=ValueError)
        sizes[count] = (root / rel).stat().st_size
    assert abs(sizes[4000] - sizes[400]) < 64


def test_slim_inline_text_has_handle_of_its_own_and_counts_writes(tmp_path):
    _script(tmp_path)
    raw, handle = _big_manifest(400, rows=1)
    twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=SMALL_CAP, refuse=ValueError)
    inline = json.loads(next(
        ln for ln in (tmp_path / nrc.staged_twin_rel("run1", handle["sha256"])).read_text(encoding="utf-8").splitlines()
        if ln.startswith("const _STAGED_INLINE = ")
    )[len("const _STAGED_INLINE = "):-1])
    assert inline["full_handle"] == handle
    assert inline[nrc.STAGED_SPILL_KEY] == nrc.staged_spill_rel("run1", handle["sha256"])
    assert inline["handle"]["sha256"] == hashlib.sha256(inline["text"].encode("ascii")).hexdigest()
    assert inline["handle"]["bytes"] == len(inline["text"])
    row = json.loads(inline["text"])["rows"][0]
    assert row["writes"] == [] and row["writes_count"] == 400 and "title" not in row
    assert row["id"] == "C0" and row["brief_path"] == "scratch/b0.md"


def test_manifest_without_rows_list_inlines_whole_with_no_spill(tmp_path):
    _script(tmp_path)
    raw = b'{"other":1}'
    handle = {"path": "state/warp/run1/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=CAP, refuse=ValueError)
    text = (tmp_path / nrc.staged_twin_rel("run1", handle["sha256"])).read_text(encoding="utf-8")
    assert '"text": "{\\"other\\":1}"' in text and "full_handle" not in text
    assert not (tmp_path / nrc.staged_spill_rel("run1", handle["sha256"])).exists()


def test_spill_write_failure_refuses_by_name(tmp_path):
    _script(tmp_path)
    raw, handle = _big_manifest(400)
    (tmp_path / nrc.staged_spill_rel("run1", handle["sha256"])).mkdir(parents=True)
    with pytest.raises(ValueError, match="staged twin write failed.*spill"):
        twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=SMALL_CAP, refuse=ValueError)
    assert not (tmp_path / nrc.staged_twin_rel("run1", handle["sha256"])).exists()


def test_remove_twins_and_sweep_also_remove_the_spill(tmp_path):
    d = tmp_path / "scratch" / "warp"
    d.mkdir(parents=True)
    spill = d / f"run1.staged-{SHA}.spill.json"
    old = d / f"runA.staged-{SHA}.spill.json"
    spill.write_text("x")
    old.write_text("x")
    os.utime(old, (1000, 1000))
    assert twin.sweep_expired_twins(tmp_path, now=10_000_000.0) == 1
    assert not old.exists() and spill.exists()
    assert twin.remove_twins(tmp_path, "run1") == 1
    assert not spill.exists()


def _manifest_for(raw: bytes) -> dict:
    return {"path": "state/warp/run1/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def test_new_sha_twin_prunes_this_runs_older_twin_and_spill_but_not_another_runs(tmp_path):
    _script(tmp_path)
    _script(tmp_path, run_id="run2")
    d = tmp_path / "scratch" / "warp"
    old_sha = "c" * 64
    old_twin = d / f"run1.staged-{old_sha}.workflow.mjs"
    old_spill = d / f"run1.staged-{old_sha}.spill.json"
    other = d / f"run2.staged-{old_sha}.workflow.mjs"
    for p in (old_twin, old_spill, other):
        p.write_text("x", encoding="utf-8")
    _call(tmp_path)
    assert _twin_path(tmp_path).is_file()
    assert not old_twin.exists() and not old_spill.exists()
    assert other.is_file() and (d / "run2.workflow.mjs").is_file()


def test_prune_keeps_the_new_twin_and_its_spill(tmp_path):
    _script(tmp_path)
    raw, handle = _big_manifest(400)
    stale = tmp_path / nrc.staged_twin_rel("run1", "d" * 64)
    stale.write_text("x", encoding="utf-8")
    twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=SMALL_CAP, refuse=ValueError)
    assert (tmp_path / nrc.staged_twin_rel("run1", handle["sha256"])).is_file()
    assert (tmp_path / nrc.staged_spill_rel("run1", handle["sha256"])).is_file()
    assert not stale.exists()


def test_prune_unlink_failure_does_not_refuse_the_twin_write(tmp_path, monkeypatch):
    _script(tmp_path)
    stale = tmp_path / nrc.staged_twin_rel("run1", "e" * 64)
    stale.write_text("x", encoding="utf-8")
    real_unlink = type(stale).unlink

    def flaky(self, *a, **k):
        if self.name == stale.name:
            raise PermissionError("held open")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(stale), "unlink", flaky)
    _call(tmp_path)
    assert _twin_path(tmp_path).is_file() and stale.exists()


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
    block = _inline(tmp_path)[nrc.STAGED_RESUME_KEY]
    assert block == {
        "arm": resolve_arm({"estimate": {"tshirt": "S"}}),
        "sizing_rel": "state/sizings/s1.yaml",
        "plan_rel": "docs/plans/p.md",
        "tshirt": "S",
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
    _script(tmp_path, "return 1;\n")
    assert twin.ensure_twin(tmp_path, "state/other/run1/manifest.json", RAW, HANDLE, max_bytes=CAP, refuse=ValueError) is None
    got = _stage(tmp_path)
    assert got is None


def test_sweep_expired_twins_also_sweeps_aged_escrows(tmp_path):
    import time

    from coordinator_core.ops.dispatch_emit import run_token

    _escrowed_run(tmp_path)
    escrow_file = run_token._record_path(tmp_path, "run1", escrow=True)
    assert escrow_file.exists()
    twin.sweep_expired_twins(tmp_path, now=time.time() + run_token.TTL_SECONDS + 60)
    assert not escrow_file.exists()


def _receipt(path):
    """The `.emitted.json` receipt beside a script."""
    return path.with_name(path.name + ".emitted.json")


def test_twin_leaves_receipt_and_remove_twins_removes_it(tmp_path):
    _script(tmp_path)
    _call(tmp_path)
    assert _receipt(_twin_path(tmp_path)).is_file()
    assert twin.remove_twins(tmp_path, "run1") == 1
    assert not _twin_path(tmp_path).exists()
    assert not _receipt(_twin_path(tmp_path)).exists()


def test_identical_rerun_restores_missing_receipt(tmp_path):
    _script(tmp_path)
    _call(tmp_path)
    _receipt(_twin_path(tmp_path)).unlink()
    _call(tmp_path)
    assert _receipt(_twin_path(tmp_path)).is_file()


def _wide_manifest(rows: int = 15, files: int = 150) -> tuple[bytes, dict]:
    """A manifest shaped like the refused 15-row migration: one path set repeated across every scope key."""
    paths = [f"coordinator_core/ops/some_package/sub_module_{i:03d}/handler_{i:03d}_impl.py" for i in range(files)]
    per_row = [paths[(i * 10) % files:][:60] for i in range(rows)]
    manifest = {
        "head_sha": "a" * 40,
        "rows": [
            {"id": f"C{i}", "brief_path": f"scratch/b{i}.md", "agent_type": "coordinator:executor", "model": "sonnet",
             "wave": i % 3, "deps": [], "writes": per_row[i]}
            for i in range(rows)
        ],
        "review_declared_paths": paths,
        "review_code_paths": paths,
        "test_targets": paths,
        "wave_test_targets": {str(w): paths[: (w + 1) * files // 3] for w in range(3)},
        "write_baseline": {p: hashlib.sha256(p.encode()).hexdigest() for p in paths},
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return raw, {"path": "state/warp/run1/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def test_wide_manifest_over_cap_stages_a_packed_twin_under_the_cap_holding_the_whole_manifest(tmp_path):
    raw, handle = _wide_manifest()
    _script(tmp_path, f"export const meta = {{ name: 'x' }}\n// {'p' * 400_000}\n{nrc.STAGED_INLINE_SLOT}\nreturn 1;\n")
    naive = len(SCRIPT) + len(json.dumps({"handle": handle, "text": raw.decode()})) + 400_000
    assert naive > CAP
    rel = twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=CAP, refuse=ValueError)
    path = tmp_path / rel
    assert path.stat().st_size <= CAP
    assert not (tmp_path / nrc.staged_spill_rel("run1", handle["sha256"])).exists()
    line = next(ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("const _STAGED_INLINE = "))
    assert nrc.STAGED_UNPACK_JS in path.read_text(encoding="utf-8")
    inline = json.loads(line[len("const _STAGED_INLINE = _unpackStaged("):-2])
    assert inline["handle"] == handle and "full_handle" not in inline
    pack = inline[nrc.STAGED_PACK_KEY]
    parts = inline["text"].split(pack["m"])
    for i in range(1, len(parts), 2):
        parts[i] = pack["t"][int(parts[i], 36)]
    assert "".join(parts).encode("ascii") == raw
    assert hashlib.sha256("".join(parts).encode("ascii")).hexdigest() == handle["sha256"]


def test_pack_keeps_the_refusal_for_a_body_that_cannot_shrink(tmp_path):
    raw, handle = _wide_manifest()
    _script(tmp_path)
    with pytest.raises(ValueError, match=nrc.STAGED_TWIN_OVER_CAP_HEAD):
        twin.ensure_twin(tmp_path, "state/warp/run1/manifest.json", raw, handle, max_bytes=300, refuse=ValueError)
    assert not _twin_path(tmp_path).exists()


def test_pack_without_repeated_strings_or_free_mark_is_declined():
    assert twin._pack_manifest('{"a":"unique-one","b":"unique-two"}') is None
    repeated = '"a-repeated-string-literal"'
    assert twin._pack_manifest(f'[{repeated},{repeated}]') is not None
    assert twin._pack_manifest(f'[{repeated},{repeated},"{twin._PACK_MARKS}"]') is None


def test_sweep_expired_twins_removes_receipt(tmp_path):
    _script(tmp_path)
    _call(tmp_path)
    assert twin.sweep_expired_twins(tmp_path, now=os.path.getmtime(_twin_path(tmp_path)) + 10**9) == 1
    assert not _receipt(_twin_path(tmp_path)).exists()
