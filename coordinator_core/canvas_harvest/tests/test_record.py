"""Tests for canvas_harvest.record: whole-or-nothing record writes under tmp_path with synthetic data."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.canvas_harvest import model, record
from coordinator_core.session import declared_writes

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
ART = "art-one"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _thread(tid, created="2026-10-12T09:00:00Z"):
    return {"id": tid, "comments": [{"created_at": created, "text": f"c-{tid}"}]}


def _lifted(tmp_path, frames=None, threads=None, artifact_id=ART, rendered="rendered text\n"):
    """frames: {path: bytes}; the sources are written beside a fake transcript."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    sources = []
    for i, (path, data) in enumerate((frames if frames is not None else {"index.html": b"<p>a</p>\n"}).items()):
        f = src / f"s{i}.bin"
        f.write_bytes(data)
        sources.append(model.FrameSource(path=path, saved_to=str(f), sha256=_sha(data)))
    threads = threads if threads is not None else [_thread("thr-1"), _thread("thr-2", "2026-10-12T10:00:00+00:00")]
    return model.Lifted(
        artifact_read=model.ArtifactRead(
            url=f"https://claude.ai/artifact/{artifact_id}", artifact_id=artifact_id, title="T", type="design",
            version="v1", frames=tuple(sources), read_at="2026-10-13T10:00:00Z",
        ),
        comments=model.CommentsRead(threads=threads, rendered_text=rendered, read_at="2026-10-13T10:00:01Z"),
        version=None, error=None, transcript_dir=src, session_id=None,
    )


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _refusal(lifted, out, reason):
    with pytest.raises(model.HarvestError) as exc:
        record.write_record(lifted, out, NOW)
    assert exc.value.reason == reason
    return exc.value


def test_full_write_and_field_set(tmp_path):
    out = tmp_path / "out"
    lifted = _lifted(tmp_path, {"index.html": b"<p>a</p>\n", "sub/b.css": b"x{}\n"})
    result = record.write_record(lifted, out, NOW)
    doc = json.loads((out / "harvest.json").read_bytes())
    assert list(doc) == ["schema", "artifact", "harvest"]
    assert doc["schema"] == model.SCHEMA
    assert doc["artifact"] == {"url": f"claude.ai/artifact/{ART}", "id": ART, "title": "T", "type": "design"}
    assert list(doc["harvest"]) == ["harvested_at", "account", "version", "thread_ids", "latest_comment_at", "frames"]
    assert doc["harvest"]["harvested_at"] == "2026-10-13T12:00:00Z"
    assert doc["harvest"]["account"] is None
    assert doc["harvest"]["thread_ids"] == ["thr-1", "thr-2"]
    assert doc["harvest"]["latest_comment_at"] == "2026-10-12T10:00:00+00:00"
    assert doc["harvest"]["frames"] == [
        {"path": "index.html", "sha256": _sha(b"<p>a</p>\n")},
        {"path": "sub/b.css", "sha256": _sha(b"x{}\n")},
    ]
    assert (out / "frames/index.html").read_bytes() == b"<p>a</p>\n"
    assert (out / "frames/sub/b.css").read_bytes() == b"x{}\n"
    assert json.loads((out / "threads/thr-1.json").read_bytes()) == _thread("thr-1")
    assert (out / "threads/thr-1.json").read_bytes() == model.dump_json(_thread("thr-1"))
    assert (out / "threads.rendered.txt").read_bytes() == b"rendered text\n"
    assert not (out / "SETTLED.md").exists()
    assert result.deleted == ()
    assert result.written[-1] == out / "harvest.json"
    assert set(result.written) == {p for p in out.rglob("*") if p.is_file()}


def test_account_is_recorded_in_harvest_json(tmp_path):
    out = tmp_path / "out"
    record.write_record(_lifted(tmp_path), out, NOW, account="a@example.com")
    assert json.loads((out / "harvest.json").read_bytes())["harvest"]["account"] == "a@example.com"


def test_api_harvest_stamps_the_current_login(tmp_path, monkeypatch):
    from coordinator_core.canvas_harvest import api, lift

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    lifted = _lifted(tmp_path)
    monkeypatch.setattr(lift, "lift", lambda *a, **k: lifted)
    out = tmp_path / "out"
    api.harvest(f"https://claude.ai/artifact/{ART}", out, session_id="s", transcript=transcript, now=NOW)
    assert json.loads((out / "harvest.json").read_bytes())["harvest"]["account"] == "me@example.com"


