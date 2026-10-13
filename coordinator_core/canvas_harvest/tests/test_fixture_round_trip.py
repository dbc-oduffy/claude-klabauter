"""Seam test: the canvas harvest op over DoE's fixture canvas finds every frame and every thread.

Every oracle is derived from the fixture's own raw tool results, never a literal count or id.
Runs in-process (api for the record, the CLI module with the registered handler for refusals);
all output lands under tmp_path. Skips only when no coordinator-content-repo checkout resolves.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.canvas_harvest import api, model
from coordinator_core.conftest import _REAL_CONTENT_ROOT

FIXTURE_SUBPATH = "coordinator/tests/fixtures/canvas-harvest/raw"
REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = REPO_ROOT / "coordinator" / "bin" / "canvas-harvest.py"

HARVESTED_AT = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
PROBED_AT = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
PROBE_SHIFT = timedelta(days=1)
SESSION = "sid-fixture-round-trip"

_SESSION_ENV = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def fixture_raw_dir() -> Path:
    """DoE's fixture `raw/` directory; skips with its reason when no checkout resolves, fails when one lacks it."""
    # conftest quarantines resolve_content_root() to a stub under pytest; its collection-time
    # capture of the same resolver is the real checkout.
    root = _REAL_CONTENT_ROOT
    if not root:
        pytest.skip("no coordinator-content-repo checkout resolves (resolve_content_root() is empty)")
    raw = Path(root) / FIXTURE_SUBPATH
    assert raw.is_dir(), f"a coordinator-content-repo checkout resolves at {root} but holds no fixture at {raw}"
    return raw


def shift_timestamps(path: Path, delta: timedelta) -> list:
    """The JSONL lines of `path` with every record `timestamp` moved by `delta`."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        entry = json.loads(raw)
        moment = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")) + delta
        entry["timestamp"] = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(json.dumps(entry, ensure_ascii=False))
    return lines


def load_cli():
    spec = importlib.util.spec_from_file_location("canvas_harvest_cli_under_test", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_bytes(root: Path) -> dict:
    """Every file under `root` by relative posix path to its bytes."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch):
    for name in _SESSION_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def raw():
    return fixture_raw_dir()


@pytest.fixture
def oracle(raw):
    read = json.loads((raw / "artifact-read.toolUseResult.json").read_text(encoding="utf-8"))["files_read"]
    comments = json.loads((raw / "comments-read.toolUseResult.json").read_text(encoding="utf-8"))
    return {
        "artifact_id": model.artifact_id_from_url(read["url"]),
        "read": read,
        "threads": comments["threads"],
        "slice": raw / "transcript-slice.jsonl",
    }


@pytest.fixture
def cli(monkeypatch, tmp_path):
    from coordinator_core.ops import canvas_harvest as op_module

    module = load_cli()
    monkeypatch.chdir(tmp_path)

    def run(mode, url, out, transcript, capsys):
        argv = [mode, "--url", url, "--out", str(out), "--transcript", str(transcript), "--session-id", SESSION]
        code = module.main(argv, op=op_module._handler)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def _harvest(oracle, out: Path):
    return api.harvest(
        oracle["artifact_id"], out, session_id=SESSION, transcript=oracle["slice"], now=HARVESTED_AT
    )


def _probe_transcript(oracle, tmp_path: Path) -> Path:
    path = tmp_path / "probe-transcript" / "transcript.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(shift_timestamps(oracle["slice"], PROBE_SHIFT)) + "\n", encoding="utf-8")
    return path


def test_harvest_writes_every_frame_and_thread(oracle, raw, tmp_path):
    out = tmp_path / "record"
    _harvest(oracle, out)

    record = json.loads((out / "harvest.json").read_text(encoding="utf-8"))
    read = oracle["read"]
    assert set(record) == {"schema", "artifact", "harvest"}
    assert record["schema"] == model.SCHEMA
    assert record["artifact"] == {
        "url": f"claude.ai/artifact/{oracle['artifact_id']}",
        "id": oracle["artifact_id"],
        "title": read["title"],
        "type": read["type"]["title"],
    }
    thread_ids = [t["id"] for t in oracle["threads"]]
    assert thread_ids, "the fixture holds no threads: the thread oracle would be vacuous"
    stamps = [c["created_at"] for t in oracle["threads"] for c in t["comments"]]
    assert record["harvest"] == {
        "harvested_at": "2026-10-13T12:00:00Z",
        "version": read["ver"],
        "thread_ids": thread_ids,
        "latest_comment_at": max(stamps),
        "frames": [{"path": f["path"], "sha256": f["sha256"]} for f in read["files"]],
    }

    fixture_frames = {p.relative_to(raw / "frames").as_posix(): p for p in (raw / "frames").rglob("*") if p.is_file()}
    assert fixture_frames, "the fixture holds no frames: the frame oracle would be vacuous"
    assert set(fixture_frames) == {f["path"] for f in read["files"]}
    written_frames = {p.relative_to(out / "frames").as_posix(): p for p in (out / "frames").rglob("*") if p.is_file()}
    assert set(written_frames) == set(fixture_frames)
    listed = {f["path"]: f["sha256"] for f in record["harvest"]["frames"]}
    for rel, source in fixture_frames.items():
        data = source.read_bytes()
        assert written_frames[rel].read_bytes() == data
        assert hashlib.sha256(data).hexdigest() == listed[rel]

    written_threads = {p.name: p for p in (out / "threads").iterdir()}
    assert set(written_threads) == {f"{tid}.json" for tid in thread_ids}
    for thread in oracle["threads"]:
        assert json.loads(written_threads[f"{thread['id']}.json"].read_text(encoding="utf-8")) == thread

    settled = (out / "SETTLED.md").read_text(encoding="utf-8").splitlines()
    assert settled[0] == "## Thread-settled behaviour"
    assert not [line for line in settled if line.lstrip().startswith(("- ", "* "))]


