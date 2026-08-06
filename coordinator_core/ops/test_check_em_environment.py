"""
coordinator_core.ops.test_check_em_environment — behavior-parity tests for the
naked-Python port of the example-doctrine-repo-owned check-em-environment.sh bash script
(Port of: check-em-environment.sh, example-doctrine-repo 894d4bc6, 2026-07-22).

Mirrors the oracle's own suite (Port of: test-check-em-environment.sh, example-doctrine-repo
894d4bc6, 2026-07-22):
clean-env silence, effort-axis warn, model-axis warn, both-axes warn, and the
always-exit-0 invariant. HOME / CLAUDE_PROJECT_DIR point at tmp_path fixtures
so effort resolution never touches real machine settings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.check_em_environment import main


def _mk_home(tmp_path: Path, name: str, effort: str | None) -> Path:
    d = tmp_path / name
    (d / ".claude").mkdir(parents=True)
    settings = {} if effort is None else {"effortLevel": effort}
    (d / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return d


def _mk_transcript(tmp_path: Path, name: str, model: str) -> Path:
    p = tmp_path / name
    # No space after the colons — matches the bash oracle's compact fixture
    # format and the "model":"..." regex both the bash and Python ports match.
    p.write_text('{"message":{"model":"%s"}}\n' % model, encoding="utf-8")
    return p


@pytest.fixture
def homes(tmp_path):
    return {
        "medium": _mk_home(tmp_path, "home_medium", "medium"),
        "high": _mk_home(tmp_path, "home_high", "high"),
        "none": _mk_home(tmp_path, "home_none", None),
    }


@pytest.fixture
def transcripts(tmp_path):
    return {
        "opus": _mk_transcript(tmp_path, "opus.jsonl", "claude-opus-4-8"),
        "sonnet": _mk_transcript(tmp_path, "sonnet.jsonl", "claude-sonnet-4-6"),
    }


def _run(capsys, monkeypatch, home: Path, transcript: Path | None):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home))
    argv = ["--transcript", str(transcript)] if transcript is not None else []
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, out


def _classify(out: str) -> str:
    has_model = "MODEL:" in out
    has_effort = "EFFORT:" in out
    if has_model and has_effort:
        return "both"
    if has_model:
        return "model"
    if has_effort:
        return "effort"
    return "silent"


def test_opus_and_medium_is_silent(capsys, monkeypatch, homes, transcripts):
    rc, out = _run(capsys, monkeypatch, homes["medium"], transcripts["opus"])
    assert rc == 0
    assert _classify(out) == "silent"


def test_no_transcript_and_medium_is_silent(capsys, monkeypatch, homes):
    rc, out = _run(capsys, monkeypatch, homes["medium"], None)
    assert rc == 0
    assert _classify(out) == "silent"


def test_opus_and_high_effort_warns_effort(capsys, monkeypatch, homes, transcripts):
    rc, out = _run(capsys, monkeypatch, homes["high"], transcripts["opus"])
    assert rc == 0
    assert _classify(out) == "effort"


def test_opus_and_unpinned_effort_warns_effort(capsys, monkeypatch, homes, transcripts):
    rc, out = _run(capsys, monkeypatch, homes["none"], transcripts["opus"])
    assert rc == 0
    assert _classify(out) == "effort"


def test_sonnet_and_medium_warns_model(capsys, monkeypatch, homes, transcripts):
    rc, out = _run(capsys, monkeypatch, homes["medium"], transcripts["sonnet"])
    assert rc == 0
    assert _classify(out) == "model"


def test_sonnet_and_high_warns_both(capsys, monkeypatch, homes, transcripts):
    rc, out = _run(capsys, monkeypatch, homes["high"], transcripts["sonnet"])
    assert rc == 0
    assert _classify(out) == "both"


def test_always_exits_zero_on_drift(capsys, monkeypatch, homes, transcripts):
    rc, _out = _run(capsys, monkeypatch, homes["high"], transcripts["sonnet"])
    assert rc == 0
