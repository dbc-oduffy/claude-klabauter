"""The halt commit lands only the halted run's own hunks of a path a peer run also edits (R4, cases 5-9).

Two runs edit one file: the halting run's recorded hunk lands, the live peer's hunk stays in the worktree
uncommitted and is named under `foreign_hunks`; a hunk with no recorded owner refuses the path naming lines
and owner. Case 9 (example-game-repo b9c1a5-1 swept run c35e97-2's live driver.py hunks) is the same shape in a
second repo: the candidate rule reads only the repo's own record root."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import hunk_provenance, stranded_sibling, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _git,
    _landed,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHARED = "src/shared.py"
PEER_RUN = "ask-20261114T205015-c35e97"


def _text(edits: dict) -> str:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11))


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _blob(root, text: str, path: str = SHARED) -> str:
    _write(root, path, text)
    return _git(["hash-object", "-w", path], root).strip()


def _scene(
    root, *, own_recorded: bool, peer_recorded: bool, path: str = SHARED, peer_run: str = PEER_RUN,
) -> None:
    """HEAD holds line1..10; the run baselined HEAD, owns line 8; the live peer run wrote line 2."""
    _write(root, path, _text({}))
    _git(["add", path], root)
    _git(["commit", "-q", "-m", "shared"], root)
    head_blob = _blob(root, _text({}), path)
    own_blob = _blob(root, _text({8: "OWN"}), path)
    peer_blob = _blob(root, _text({2: "PEER"}), path)
    _write(root, path, _text({2: "PEER", 8: "OWN"}))
    record = f"{RECORD_DIR_ROOT}/{RUN}"
    _write(root, f"{record}/halted.json", json.dumps({
        "status": "halted", "halted": "ledger-check",
        "run_artifacts": {"withheld": {path: "halt-record-only"}},
    }))
    _write(root, f"{record}/manifest.json", json.dumps({"rows": [], "write_baseline": {path: head_blob}}))
    if own_recorded:
        stranded_sibling.write(root / record, {path: {"base": head_blob, "run": own_blob}})
    if peer_recorded:
        stranded_sibling.write(root / RECORD_DIR_ROOT / peer_run, {path: {"base": head_blob, "run": peer_blob}})


def _land_only(root) -> dict:
    return terminal_commit._handler({"run_id": RUN, HALTED_RECORD_PARAM: LEDGER}, repo_root=root / ".git")


def test_the_halting_runs_hunk_lands_and_the_live_peers_hunk_stays_uncommitted(repo):
    _scene(repo, own_recorded=True, peer_recorded=True)

    out = _land_only(repo)

    assert SHARED not in out.get("withheld", {}), out
    assert SHARED in _landed(repo, out, "--name-only")
    assert _git(["show", f"HEAD:{SHARED}"], repo) == _text({8: "OWN"})
    assert (repo / SHARED).read_text(encoding="utf-8") == _text({2: "PEER", 8: "OWN"})
    assert out["foreign_hunks"][SHARED] == [{"lines": "2", "owner": PEER_RUN}]


def test_a_path_no_peer_run_records_lands_whole(repo):
    _scene(repo, own_recorded=True, peer_recorded=False)

    out = _land_only(repo)

    assert SHARED in _landed(repo, out, "--name-only")
    assert "foreign_hunks" not in out
    assert _git(["show", f"HEAD:{SHARED}"], repo) == _text({2: "PEER", 8: "OWN"})


def test_an_unowned_hunk_is_excluded_naming_unknown_and_the_runs_own_hunk_lands(repo):
    _scene(repo, own_recorded=True, peer_recorded=False)
    baseline = _blob(repo, _text({2: "PEER"}))
    _write(repo, SHARED, _text({2: "PEER", 8: "OWN"}))
    manifest = repo / RECORD_DIR_ROOT / RUN / "manifest.json"
    manifest.write_text(json.dumps({"rows": [], "write_baseline": {SHARED: baseline}}), encoding="utf-8")

    out = _land_only(repo)

    assert SHARED in _landed(repo, out, "--name-only")
    assert _git(["show", f"HEAD:{SHARED}"], repo) == _text({8: "OWN"})
    assert (repo / SHARED).read_text(encoding="utf-8") == _text({2: "PEER", 8: "OWN"})
    assert out["foreign_hunks"][SHARED] == [{"lines": "2", "owner": "unknown"}]


def test_a_path_with_no_recorded_own_hunk_and_an_unowned_hunk_is_withheld(repo):
    _scene(repo, own_recorded=False, peer_recorded=False)
    manifest = repo / RECORD_DIR_ROOT / RUN / "manifest.json"
    manifest.write_text(
        json.dumps({"rows": [], "write_baseline": {SHARED: _blob(repo, _text({2: "PEER"}))}}), encoding="utf-8"
    )
    _write(repo, SHARED, _text({2: "PEER", 8: "OWN"}))

    out = _land_only(repo)

    assert out["withheld"][SHARED] == hunk_provenance.FOREIGN_HUNK_UNPRODUCED
    assert SHARED not in _landed(repo, out, "--name-only")


def test_case9_example_game_repo_b9c1a5_lands_only_its_own_driver_hunks_not_live_run_c35e97s(repo):
    driver, live_peer = "Source/Example-Game-Repo/driver.py", "ask-20261114T203000-c35e97"
    _scene(repo, own_recorded=True, peer_recorded=True, path=driver, peer_run=live_peer)

    out = _land_only(repo)

    assert driver in _landed(repo, out, "--name-only")
    assert _git(["show", f"HEAD:{driver}"], repo) == _text({8: "OWN"})
    assert (repo / driver).read_text(encoding="utf-8") == _text({2: "PEER", 8: "OWN"})
    assert out["foreign_hunks"][driver] == [{"lines": "2", "owner": live_peer}]
