"""A critical finding, a ledger conflict or a halted owner's live refire on a path carrying a live
peer's uncommitted hunk never lands the peer's hunk through the default `dispatch.terminal_commit`.

Each scenario asserts HEAD's blob for the contested path is the seed plus this run's own hunk only,
or that the path is named in `sibling_withheld`.
"""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_uncodeclared_peer_hunks_incident import (  # noqa: F401
    OWN_MARK, P, PEER_MARK, RUN_OWN, RUN_PEER, SELF, _body, _build, _declare, _git,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_REFIRE = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 30))}-5b4c54"
CONFLICT = f"memo says {P} stays untouched; the ask edits it"


def _flag_critical(root) -> None:
    share = root / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True)
    (share / f"{RUN_OWN}.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8"
    )
    row = {"id": "finding-1", "file": P, "summary": "peer hunks ride along", "severity": "critical"}
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\nPeer hunks.\n"
        f"\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n",
        encoding="utf-8",
    )


def _assert_no_peer_hunk_landed(root, seed_blob: str, out: dict) -> None:
    withheld = P in (out.get("sibling_withheld") or {})
    head = _git(["show", f"HEAD:{P}"], root)
    assert PEER_MARK not in head, out
    assert withheld or head == _body(own=True).strip(), out
    if withheld:
        assert head == seed_blob, out
    assert PEER_MARK in (root / P).read_text(encoding="utf-8")


def _fire_with(root, **extra) -> dict:
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(P,)),))
    marker = root / RUN_DIR_ROOT / RUN_OWN / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}state/warp/{RUN_OWN}/manifest.json\n", encoding="utf-8")
    params = {
        "inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [],
        "run_id": RUN_OWN, "session_id": SELF, **extra,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


@pytest.mark.parametrize("peer_declares", [True, False])
def test_a_critical_finding_on_a_path_with_a_live_peer_hunk_never_lands_the_peer_hunk(
    tmp_path, monkeypatch, peer_declares,
):
    root = _build(tmp_path, monkeypatch, peer_hunk=True, peer_declares=peer_declares)
    _flag_critical(root)
    seed_blob = _git(["show", f"HEAD:{P}"], root)

    out = _fire_with(root)

    _assert_no_peer_hunk_landed(root, seed_blob, out)


@pytest.mark.parametrize("peer_declares", [True, False])
def test_a_ledger_conflict_naming_a_path_with_a_live_peer_hunk_never_lands_the_peer_hunk(
    tmp_path, monkeypatch, peer_declares,
):
    root = _build(tmp_path, monkeypatch, peer_hunk=True, peer_declares=peer_declares)
    seed_blob = _git(["show", f"HEAD:{P}"], root)

    out = _fire_with(root, ledger_verdict={"items": [], "conflicts": [CONFLICT]})

    _assert_no_peer_hunk_landed(root, seed_blob, out)


def test_a_halted_owner_with_a_live_refire_declaring_the_path_never_lands_the_peer_hunk(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch, peer_hunk=True, peer_declares=True)
    (root / "state" / "warp" / RUN_PEER / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    _declare(root, RUN_REFIRE, None)
    run_token.mint(root, RUN_REFIRE, "refire.mjs", session_id=SELF)
    seed_blob = _git(["show", f"HEAD:{P}"], root)

    out = _fire_with(root)

    _assert_no_peer_hunk_landed(root, seed_blob, out)


def test_a_ledger_conflict_naming_an_own_only_path_still_lands_it_whole(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch, peer_hunk=False)

    out = _fire_with(root, ledger_verdict={"items": [], "conflicts": [CONFLICT]})

    assert out["committed"] is True, out
    assert _git(["show", f"HEAD:{P}"], root) == _body(own=True).strip()


def test_the_composed_script_hands_the_ledger_conflicts_to_the_terminal_commit():
    script = _compose()
    assert (
        "_nextAction.params.ledger_verdict = { "
        "conflicts: Array.isArray(_ledger.conflicts) ? _ledger.conflicts : [], items:"
    ) in script


def test_the_ledger_conflict_strings_are_read_defensively():
    f = terminal_commit._ledger_conflict_strings
    assert f({"conflicts": ["a", 3, "b"]}) == ("a", "b")
    assert f({"conflicts": "abc"}) == () and f({}) == () and f(None) == ()
