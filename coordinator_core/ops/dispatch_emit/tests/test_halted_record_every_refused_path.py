"""A landing refusal's halted.json records every refused path with its measured and landing blobs."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.git.commit import checkin_blob_sha
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import (
    HALT_REFUSED_PATHS_FIELD, HALTED_FILE, refused_path_entries, write_halt_marker,
)

RUN = "warp-2026-11-21-x-20261121T000000-aaaaaa"
SIZING = "state/sizings/2026-11-21-x.yaml"
PATHS = ["a/gnm_register.py", "b/synthesis.md", "c/third.txt"]


def _staged(tmp_path: Path) -> tuple:
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    for i, rel in enumerate(PATHS):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(f"landing {i}\n".encode())
    measured = {rel: f"{i:040x}" for i, rel in enumerate(PATHS)}
    (rd / "measured-rev.json").write_text(json.dumps({"blobs": measured}), encoding="utf-8")
    return rd, measured


def test_every_refused_path_is_recorded_with_both_blobs(tmp_path):
    rd, measured = _staged(tmp_path)
    prose = "landing refused: " + " | ".join(f"{p}: measured {measured[p]}" for p in PATHS)
    marker = write_halt_marker(
        rd, halted="commit", sizing_rel=SIZING, reason=prose[:60], refused_paths=list(PATHS),
    )
    data = json.loads(marker.read_text(encoding="utf-8"))
    entries = data[HALT_REFUSED_PATHS_FIELD]
    assert [e["path"] for e in entries] == PATHS
    for e in entries:
        assert e["measured"] == measured[e["path"]]
        assert e["landing"] == checkin_blob_sha(tmp_path, e["path"])
        assert e["landing"] and e["landing"] != e["measured"]
    assert len(data["reason"]) <= 60 < len(prose)


def test_caller_supplied_blobs_win_and_duplicates_collapse(tmp_path):
    rd, _ = _staged(tmp_path)
    given = [{"path": PATHS[0], "measured": "m" * 40, "landing": "l" * 40}, PATHS[0], PATHS[1]]
    entries = refused_path_entries(rd, given)
    assert entries[0] == {"path": PATHS[0], "measured": "m" * 40, "landing": "l" * 40}
    assert [e["path"] for e in entries] == [PATHS[0], PATHS[1]]


def test_unknown_blobs_are_none_not_a_dropped_path(tmp_path):
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    entries = refused_path_entries(rd, ["gone/file.txt"])
    assert entries == [{"path": "gone/file.txt", "measured": None, "landing": None}]


def test_marker_without_refused_paths_is_unchanged(tmp_path):
    rd, _ = _staged(tmp_path)
    marker = write_halt_marker(rd, halted="commit", sizing_rel=SIZING, refused_paths=[])
    assert HALT_REFUSED_PATHS_FIELD not in json.loads((rd / HALTED_FILE).read_text(encoding="utf-8"))
    assert marker == rd / HALTED_FILE


def test_commit_stage_halt_keeps_every_refused_path_beyond_the_reason_cap(tmp_path):
    from coordinator_core.ops.dispatch_emit import ask_contract
    from coordinator_core.ops.dispatch_emit.commit_stage_halt import halt_run_on_commit_stage_end

    rd, measured = _staged(tmp_path)
    reply = {
        "committed": False, "refused": "landing-tree-differs-from-measured",
        "error": "landing refused: " + "x" * 900,
        "refused_paths": [{"path": p, "measured": measured[p], "landing": "l" * 40} for p in PATHS],
    }
    run_dir = tmp_path / "runs" / "wf_abc"
    run_dir.mkdir(parents=True)
    journal = run_dir / "journal.jsonl"
    journal.write_text("".join(json.dumps(e) + "\n" for e in [
        {"type": "started", "agentId": "a1", "label": "commit", "key": "k1"},
        {"type": "result", "agentId": "a1", "key": "k1", "result": reply},
    ]), encoding="utf-8")

    assert halt_run_on_commit_stage_end(
        rd, str(journal), terminal_status="failed", sizing_rel=SIZING, launched_at=1.0,
    ) is not None
    data = json.loads((rd / HALTED_FILE).read_text(encoding="utf-8"))
    assert len(data["reason"]) <= ask_contract.HALT_REASON_MAX_CHARS < len(reply["error"])
    assert [(e["path"], e["measured"], e["landing"]) for e in data[HALT_REFUSED_PATHS_FIELD]] == [
        (p, measured[p], "l" * 40) for p in PATHS
    ]


def test_halted_record_commit_records_every_refused_path(tmp_path):
    import subprocess

    from coordinator_core.ops.dispatch_emit.halted_record_commit import commit_halted_record
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
    from coordinator_core.win_portability import no_console_creationflags

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=True, **no_console_creationflags(),
        ).stdout

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    rd, measured = _staged(tmp_path)
    (tmp_path / "run.mjs").write_text("// script\n", encoding="utf-8")
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    halted = {
        "halted": "commit", "reason": "landing refused",
        "refused_paths": [{"path": p, "measured": measured[p], "landing": "l" * 40} for p in PATHS],
    }

    out = commit_halted_record(
        tmp_path, tmp_path / ".git", {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted},
    )

    assert out["committed"] is True, out
    data = json.loads(git("show", f"HEAD:{RECORD_DIR_ROOT}/{RUN}/{HALTED_FILE}"))
    assert [e["path"] for e in data[HALT_REFUSED_PATHS_FIELD]] == PATHS
    assert all(e["landing"] == "l" * 40 for e in data[HALT_REFUSED_PATHS_FIELD])
