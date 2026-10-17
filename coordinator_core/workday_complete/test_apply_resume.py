"""A killed workday-complete apply, rerun over the same directive list, skips every directive that landed.

The kill is a test-local BaseException raised from inside directive k's dispatch; it passes the
loop's `except Exception` arms the way a process kill passes them. Each pass opens a fresh
checkpoint the way a new process would.

Run: python -m pytest coordinator_core/workday_complete/test_apply_resume.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.workday_complete import apply as wc_apply

KILL_AT = 3


class _Kill(BaseException):
    """Stands in for the process kill: not an Exception, so no per-directive arm swallows it."""


def _directive(did: str, *, stdin_from: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": did,
        "cli": "fake-cli",
        "args": [],
        "depends_on": None,
        "already_satisfied": False,
        "stdin_from": stdin_from,
    }


def _six() -> list[dict[str, Any]]:
    return [_directive(f"d{i}") for i in range(6)]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    sessions = str(tmp_path / ".git" / "coordinator-sessions")
    from coordinator_core.ceremony_common import apply_checkpoint
    from coordinator_core.session import core

    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: sessions)
    monkeypatch.setattr(apply_checkpoint, "sessions_dir", lambda cwd=None: sessions)
    monkeypatch.setattr(wc_apply, "_resolve_cli", lambda name: Path(name))
    return tmp_path


class _Pass:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        self.monkeypatch = monkeypatch
        self.root = root
        self.dispatched: list[str] = []
        self.stdin_seen: dict[str, Optional[str]] = {}
        self.exit_codes: dict[str, int] = {}

    def run(self, directives: list[dict[str, Any]], kill_at: Optional[str] = None):
        def fake(directive: dict[str, Any], *, stdin_text: Optional[str] = None) -> dict[str, Any]:
            did = directive["id"]
            self.dispatched.append(did)
            self.stdin_seen[did] = stdin_text
            if did == kill_at:
                raise _Kill()
            code = self.exit_codes.get(did, 0)
            return {
                "id": did, "cli": directive["cli"], "args": [], "exit_code": code,
                "stdout": f"out-{did}\n", "stderr": "", "exit_class": "returned",
            }

        self.monkeypatch.setattr(wc_apply, "_dispatch_directive", fake)
        from coordinator_core.ceremony_common.apply_checkpoint import open_checkpoint

        checkpoint = open_checkpoint(self.root, "workday-complete")
        return wc_apply._execute_directives(directives, [], {}, checkpoint=checkpoint)


@pytest.fixture
def run_pass(monkeypatch: pytest.MonkeyPatch, root: Path) -> _Pass:
    return _Pass(monkeypatch, root)


def test_rerun_after_a_kill_dispatches_each_landed_directive_once(run_pass: _Pass) -> None:
    directives = _six()
    with pytest.raises(_Kill):
        run_pass.run(directives, kill_at=f"d{KILL_AT}")
    exit_code, report = run_pass.run(directives)

    counts = {d["id"]: run_pass.dispatched.count(d["id"]) for d in directives}
    assert {i: counts[f"d{i}"] for i in range(KILL_AT)} == {0: 1, 1: 1, 2: 1}
    assert counts[f"d{KILL_AT}"] == 2
    assert {counts["d4"], counts["d5"]} == {1}
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)
    assert report["interrupted"] == [f"d{KILL_AT}"]
    assert report["resumed"] == ["d0", "d1", "d2"]
    assert report["landed"] == [d["id"] for d in directives]


def test_a_resumed_stdin_consumer_is_fed_its_producer_stdout(run_pass: _Pass) -> None:
    directives = [_directive("p"), _directive("c", stdin_from="p"), _directive("t")]
    with pytest.raises(_Kill):
        run_pass.run(directives, kill_at="c")
    exit_code, report = run_pass.run(directives)

    assert run_pass.dispatched.count("p") == 1
    assert run_pass.stdin_seen["c"] == "out-p\n"
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)
    assert "p" in report["resumed"]


def test_a_failed_directive_is_not_a_landed_row_and_dispatches_again(run_pass: _Pass) -> None:
    directives = _six()
    run_pass.exit_codes["d2"] = 1
    first_code, first = run_pass.run(directives)
    assert first_code == int(wc_apply.WorkdayApplyExitCode.PARTIAL_MUTATION)
    assert [f["id"] for f in first["failed"]] == ["d2"]

    run_pass.exit_codes.clear()
    run_pass.run(directives)
    counts = {d["id"]: run_pass.dispatched.count(d["id"]) for d in directives}
    assert counts["d2"] == 2
    assert {counts[i] for i in ("d0", "d1", "d3", "d4", "d5")} == {1}
