"""Process-time and spawn budget of the canvas harvest op, measured through the CLI over a 47MB transcript.

Process time and child-process count only, never wall clock. Every measured command runs cold
in a git work tree with a session id set, so the claim recorder is inside the measured window.
Skips only when no coordinator-content-repo checkout resolves.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.canvas_harvest import api, model
from coordinator_core.canvas_harvest.tests.test_fixture_round_trip import (
    CLI_PATH,
    REPO_ROOT,
    fixture_raw_dir,
    shift_timestamps,
    tree_bytes,
)
from coordinator_core.op_census.timing import PROCESS_TIME_BAR_MS
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PAD_BYTES = 47 * 1024 * 1024
SINGLE_PROCESS_LINE_MS = 200.0
# C1 (docs/research/spike-verdicts/2026-10-13-canvas-harvest-op-cost.md): the root interpreter only.
C1_PROCS_PER_CALL = 1.0
PROBE_SHIFT = timedelta(days=3650)
SESSION = "sid-canvas-harvest-budget"
CEILING_ARTIFACT = "SyntheticBudgetCanvas0001"
CEILING_THREADS = 20
K = 4
CEILING_N = 5

_GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@example.com",
)


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _work_tree(path: Path) -> Path:
    path.mkdir()
    _git(["init", "-q", "-b", "main"], path)
    (path / "seed.txt").write_text("seed", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "seed"], path)
    return path


def _measure_env() -> dict:
    env = dict(os.environ)
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        env.pop(name, None)
    env["COORDINATOR_SESSION_ID"] = SESSION
    env["COORDINATOR_ENGINE_ROOT"] = str(REPO_ROOT)
    return env


def _cli_cmd(mode: str, url: str, out: Path, transcript: Path) -> list:
    return [sys.executable, str(CLI_PATH), mode, "--url", url, "--out", str(out), "--transcript", str(transcript)]


def _touched(repo: Path) -> set:
    path = repo / ".git" / "coordinator-sessions" / SESSION / "touch-record.jsonl"
    if not path.is_file():
        return set()
    events = [touch_record.decode_line(line) for line in touch_record.iter_complete_lines(path.read_bytes())]
    return {e.path for e in events if e.verb == touch_record.VERB_TOUCH}


def _assert_every_file_claimed(repo: Path, record_dir: Path) -> None:
    held = {p.relative_to(repo).as_posix() for p in record_dir.rglob("*") if p.is_file()}
    assert held
    unclaimed = held - _touched(repo)
    assert not unclaimed, f"files written but absent from the session's touch record: {sorted(unclaimed)}"


def _write_filler(path: Path) -> None:
    line = (
        json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "x" * 8000}]}})
        + "\n"
    ).encode("utf-8")
    with path.open("wb") as handle:
        for _ in range(PAD_BYTES // len(line) + 1):
            handle.write(line)
    assert path.stat().st_size >= PAD_BYTES


@pytest.fixture(scope="module")
def filler(tmp_path_factory):
    path = tmp_path_factory.mktemp("canvas-harvest-filler") / "filler.jsonl"
    _write_filler(path)
    yield path
    path.unlink()


@pytest.fixture(scope="module")
def raw():
    return fixture_raw_dir()


@pytest.fixture(scope="module")
def fixture_transcript(filler, raw, tmp_path_factory):
    """The 47MB padded transcript: filler, the fixture slice, then the slice again, a decade later."""
    directory = tmp_path_factory.mktemp("canvas-harvest-fixture-transcript")
    path = directory / "transcript.jsonl"
    shutil.copyfile(filler, path)
    shutil.copytree(raw / "frames", directory / "frames")
    slice_path = raw / "transcript-slice.jsonl"
    lines = shift_timestamps(slice_path, timedelta(0)) + shift_timestamps(slice_path, PROBE_SHIFT)
    with path.open("ab") as handle:
        handle.write(("\n".join(lines) + "\n").encode("utf-8"))
    yield path
    path.unlink()


@pytest.fixture(scope="module")
def fixture_artifact(raw):
    read = json.loads((raw / "artifact-read.toolUseResult.json").read_text(encoding="utf-8"))["files_read"]
    return model.artifact_id_from_url(read["url"])


def _synthetic_pairs(directory: Path, frames: int, threads: int, minute: int) -> bytes:
    """One Artifact read pair and one ArtifactComments read pair, shaped as the fixture's, at 2026-10-13T11:<minute>."""
    (directory / "frames").mkdir(exist_ok=True)
    files = []
    for i in range(frames):
        data = f"<p>synthetic frame {i}</p>\n".encode("utf-8")
        name = f"frame-{i:02d}.html"
        (directory / "frames" / name).write_bytes(data)
        files.append(
            {
                "path": name,
                "saved_to": f"frames/{name}",
                "size_bytes": len(data),
                "content_type": "text/html",
                "sha256": hashlib.sha256(data).hexdigest(),
                "source": False,
                "foreign": False,
            }
        )
    files_read = {
        "url": f"https://claude.ai/artifact/{CEILING_ARTIFACT}",
        "title": "Synthetic Budget Canvas",
        "ver": "1799000000-cd34",
        "saved_dir": "frames",
        "files": files,
        "cowritten": False,
        "from_type": False,
        "type": {"url": "", "title": ""},
        "foreign": False,
    }
    comment_threads = [
        {
            "id": f"thread-{i:03d}",
            "created_at": "2026-10-02T09:14:05Z",
            "resolved": False,
            "claude_activated": False,
            "carried": False,
            "comments": [
                {
                    "id": f"comment-{i:03d}",
                    "account": "avery.test@example.invalid",
                    "text": "Synthetic comment.",
                    "created_at": "2026-10-02T09:14:05Z",
                    "sent_to_claude": False,
                    "sent_by_viewer": True,
                    "access": "owner",
                }
            ],
        }
        for i in range(threads)
    ]

    def stamp(second: int) -> str:
        return f"2026-10-13T11:{minute:02d}:{second:02d}Z"

    def call(uuid: str, second: int, tool_id: str, name: str, tool_input: dict) -> dict:
        return {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": stamp(second),
            "sessionId": SESSION,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]},
        }

    def result(uuid: str, second: int, tool_id: str, payload: dict) -> dict:
        return {
            "type": "user",
            "uuid": uuid,
            "timestamp": stamp(second),
            "sessionId": SESSION,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "synthetic"}]},
            "toolUseResult": payload,
        }

    tag = f"{minute:02d}"
    records = [
        call(f"c0000001-0000-4000-8000-0000000000{tag}", 0, f"toolu_budget_read_{tag}", "Artifact",
             {"action": "read", "paths": [f["path"] for f in files]}),
        result(f"c0000002-0000-4000-8000-0000000000{tag}", 1, f"toolu_budget_read_{tag}", {"files_read": files_read}),
        call(f"c0000003-0000-4000-8000-0000000000{tag}", 5, f"toolu_budget_comments_{tag}", "ArtifactComments",
             {"action": "read", "artifact_id": CEILING_ARTIFACT}),
        result(f"c0000004-0000-4000-8000-0000000000{tag}", 6, f"toolu_budget_comments_{tag}",
               {"foreign": False, "threads": comment_threads}),
    ]
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


