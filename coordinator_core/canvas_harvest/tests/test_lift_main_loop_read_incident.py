"""Incident: a main-loop `Artifact` action:read plus an empty `ArtifactComments` read must lift a version and comments."""

import json

import pytest

from coordinator_core.canvas_harvest import lift as lift_mod
from coordinator_core.canvas_harvest import model

URL = "https://claude.ai/artifact/QiKJeVwbA9aE4sWUPVgY6x"
AID = model.artifact_id_from_url(URL)


def _transcript(tmp_path, *, version_read=True, comments_read=True):
    rows = []

    def call(tid, name, inp, result, text, ts):
        rows.append({"timestamp": ts, "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        rows.append({
            "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
            "toolUseResult": result,
        })

    if version_read:
        call(
            "t1", "Artifact", {"action": "read", "url": URL, "prompt": "list frames"},
            {
                "read": {"code": 200, "url": URL, "title": "T"},
                "artifactRead": {"slug": "c006f6d2-346d-4532-bc32-36540689938d", "ver": "1792714957-e4db"},
                "owned": True,
            },
            "page text", "2026-01-01T10:00:00Z",
        )
    if comments_read:
        call(
            "t2", "ArtifactComments", {"action": "read", "url": URL},
            {"foreign": True, "threads": []}, "No comment threads on this artifact yet.", "2026-01-01T10:01:00Z",
        )
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_artifact_read_version_and_empty_comments_lift(tmp_path):
    lifted = lift_mod.lift([_transcript(tmp_path)], AID, require_frames=False)
    assert lifted.version is not None and lifted.version.version == "1792714957-e4db"
    assert lifted.comments is not None and list(lifted.comments.threads) == []


def test_missing_comments_read_leaves_comments_none(tmp_path):
    lifted = lift_mod.lift([_transcript(tmp_path, comments_read=False)], AID, require_frames=False)
    assert lifted.version is not None and lifted.comments is None


def test_missing_version_read_leaves_version_none(tmp_path):
    lifted = lift_mod.lift([_transcript(tmp_path, version_read=False)], AID, require_frames=False)
    assert lifted.version is None and lifted.comments is not None


def test_no_reads_still_refuses(tmp_path):
    with pytest.raises(model.HarvestError):
        lift_mod.lift([_transcript(tmp_path, version_read=False, comments_read=False)], AID, require_frames=False)
