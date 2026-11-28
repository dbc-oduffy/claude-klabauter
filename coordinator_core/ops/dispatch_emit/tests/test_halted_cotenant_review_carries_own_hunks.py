"""A path a halted run names as co-tenant reaches review at HEAD plus the asking run's own hunks, the halted run's hunks excluded."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import co_tenant_owner, halted_withheld_index, peer_hunk_gate, review_edits, run_write_prints, stage_wait
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_record_ledger import _co_tenant_print
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ASKER = "ask-20261128T064754-423d66"
HALTED = "warp-2026-11-28-takeover-own-writes-not-foreign-cdffc5-20261128T051056-f97c57"
PATH = "a.py"
HEAD_LINES = [f"line {i:02d}\n" for i in range(1, 31)]
OWN_LINES = [*(f"own {i:02d}\n" for i in range(1, 4)), *HEAD_LINES[3:]]
HALTED_LINES = [*HEAD_LINES[:19], *(f"halted {i:02d}\n" for i in range(20, 23)), *HEAD_LINES[22:]]
BOTH_LINES = [*OWN_LINES[:19], *HALTED_LINES[19:22], *OWN_LINES[22:]]


def _git(root, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, input=None if stdin is None else stdin.encode("utf-8"),
        **no_console_creationflags(),
    ).stdout.decode("utf-8")


def _write(root, lines):
    (root / PATH).write_text("".join(lines), encoding="utf-8", newline="")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """HEAD holds a.py; the asking run X edited lines 1-3 (write print recorded), halted run H edited lines
    20-22 and its record withholds and keeps a.py; the worktree carries both runs' hunks."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, HEAD_LINES)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    head_blob = _git(root, "rev-parse", f"HEAD:{PATH}").strip()
    asker = root / RECORD_DIR_ROOT / ASKER
    asker.mkdir(parents=True)
    (asker / "manifest.json").write_text(
        json.dumps({"write_baseline": {PATH: head_blob}, "review_declared_paths": [PATH]}), encoding="utf-8",
    )
    _write(root, OWN_LINES)
    run_write_prints.record(root, ASKER, [PATH])
    _write(root, BOTH_LINES)
    halted = root / RECORD_DIR_ROOT / HALTED
    halted.mkdir(parents=True)
    (halted / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "run_artifacts": {"withheld": {PATH: "kept"}}}),
        encoding="utf-8",
    )
    halted_blob = _git(root, "hash-object", "-w", "--stdin", stdin="".join(HALTED_LINES)).strip()
    (halted / "withheld-prints.json").write_text(json.dumps({PATH: halted_blob}), encoding="utf-8")
    (halted / "kept.json").write_text(
        json.dumps({"chunks": {"C0": {"paths": {PATH: halted_blob}}}}), encoding="utf-8",
    )
    index = root / halted_withheld_index.INDEX_DIR
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{HALTED}.json").write_text(
        json.dumps({"paths": [PATH], "declared": [PATH], "at_ns": 1_000}), encoding="utf-8",
    )
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    return root


def _own_bytes() -> bytes:
    return "".join(OWN_LINES).encode("utf-8")


def test_ac1_a_halted_run_naming_the_path_is_not_its_owner(repo) -> None:
    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=ASKER) == {"run_id": None, "state": None}


def test_ac2_peer_hunk_runs_names_the_halted_run_and_drops_the_asker(repo) -> None:
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=ASKER) == (HALTED,)
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=HALTED) == ()


def test_ac2_peer_hunk_runs_ignores_a_path_no_run_names_and_spawns_nothing(repo, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)

    assert co_tenant_owner.peer_hunk_runs(repo, "other.py", exclude_run_id=ASKER) == ()
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=ASKER) == (HALTED,)


@pytest.mark.parametrize("parked_behind_asker", [False, True])
def test_ac3_the_withheld_print_is_head_plus_the_own_hunks_never_a_typed_entry(repo, parked_behind_asker) -> None:
    if parked_behind_asker:
        stage_wait.park(repo, HALTED, [ASKER], [PATH])
    printed = _co_tenant_print(repo, ASKER, PATH)

    assert isinstance(printed, str), printed
    assert _git(repo, "cat-file", "blob", printed) == _own_bytes().decode("utf-8")


def test_ac4_own_delta_overrides_the_path_to_head_plus_the_own_hunks_without_the_halted_hunks(repo) -> None:
    paths, overrides = cov.own_delta(repo, ASKER, [PATH])

    assert paths == [PATH]
    assert overrides == {PATH: _own_bytes()}
    assert b"halted" not in overrides[PATH]