def _ceiling_frames() -> int:
    return model.MAX_RECORD_FILES - 3 - CEILING_THREADS


@pytest.fixture
def ceiling_transcript(filler, tmp_path):
    """Filler plus a read pair whose record is exactly model.MAX_RECORD_FILES files."""
    assert _ceiling_frames() >= 1
    directory = tmp_path / "ceiling-transcript"
    directory.mkdir()
    path = directory / "transcript.jsonl"
    shutil.copyfile(filler, path)
    with path.open("ab") as handle:
        handle.write(_synthetic_pairs(directory, _ceiling_frames(), CEILING_THREADS, minute=0))
    yield path
    path.unlink()


def test_harvest_cli_is_under_the_bar_with_the_claim_recorder_running(fixture_transcript, fixture_artifact, tmp_path):
    repo = _work_tree(tmp_path / "repo")
    out = repo / "record"
    cmd = _cli_cmd("harvest", fixture_artifact, out, fixture_transcript)

    measured = batched_process_time_ms(cmd, k=K, env=_measure_env(), cwd=str(repo))

    assert measured["rc"] == 0, measured
    record = json.loads((out / "harvest.json").read_text(encoding="utf-8"))
    assert record["artifact"]["id"] == fixture_artifact and record["harvest"]["thread_ids"]
    _assert_every_file_claimed(repo, out)
    print(f"harvest {measured['process_time_ms']:.1f}ms procs/call {measured['procs_per_call']}")
    assert measured["process_time_ms"] > 0
    assert measured["process_time_ms"] < PROCESS_TIME_BAR_MS
    assert measured["procs_per_call"] == C1_PROCS_PER_CALL


