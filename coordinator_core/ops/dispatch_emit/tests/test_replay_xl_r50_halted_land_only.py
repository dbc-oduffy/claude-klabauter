"""Replay of the XL r50 ladder cell (relayed-ask f84aeb): a halted run withholding DELIVERY.md and 5 more
paths releases every declared path it does not withhold, and a land-only ask on it commits what it withheld."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

HALTED = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-46eaa5"
PEER = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-aaaaaa"
WITHHELD = ["DELIVERY.md", "src/ontology/model.py", "src/ontology/store.py", "src/ontology/query.py",
            "tests/test_ontology.py", "docs/ontology.md"]
DECLARED_NOT_WITHHELD = "src/ontology/extra.py"


@pytest.fixture(autouse=True)
def _hosts_state(monkeypatch):
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "seed.txt", "seed\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    rec = f"state/warp/{HALTED}"
    _write(root, f"{rec}/halted.json", json.dumps({
        "status": "halted", "halted": "ledger-check",
        "run_artifacts": {"withheld": {p: "halt-record-only" for p in WITHHELD}},
    }))
    _write(root, f"{rec}/manifest.json", json.dumps(
        {"rows": [{"id": "C1", "writes": [*WITHHELD, DECLARED_NOT_WITHHELD]}], "doc_writes": []}))
    for p in WITHHELD:
        _write(root, p, f"# finished work in {p}\n")
    _write(root, DECLARED_NOT_WITHHELD, "# declared by the halted run, committed by a peer\n")
    return root


def test_a_peer_is_not_refused_a_path_the_halted_run_declared_but_did_not_withhold(root):
    """Peer-side premise only: drives sibling_claims, not the land-only declaration."""
    from coordinator_core.ops.dispatch_emit import sibling_claims

    claims = sibling_claims.sibling_run_claims(root, PEER, [DECLARED_NOT_WITHHELD, *WITHHELD])
    assert DECLARED_NOT_WITHHELD not in claims
    assert {p: claims[p] for p in WITHHELD if p in claims} == {p: HALTED for p in WITHHELD}


def test_a_land_only_ask_on_the_halted_run_commits_the_withheld_paths(root):
    from coordinator_core.ops.dispatch_emit import land_run

    out = land_run._handler({"run_id": HALTED}, repo_root=root / ".git")

    assert out.get("refused") != "halted-run", out
    assert out.get("ok") is True or out.get("committed") is True, out  # either success key; `landed` below is the real check
    landed = set(_git(root, "show", "--name-only", "--format=", out["sha"]).splitlines())
    assert set(WITHHELD) <= landed, sorted(set(WITHHELD) - landed)
    assert DECLARED_NOT_WITHHELD not in landed


def test_a_land_only_ask_on_a_halted_run_withholding_nothing_is_still_refused(root):
    from coordinator_core.ops.dispatch_emit import land_run

    marker = root / "state" / "warp" / HALTED / "halted.json"
    marker.write_text(json.dumps({"status": "halted", "halted": "ledger-check"}), encoding="utf-8")
    out = land_run._handler({"run_id": HALTED}, repo_root=root / ".git")
    assert out["refused"] == "halted-run"