def test_sha_mismatch_and_missing_source(tmp_path):
    out = tmp_path / "out"
    lifted = _lifted(tmp_path)
    bad = model.FrameSource(path="index.html", saved_to=lifted.artifact_read.frames[0].saved_to, sha256="0" * 64)
    mismatch = model.Lifted(**{**lifted.__dict__, "artifact_read": model.ArtifactRead(**{**lifted.artifact_read.__dict__, "frames": (bad,)})})
    _refusal(mismatch, out, model.FRAME_SHA_MISMATCH)
    gone = model.FrameSource(path="index.html", saved_to=str(tmp_path / "nope"), sha256="0" * 64)
    missing = model.Lifted(**{**lifted.__dict__, "artifact_read": model.ArtifactRead(**{**lifted.artifact_read.__dict__, "frames": (gone,)})})
    _refusal(missing, out, model.FRAME_SOURCE_MISSING)
    assert not out.exists()


def test_relative_saved_to_resolves_against_transcript_dir_and_must_stay_inside(tmp_path):
    out = tmp_path / "out"
    lifted = _lifted(tmp_path)
    data = b"<p>a</p>\n"
    rel = model.FrameSource(path="index.html", saved_to="s0.bin", sha256=_sha(data))
    ok = model.Lifted(**{**lifted.__dict__, "artifact_read": model.ArtifactRead(**{**lifted.artifact_read.__dict__, "frames": (rel,)})})
    record.write_record(ok, out, NOW)
    assert (out / "frames/index.html").read_bytes() == data
    esc = model.FrameSource(path="index.html", saved_to="../s0.bin", sha256=_sha(data))
    bad = model.Lifted(**{**lifted.__dict__, "artifact_read": model.ArtifactRead(**{**lifted.artifact_read.__dict__, "frames": (esc,)})})
    _refusal(bad, tmp_path / "out2", model.UNSAFE_PATH)


@pytest.mark.parametrize("path", ["../x.html", "/abs.html", "a/../../x", "a:b.html"])
def test_hostile_frame_paths(tmp_path, path):
    out = tmp_path / "out"
    _refusal(_lifted(tmp_path, {path: b"x"}), out, model.UNSAFE_PATH)
    assert not out.exists()


def test_hostile_thread_id(tmp_path):
    out = tmp_path / "out"
    _refusal(_lifted(tmp_path, threads=[_thread("../evil")]), out, model.UNSAFE_PATH)
    assert not out.exists()


def test_case_folded_frame_paths(tmp_path):
    out = tmp_path / "out"
    _refusal(_lifted(tmp_path, {"A.html": b"1", "a.html": b"2"}), out, model.UNSAFE_PATH)
    assert not out.exists()


def test_zero_threads(tmp_path):
    out = tmp_path / "out"
    result = record.write_record(_lifted(tmp_path, threads=[], rendered=""), out, NOW)
    doc = json.loads((out / "harvest.json").read_bytes())
    assert doc["harvest"]["thread_ids"] == [] and doc["harvest"]["latest_comment_at"] is None
    assert not list((out / "threads").glob("*")) if (out / "threads").exists() else True
    assert out / "threads.rendered.txt" in result.written


def test_reharvest_removes_dropped_files_and_probe_and_keeps_settled(tmp_path):
    out = tmp_path / "out"
    record.write_record(_lifted(tmp_path, {"keep.html": b"k", "drop.html": b"d"}, [_thread("t1"), _thread("t2")]), out, NOW)
    (out / "SETTLED.md").write_text("mine\n")
    doc = json.loads((out / "harvest.json").read_bytes())
    doc["probe"] = {"probed_at": "x"}
    (out / "harvest.json").write_bytes(model.dump_json(doc))
    with declared_writes.collecting() as declared:
        result = record.write_record(_lifted(tmp_path, {"keep.html": b"k2"}, [_thread("t1")]), out, NOW)
    assert not (out / "frames/drop.html").exists() and not (out / "threads/t2.json").exists()
    assert set(result.deleted) == {out / "frames/drop.html", out / "threads/t2.json"}
    assert not (set(map(str, result.deleted)) & set(declared))
    assert "probe" not in json.loads((out / "harvest.json").read_bytes())
    assert (out / "SETTLED.md").read_text() == "mine\n"
    assert (out / "frames/keep.html").read_bytes() == b"k2"