def test_probe_cli_is_under_the_bar_and_stamps_a_probe(fixture_transcript, fixture_artifact, tmp_path):
    repo = _work_tree(tmp_path / "repo")
    out = repo / "record"
    env = _measure_env()
    first = subprocess.run(
        _cli_cmd("harvest", fixture_artifact, out, fixture_transcript),
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert first.returncode == 0, first.stderr
    assert "probe" not in json.loads((out / "harvest.json").read_text(encoding="utf-8"))
    cmd = _cli_cmd("probe", fixture_artifact, out, fixture_transcript)

    measured = batched_process_time_ms(cmd, k=K, env=env, cwd=str(repo))

    assert measured["rc"] == 0, measured
    probe = json.loads((out / "harvest.json").read_text(encoding="utf-8")).get("probe")
    assert isinstance(probe, dict) and probe["readable"] is True
    print(f"probe {measured['process_time_ms']:.1f}ms procs/call {measured['procs_per_call']}")
    assert measured["process_time_ms"] > 0
    assert measured["process_time_ms"] < PROCESS_TIME_BAR_MS
    assert measured["procs_per_call"] == C1_PROCS_PER_CALL


def test_api_harvest_spawns_no_process(fixture_transcript, fixture_artifact, tmp_path, monkeypatch):
    repo = _work_tree(tmp_path / "repo")
    with _count_spawns_attributed(monkeypatch) as spawns:
        api.harvest(fixture_artifact, repo / "record", session_id=SESSION, transcript=fixture_transcript)

    assert (repo / "record" / "harvest.json").is_file()
    assert [s.argv for s in spawns] == []


def test_record_at_the_ceiling_is_under_the_single_process_line_and_fully_claimed(ceiling_transcript, tmp_path):
    repo = _work_tree(tmp_path / "repo")
    out = repo / "record"
    cmd = _cli_cmd("harvest", CEILING_ARTIFACT, out, ceiling_transcript)
    env = _measure_env()

    batches = [batched_process_time_ms(cmd, k=K, env=env, cwd=str(repo)) for _ in range(CEILING_N)]

    assert [b["rc"] for b in batches] == [0] * CEILING_N, batches
    held = [p for p in out.rglob("*") if p.is_file()]
    assert len(held) == model.MAX_RECORD_FILES
    p50 = statistics.median(b["process_time_ms"] for b in batches)
    print(f"ceiling record {len(held)} files p50 {p50:.1f}ms samples {[round(b['process_time_ms'], 1) for b in batches]}")
    assert p50 > 0
    assert p50 < PROCESS_TIME_BAR_MS
    assert p50 < SINGLE_PROCESS_LINE_MS
    assert [b["procs_per_call"] for b in batches] == [C1_PROCS_PER_CALL] * CEILING_N
    _assert_every_file_claimed(repo, out)


def test_record_one_thread_over_the_ceiling_is_refused_and_leaves_the_record(ceiling_transcript, tmp_path):
    repo = _work_tree(tmp_path / "repo")
    out = repo / "record"
    env = _measure_env()
    cmd = _cli_cmd("harvest", CEILING_ARTIFACT, out, ceiling_transcript)
    first = subprocess.run(
        cmd, cwd=str(repo), env=env, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert first.returncode == 0, first.stderr
    before = tree_bytes(out)
    assert len(before) == model.MAX_RECORD_FILES

    with ceiling_transcript.open("ab") as handle:
        handle.write(_synthetic_pairs(ceiling_transcript.parent, _ceiling_frames(), CEILING_THREADS + 1, minute=1))
    refused = subprocess.run(
        cmd, cwd=str(repo), env=env, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert refused.returncode == 1
    assert refused.stdout == ""
    lines = refused.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("canvas-harvest: record-too-large:"), refused.stderr
    assert tree_bytes(out) == before
