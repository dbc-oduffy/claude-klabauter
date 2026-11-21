"""peer_hunk_gate holds a path any unresolved ledger row names when the path carries engine-measured foreign hunks."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests.test_peer_hunk_gate import (  # noqa: F401
    OWN, PATH, SELF, _declare, _record, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "".join(f"line {i}\n" for i in range(1, 21))
OWN_EDIT = BASE.replace("line 3\n", "line 3 own\n")
PEER_EDIT = OWN_EDIT.replace("line 18\n", "line 18 peer\n")


def _git(repo, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _commit_base_and_record_baseline(repo) -> None:
    target = repo / PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(BASE.encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    blob = _git(repo, "hash-object", "-w", PATH)
    manifest = _declare(repo, OWN, PATH) / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**data, "write_baseline": {PATH: blob}}), encoding="utf-8")


def _record_measured(repo, text: str) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    _write(repo, text)
    blob = _git(repo, "hash-object", "-w", PATH)
    record = measured_rev_record_path(repo, OWN)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"blobs": {PATH: blob}}), encoding="utf-8")


def _row(repo, severity: str = "major", status=None, file=PATH) -> None:
    _record(repo, OWN, "rev-1")
    row = {"id": "f1", "file": file, "summary": "carries foreign stat-cache hunks", "severity": severity}
    if status:
        row["status"] = status
    (repo / ".coordinator-local" / "subagent-share" / SELF / "rev-1.md").write_text(
        f"---\nagent_type: reviewer\n---\n\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n", encoding="utf-8"
    )


def _write(repo, text: str) -> None:
    (repo / PATH).write_bytes(text.encode())


@pytest.mark.parametrize("severity", ["major", "P1", "minor", "nit"])
def test_a_non_critical_row_on_a_path_with_a_peer_hunk_is_held(repo, severity) -> None:
    _commit_base_and_record_baseline(repo)
    _record_measured(repo, OWN_EDIT)
    _write(repo, PEER_EDIT)
    _row(repo, severity)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == [(PATH, "rev-1", "carries foreign stat-cache hunks")]


def test_the_same_row_on_an_own_hunks_only_path_holds_nothing(repo) -> None:
    _commit_base_and_record_baseline(repo)
    _record_measured(repo, OWN_EDIT)
    _row(repo)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_peer_dirt_that_predates_the_run_is_foreign(repo) -> None:
    _commit_base_and_record_baseline(repo)
    manifest = repo / "state" / "warp" / OWN / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    _write(repo, PEER_EDIT.replace("line 3 own\n", "line 3\n"))
    data["write_baseline"][PATH] = _git(repo, "hash-object", "-w", PATH)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    _row(repo)
    assert [h[0] for h in peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH])] == [PATH]


def test_an_unmodified_path_holds_nothing(repo) -> None:
    _commit_base_and_record_baseline(repo)
    _row(repo)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_a_path_with_no_recorded_baseline_is_not_measured_and_holds_nothing(repo) -> None:
    _commit_base_and_record_baseline(repo)
    (repo / "state" / "warp" / OWN / "manifest.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    _write(repo, PEER_EDIT)
    _row(repo)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


@pytest.mark.parametrize("status", ["applied", "em-rejected"])
def test_a_resolved_row_on_a_peer_hunk_path_holds_nothing(repo, status) -> None:
    _commit_base_and_record_baseline(repo)
    _record_measured(repo, OWN_EDIT)
    _write(repo, PEER_EDIT)
    _row(repo, status=status)
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_a_row_naming_another_path_holds_nothing(repo) -> None:
    _commit_base_and_record_baseline(repo)
    _record_measured(repo, OWN_EDIT)
    _write(repo, PEER_EDIT)
    _row(repo, file="pkg/other.py")
    assert peer_hunk_gate.out_of_ask_holds(repo, SELF, OWN, [PATH]) == []


def test_landing_holds_refuses_the_path_while_the_peer_hunk_rides_it(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import hunk_provenance

    _commit_base_and_record_baseline(repo)
    _record_measured(repo, OWN_EDIT)
    _write(repo, PEER_EDIT)
    _row(repo)
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda *a, **k: set())
    held = peer_hunk_gate.landing_holds(repo, SELF, OWN, None, [PATH], [], [PATH], [PATH], None)
    assert list(held) == [PATH]