def test_failed_reharvest_leaves_prior_record_whole(tmp_path):
    out = tmp_path / "out"
    record.write_record(_lifted(tmp_path, {"a.html": b"a", "b.html": b"b"}), out, NOW)
    before = _snapshot(out)
    lifted = _lifted(tmp_path, {"a.html": b"a2"})
    fr = lifted.artifact_read.frames[0]
    bad = model.FrameSource(path=fr.path, saved_to=fr.saved_to, sha256="f" * 64)
    broken = model.Lifted(**{**lifted.__dict__, "artifact_read": model.ArtifactRead(**{**lifted.artifact_read.__dict__, "frames": (bad,)})})
    _refusal(broken, out, model.FRAME_SHA_MISMATCH)
    assert _snapshot(out) == before


def test_artifact_mismatch_leaves_prior_record_whole(tmp_path):
    out = tmp_path / "out"
    record.write_record(_lifted(tmp_path, artifact_id="other"), out, NOW)
    before = _snapshot(out)
    _refusal(_lifted(tmp_path, {"zz.html": b"z"}, [], artifact_id=ART), out, model.RECORD_ARTIFACT_MISMATCH)
    assert _snapshot(out) == before


def _declared_equals_written(tmp_path, n_frames, n_threads):
    out = tmp_path / "out"
    frames = {f"project/f{i}.html": f"{i}".encode() for i in range(n_frames)}
    lifted = _lifted(tmp_path, frames, [_thread(f"t{i}") for i in range(n_threads)])
    with declared_writes.collecting() as declared:
        result = record.write_record(lifted, out, NOW)
    assert sorted(declared) == sorted(str(p) for p in result.written)
    return result


def test_every_written_file_is_declared_small(tmp_path):
    _declared_equals_written(tmp_path, 2, 1)


def test_every_written_file_is_declared_over_the_default_touch_bound(tmp_path):
    from coordinator_core import ipc

    result = _declared_equals_written(tmp_path, ipc._MAX_DECLARED_TOUCH_PATHS + 2, 3)
    assert len(result.written) > ipc._MAX_DECLARED_TOUCH_PATHS


def test_record_of_exactly_the_ceiling_is_written_whole(tmp_path):
    n = model.MAX_RECORD_FILES - 3
    result = _declared_equals_written(tmp_path, n - 5, 5)
    assert len(result.written) == model.MAX_RECORD_FILES - 1  # SETTLED.md is the skeleton writer's


def test_record_one_over_is_refused_over_an_empty_directory(tmp_path):
    out = tmp_path / "out"
    err = _refusal(_lifted(tmp_path, {f"project/f{i}": b"x" for i in range(model.MAX_RECORD_FILES - 2)}, []), out, model.RECORD_TOO_LARGE)
    assert err.detail == f"{model.MAX_RECORD_FILES + 1} files, ceiling {model.MAX_RECORD_FILES}"
    assert not out.exists()


def test_runtime_files_do_not_count_toward_the_ceiling(tmp_path):
    out = tmp_path / "out"
    frames = {f"project/f{i}": b"x" for i in range(model.MAX_RECORD_FILES - 5)}
    frames.update({f"artifact-type/r{i}.js": b"y" for i in range(20)})
    record.write_record(_lifted(tmp_path, frames, [_thread("t1"), _thread("t2")]), out, NOW)
    assert (out / "harvest.json").is_file()


def test_record_one_over_leaves_a_prior_record_whole(tmp_path):
    out = tmp_path / "out"
    record.write_record(_lifted(tmp_path, {"a.html": b"a"}), out, NOW)
    before = _snapshot(out)
    _refusal(_lifted(tmp_path, {f"project/f{i}": b"x" for i in range(model.MAX_RECORD_FILES - 2)}, []), out, model.RECORD_TOO_LARGE)
    assert _snapshot(out) == before


def test_ceiling_is_within_the_recorder_ceiling():
    from coordinator_core import ipc

    assert model.MAX_RECORD_FILES <= ipc._DECLARED_TOUCH_BOUND_CEILING