def test_harvest_result_names_only_written_paths(oracle, tmp_path):
    out = tmp_path / "record"
    result = _harvest(oracle, out)

    assert all(isinstance(p, Path) for p in result.written), result.written
    on_disk = {p.resolve() for p in out.rglob("*") if p.is_file()}
    assert {Path(p).resolve() for p in result.written} == on_disk
    assert result.deleted == ()


def test_probe_changes_only_the_probe_key(oracle, cli, tmp_path, capsys):
    out = tmp_path / "record"
    _harvest(oracle, out)
    before_files = tree_bytes(out)
    before = json.loads(before_files["harvest.json"])
    assert "probe" not in before

    code, stdout, stderr = cli("probe", oracle["artifact_id"], out, _probe_transcript(oracle, tmp_path), capsys)
    assert (code, stderr) == (0, ""), stderr
    assert Path(stdout.strip().splitlines()[0]).name == "harvest.json"

    after_files = tree_bytes(out)
    after = json.loads(after_files["harvest.json"])
    probe = after.pop("probe")
    assert after == before
    assert probe["readable"] is True
    assert probe["unreadable_reason"] is None
    assert probe["session_id"] == SESSION
    assert probe["version"] == oracle["read"]["ver"]
    assert probe["thread_ids"] == [t["id"] for t in oracle["threads"]]
    assert set(after_files) == set(before_files)
    for rel, data in before_files.items():
        if rel != "harvest.json":
            assert after_files[rel] == data, rel


def test_probe_over_reads_not_newer_than_the_harvest_is_refused(oracle, cli, tmp_path, capsys):
    out = tmp_path / "record"
    _harvest(oracle, out)
    before = tree_bytes(out)

    code, stdout, stderr = cli("probe", oracle["artifact_id"], out, oracle["slice"], capsys)

    assert code == 1
    assert stdout == ""
    lines = stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("canvas-harvest: probe-reads-stale:"), stderr
    assert tree_bytes(out) == before


def _unreadable_slice(raw: Path) -> tuple:
    path = raw / "unreadable" / "transcript-slice.jsonl"
    text = path.read_text(encoding="utf-8")
    ids = {
        entry["message"]["content"][0]["input"]["artifact_id"]
        for entry in map(json.loads, text.splitlines())
        if entry["type"] == "assistant"
    }
    assert len(ids) == 1
    return path, ids.pop(), text


@pytest.mark.parametrize("mode", ["harvest", "probe"])
def test_unreadable_canvas_over_its_record_marks_probe_unreadable(oracle, raw, cli, tmp_path, capsys, mode):
    out = tmp_path / "record"
    _harvest(oracle, out)
    before_files = tree_bytes(out)
    before = json.loads(before_files["harvest.json"])
    _, own_id, text = _unreadable_slice(raw)
    rewritten = tmp_path / "unreadable-rewritten" / "transcript.jsonl"
    rewritten.parent.mkdir()
    rewritten.write_text(text.replace(own_id, oracle["artifact_id"]), encoding="utf-8")

    code, stdout, stderr = cli(mode, oracle["artifact_id"], out, rewritten, capsys)

    assert code == 1
    assert stdout == ""
    lines = stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("canvas-harvest: canvas-unreadable: not-found"), stderr
    after_files = tree_bytes(out)
    after = json.loads(after_files["harvest.json"])
    probe = after.pop("probe")
    assert after == before
    assert probe["readable"] is False
    assert probe["unreadable_reason"] == "not-found"
    assert probe["version"] == ""
    assert probe["thread_ids"] == []
    assert probe["latest_comment_at"] is None
    for rel, data in before_files.items():
        if rel != "harvest.json":
            assert after_files[rel] == data, rel


@pytest.mark.parametrize("mode", ["harvest", "probe"])
def test_unreadable_canvas_over_an_empty_directory_writes_nothing(raw, cli, tmp_path, capsys, mode):
    path, own_id, _ = _unreadable_slice(raw)
    out = tmp_path / "empty"
    out.mkdir()

    code, stdout, stderr = cli(mode, own_id, out, path, capsys)

    assert code == 1
    assert stdout == ""
    lines = stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("canvas-harvest: canvas-unreadable: not-found"), stderr
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("mode", ["harvest", "probe"])
def test_unreadable_canvas_of_another_artifact_is_a_mismatch(oracle, raw, cli, tmp_path, capsys, mode):
    out = tmp_path / "record"
    _harvest(oracle, out)
    before = tree_bytes(out)
    path, own_id, _ = _unreadable_slice(raw)
    assert own_id != oracle["artifact_id"]

    code, stdout, stderr = cli(mode, own_id, out, path, capsys)

    assert code == 1
    assert stdout == ""
    lines = stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("canvas-harvest: record-artifact-mismatch:"), stderr
    assert tree_bytes(out) == before
