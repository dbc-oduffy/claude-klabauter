"""`peer_hunk_gate.landing_holds` withholds a path a live sibling run declares while the file carries a
hunk the landing run did not produce.

Mechanism of 348c80115d (run 39dfab landed `unimatrix-install.py` carrying another live run's
status-line seeding): `landing_holds` consulted `co_declared_peer_holds` only for paths a reviewer
finding named, and the finding carried no `out_of_ask: true`, so a live sibling's declared path was
never examined and the path-level commit swept the sibling's uncommitted hunks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_token
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

SELF = "5e1f0000-0000-4000-8000-0000000000c4"
PATH = "pkg/unimatrix_install.py"
OWN = "run-own"
LIVE = "run-live-sibling"
ROWS = [f"x_{i} = {i}\n" for i in range(1, 41)]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / PATH).write_text("".join(ROWS), encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    head_blob = _git(["rev-parse", f"HEAD:{PATH}"], root)
    own = root / "state" / "warp" / OWN
    own.mkdir(parents=True)
    (own / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: head_blob}}),
        encoding="utf-8",
    )
    sib = root / "state" / "warp" / LIVE
    sib.mkdir(parents=True)
    (sib / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}), encoding="utf-8"
    )
    run_token.mint(root, LIVE, "sibling.mjs", session_id=SELF)
    return root


def _holds(root: Path) -> dict:
    return peer_hunk_gate.landing_holds(
        root, SELF, OWN, None, [PATH], [], [PATH], [PATH], None,
    )


def test_a_live_siblings_declared_path_carrying_its_hunk_is_withheld_naming_it(repo):
    rows = list(ROWS)
    rows[2] = "x_3 = 'SIBLING status-line seeding'\n"
    rows[37] = "x_38 = 'OWN'\n"
    (repo / PATH).write_text("".join(rows), encoding="utf-8")
    manifest = repo / "state" / "warp" / OWN / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    sib_only = list(ROWS)
    sib_only[2] = rows[2]
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input="".join(sib_only).encode("utf-8"),
        capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    data["write_baseline"] = {PATH: blob}  # the sibling's hunk was in the tree when this run staged
    manifest.write_text(json.dumps(data), encoding="utf-8")

    assert _holds(repo) == {PATH: LIVE}


def test_a_live_sibling_declaring_the_path_with_no_hunk_in_it_does_not_hold_it(repo):
    rows = list(ROWS)
    rows[37] = "x_38 = 'OWN'\n"
    (repo / PATH).write_text("".join(rows), encoding="utf-8")

    assert _holds(repo) == {}


def test_a_terminal_sibling_holds_nothing(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import hunk_provenance

    rows = list(ROWS)
    rows[2] = "x_3 = 'SIBLING'\n"
    (repo / PATH).write_text("".join(rows), encoding="utf-8")
    monkeypatch.setattr(hunk_provenance, "_non_live", lambda root, owner: owner == LIVE)

    assert _holds(repo) == {}
