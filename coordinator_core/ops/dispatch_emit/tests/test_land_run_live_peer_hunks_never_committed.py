"""dispatch.land_run never commits a live co-declaring peer run's hunks or untracked module under its own run."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, peer_hunk_gate, run_token
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/shared.py"
OTHER = "pkg/own.py"
DELIVERY = "docs/delivery/shared.md"
IMPORTER = "pk/u.py"
MODULE = "pk/m.py"
M_BASE = "def old():\n    pass\n"
M_NEW = M_BASE + "\n\ndef note_ids():\n    return []\n"
U_BASE = "def run():\n    return 1\n"
U_NEW = "def run():\n    from pk import m\n\n    return m.note_ids()\n"
PEER = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 60))}-7cc61e"


def _land(root, **extra) -> dict:
    return prod._land(root, **{"inline_review": reviewed(), **extra})


def _live_peer(root, *paths: str, run_id: str = PEER) -> str:
    """A live, unlanded peer run declaring ``paths`` in its sizing's plan spine; its token is minted now."""
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    sizing = "state/sizings/peer.yaml"
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    prod._write(root, sizing, 'plan: "docs/plans/peer.md"\n')
    writes = "".join(f"    - {p}\n" for p in paths)
    prod._write(
        root, "docs/plans/peer.md", "# Peer\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n" + writes + "```\n",
    )
    run_token.mint(root, run_id, "peer-run.mjs", session_id=prod.SELF)
    return run_id


def _prov(out: dict, reason: str) -> list:
    return [e for e in out.get("provenance") or [] if e["reason"] == reason]


def _commit_seed(root, files: dict) -> None:
    for rel, text in files.items():
        prod._write(root, rel, text)
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "seed"], root)


def _head_blob(root, rel: str) -> str:
    return prod._git(["show", f"HEAD:{rel}"], root)


@pytest.mark.designed_red
def test_a_path_a_live_peer_holds_unsplit_stays_at_its_head_blob_and_is_named_withheld(repo):
    """A: the peer's hunks on PATH cannot be split off, so the commit carries no blob of it."""
    _commit_seed(repo, {PATH: "base = 0\n"})
    _live_peer(repo, PATH)
    prod._author(repo, PATH, "base = 0\nmine = 1\n")
    prod._author(repo, OTHER, "own = 1\n")
    prod._declare(repo, PATH, OTHER)
    prod._record_dir(repo)

    out = _land(repo)

    assert out.get("committed") is True and out.get("refused") is None, out
    assert PEER in str(_prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), out
    assert PATH not in prod._tree(repo), out
    assert OTHER in prod._tree(repo), out
    assert _head_blob(repo, PATH) == "base = 0\n"
    assert (repo / PATH).read_text(encoding="utf-8") == "base = 0\nmine = 1\n"
    assert out["withheld"][PATH] == f"withheld by {PEER}", out


def test_b_hunks_that_split_cleanly_commit_only_the_landing_runs_own(repo):
    """B: both runs append to a per-run artifact; the peer's baseline is HEAD plus this run's lines."""
    head = "head\n"
    mine = "mine1\nmine2\n"
    theirs = "theirs1\n"
    _commit_seed(repo, {DELIVERY: head})
    peer = _live_peer(repo, DELIVERY)
    prod._write(repo, f"state/warp/{prod.RUN}/manifest.json", json.dumps({
        "rows": [{"id": "C1", "writes": [DELIVERY]}], "write_baseline": {DELIVERY: _hash(repo, head)},
    }))
    prod._write(repo, f"state/warp/{peer}/manifest.json", json.dumps({
        "rows": [{"id": "C1", "writes": [DELIVERY]}], "write_baseline": {DELIVERY: _hash(repo, head + mine)},
    }))
    prod._author(repo, DELIVERY, head + mine + theirs)
    prod._declare(repo, DELIVERY)
    prod._record_dir(repo)

    out = _land(repo)

    assert out.get("committed") is True and out.get("refused") is None, out
    assert _head_blob(repo, DELIVERY) == head + mine
    assert (repo / DELIVERY).read_text(encoding="utf-8") == head + mine + theirs


def _hash(root, text: str) -> str:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=text.encode(), capture_output=True,
        check=True, **no_console_creationflags(),
    ).stdout.decode().strip()


def _seed_pair(root) -> None:
    _commit_seed(root, {"pk/__init__.py": "", MODULE: M_BASE, IMPORTER: U_BASE})


def _land_importer(root, **extra) -> dict:
    prod._author(root, IMPORTER, U_NEW)
    prod._declare(root, IMPORTER, OTHER)
    prod._author(root, OTHER, "own = 1\n")
    prod._record_dir(root)
    return _land(root, **extra)


def _assert_module_stays_out(root, out: dict) -> None:
    assert out.get("committed") is True and out.get("refused") is None, out
    assert [e["path"] for e in _prov(out, landing_integrity.IMPORT_CLOSURE_UNTRACKED)] == [IMPORTER], out
    tree = prod._tree(root)
    assert MODULE not in tree, out
    assert IMPORTER not in tree, out
    assert OTHER in tree, out
    assert out["withheld"][IMPORTER].endswith(f": {MODULE}"), out


@pytest.mark.designed_red
def test_c_an_untracked_module_only_a_live_peer_declares_is_not_carried_and_names_the_importer_out(repo):
    """C: the importer reads `note_ids` that only the peer's uncommitted module defines."""
    _seed_pair(repo)
    _live_peer(repo, MODULE)
    prod._write(repo, MODULE, M_NEW)

    out = _land_importer(repo)

    _assert_module_stays_out(repo, out)


@pytest.mark.designed_red
def test_c_the_module_stays_out_when_the_landing_run_is_senior_to_the_peer(repo):
    """C, junior peer: a run minted after this one is not a senior hold, yet its module is not this run's to commit."""
    _seed_pair(repo)
    token = run_token.mint(repo, prod.RUN, prod.SCRIPT, session_id=prod.SELF)
    time.sleep(0.05)
    _live_peer(repo, MODULE)
    prod._write(repo, MODULE, M_NEW)

    out = _land_importer(repo, run_token=token)

    _assert_module_stays_out(repo, out)
