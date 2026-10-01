"""Replay guard: review-brightline-gate's exit code, verdict and magnitudes match the state a
temp repo planted, for a clean session, a dirty session, a vacuous session and a bogus range."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_brightline_gate
from coordinator_core.tests.gate_verdict.contract import (
    GateExpectation,
    GateObservation,
    VerdictClass,
    disagreement,
)
from coordinator_core.tests.test_review_brightline_gate import (
    _commit_file_with_trailer,
    _init_repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

GATE_VERDICT_CASE = "review-brightline-gate"

_SESSION = "sess-mine"
_PEER = "sess-peer"
_FIELD_RE = re.compile(r"\b(loc|commits|surfaces)=(\d+)")
_VERDICT_RE = re.compile(r"VERDICT=(\S+)")


def _lines(n: int) -> str:
    return "".join(f"v{i} = {i}\n" for i in range(n))


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _observe(capsys, argv: list[str]) -> GateObservation:
    exit_code = review_brightline_gate.main(argv)
    out = capsys.readouterr().out
    verdict = _VERDICT_RE.search(out)
    return GateObservation(
        exit_code=exit_code,
        verdict=verdict.group(1) if verdict else None,
        magnitude={k: int(v) for k, v in _FIELD_RE.findall(out)},
    )


def _assert_agrees(observed: GateObservation, expected: GateExpectation) -> None:
    msg = disagreement(GATE_VERDICT_CASE, observed, expected)
    assert msg is None, msg


def test_known_clean_session_ignores_peer_work(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _head(repo)
    _commit_file_with_trailer(repo, "s1.py", _lines(100), "mine 1", _SESSION)
    _commit_file_with_trailer(repo, "s2.py", _lines(50), "mine 2", _SESSION)
    _commit_file_with_trailer(repo, "peer.py", _lines(2000), "peer", _PEER)
    (repo / "a.py").write_text(_lines(300), encoding="utf-8")
    (repo / "peer_dirty.py").write_text(_lines(300), encoding="utf-8")
    monkeypatch.chdir(repo)

    observed = _observe(capsys, ["--session-id", _SESSION, f"{base}..HEAD"])

    _assert_agrees(
        observed,
        GateExpectation(
            "single-reviewer-ok", VerdictClass.CLEAN, {"loc": 150, "commits": 2}
        ),
    )


def test_known_dirty_session_is_advisory_partition(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _head(repo)
    _commit_file_with_trailer(repo, "d1.py", _lines(300), "mine 1", _SESSION)
    _commit_file_with_trailer(repo, "d2.py", _lines(250), "mine 2", _SESSION)
    monkeypatch.chdir(repo)

    observed = _observe(capsys, ["--session-id", _SESSION, f"{base}..HEAD"])

    _assert_agrees(
        observed,
        GateExpectation(
            "PARTITION-MANDATORY", VerdictClass.ADVISORY, {"loc": 550, "commits": 2}
        ),
    )


def test_zero_match_session_over_clean_tree_is_indeterminate(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _head(repo)
    _commit_file_with_trailer(repo, "peer.py", _lines(40), "peer", _PEER)
    monkeypatch.chdir(repo)

    observed = _observe(capsys, ["--session-id", "sess-ghost", f"{base}..HEAD"])

    _assert_agrees(
        observed,
        GateExpectation(
            "indeterminate", VerdictClass.ADVISORY, {"loc": 0, "commits": 0}
        ),
    )


def test_bogus_range_is_unmeasured(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    observed = _observe(capsys, ["no-such-ref..HEAD"])

    _assert_agrees(observed, GateExpectation(None, VerdictClass.UNMEASURED, {}))
