"""The halt commit lands a land-only product path only when every hunk in it is the halted run's own."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import hunk_provenance
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _git,
    _landed,
    repo,
)
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHARED = "src/shared.py"


def _text(edits: dict) -> str:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11))


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _halted_run(root, *, baseline_text: str, tree_text: str) -> None:
    """Commit ``line1..10`` at HEAD; the run staged against ``baseline_text`` and the tree holds
    ``tree_text``; the marker withholds SHARED as the run's own product (a land-only ask)."""
    _write(root, SHARED, _text({}))
    _git(["add", SHARED], root)
    _git(["commit", "-q", "-m", "shared"], root)
    _write(root, SHARED, baseline_text)
    blob = _git(["hash-object", "-w", SHARED], root).strip()
    _write(root, SHARED, tree_text)
    record = f"{RECORD_DIR_ROOT}/{RUN}"
    _write(root, f"{record}/halted.json", json.dumps({
        "status": "halted", "halted": "ledger-check",
        "run_artifacts": {"withheld": {SHARED: "halt-record-only"}},
    }))
    _write(root, f"{record}/manifest.json", json.dumps({"rows": [], "write_baseline": {SHARED: blob}}))


def _land_only(root) -> dict:
    return terminal_commit._handler(
        {"run_id": RUN, HALTED_RECORD_PARAM: LEDGER}, repo_root=root / ".git",
    )


def test_a_product_path_carrying_a_peer_hunk_is_withheld_and_not_committed(repo):
    # The peer wrote line 2 before the run staged (it is in the baseline, uncommitted); the run wrote line 8.
    _halted_run(repo, baseline_text=_text({2: "PEER"}), tree_text=_text({2: "PEER", 8: "OWN"}))

    out = _land_only(repo)

    assert out["withheld"][SHARED] == hunk_provenance.FOREIGN_HUNK_UNPRODUCED
    assert SHARED not in _landed(repo, out, "--name-only")
    assert (repo / SHARED).read_text(encoding="utf-8") == _text({2: "PEER", 8: "OWN"})


def test_a_product_path_holding_only_the_runs_own_hunks_still_lands(repo):
    _halted_run(repo, baseline_text=_text({}), tree_text=_text({8: "OWN"}))

    out = _land_only(repo)

    assert SHARED not in out.get("withheld", {})
    assert SHARED in _landed(repo, out, "--name-only")
    assert _git(["show", f"HEAD:{SHARED}"], repo) == _text({8: "OWN"})
