"""A halted landing that credits a shared path (own print, or manifest ``review_declared_paths`` under an
unanswerable peer claim) commits only the run's own hunks of it; the peer's hunks stay in the tree.

Shape of run warp-2026-11-24-publish-engine-export-completion-marker-413cd9 (eaf37987fe7): the run halts at
the ledger check with a peer's uncommitted hunks in a file it also edits, the refire's halted landing
credits that file whole, and the commit carries the peer's hunks.

Fixture traps: the print is the raw-byte fingerprint of the worktree file (Windows writes CRLF, so a blob
hashed from the LF text never equals it), and the call carries a passing test verdict (without one a
ledger-check halt holds every chunk record-only and the credit path is never reached)."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.tests.test_halted_record_commit_peer_hunks_excluded import (  # noqa: F401
    PEER_RUN,
    SHARED,
    _scene,
    _text,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _landed,
    _marker_script,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BOTH = _text({2: "PEER", 8: "OWN"})
OWN_ONLY = _text({8: "OWN"})


@pytest.fixture
def unanswerable(monkeypatch):
    """The claim lookup answers 'indeterminate' for every path it is asked about."""

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        return [f"claim state indeterminate for {p!r}" for p in paths]

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)


def _print(root) -> None:
    """The run's withheld print of SHARED: the fingerprint of the worktree file as it is now."""
    (root / RECORD_DIR_ROOT / RUN / "withheld-prints.json").write_text(
        json.dumps({SHARED: fingerprint(root, SHARED)}), encoding="utf-8"
    )


def _declare(
    root, *, print_blob: bool, review_declared: bool, peer_recorded: bool = True, kept_whole: bool = False,
) -> None:
    """HEAD holds line1..10; the run owns line 8 and a peer wrote line 2, both uncommitted in SHARED.
    `kept_whole`: the run's `kept.json` holds the whole worktree image the halt recorded, peer line included."""
    _scene(root, own_recorded=True, peer_recorded=peer_recorded)
    record = root / RECORD_DIR_ROOT / RUN
    if kept_whole:
        whole = _git(["hash-object", "-w", SHARED], root).strip()
        (record / "kept.json").write_text(
            json.dumps({"chunks": {"C1": {"paths": {SHARED: whole}}}}), encoding="utf-8"
        )
    manifest = json.loads((record / "manifest.json").read_text(encoding="utf-8"))
    if review_declared:
        manifest["review_declared_paths"] = [SHARED]
        (record / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if print_blob:
        _print(root)
    _marker_script(root, [("C1", [SHARED])], {"C1": "DONE: C1\n"})


def _assert_own_hunks_only(root, out) -> None:
    assert out.get("committed") is True, out
    assert SHARED in _landed(root, out, "--name-only"), out
    assert _git(["show", f"HEAD:{SHARED}"], root) == OWN_ONLY, out
    assert (root / SHARED).read_text(encoding="utf-8") == BOTH


def test_print_credited_path_lands_only_the_runs_hunks(repo, unanswerable):
    _declare(repo, print_blob=True, review_declared=False)

    out = _call(repo, LEDGER, **PASSING)

    _assert_own_hunks_only(repo, out)


def test_review_declared_credited_path_lands_only_the_runs_hunks(repo, unanswerable):
    _declare(repo, print_blob=False, review_declared=True)

    out = _call(repo, LEDGER, **PASSING)

    _assert_own_hunks_only(repo, out)


def test_whole_image_kept_blob_does_not_credit_a_peer_recorded_hunk(repo, unanswerable):
    _declare(repo, print_blob=True, review_declared=True, kept_whole=True)

    out = _call(repo, LEDGER, **PASSING)

    _assert_own_hunks_only(repo, out)
    assert out["foreign_hunks"][SHARED] == [{"lines": "2", "owner": PEER_RUN}], out


def test_whole_image_kept_blob_with_no_peer_record_still_lands_whole(repo, unanswerable):
    _declare(repo, print_blob=True, review_declared=True, peer_recorded=False, kept_whole=True)

    out = _call(repo, LEDGER, **PASSING)

    assert out.get("committed") is True, out
    assert SHARED in _landed(repo, out, "--name-only"), out
    assert _git(["show", f"HEAD:{SHARED}"], repo) == BOTH


def test_unreadable_measured_evidence_is_recorded_not_silent(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_commit, stranded_sibling

    _scene(repo, own_recorded=True, peer_recorded=False)

    def boom(_record_dir):
        raise ValueError("touched-blobs.json unreadable")

    monkeypatch.setattr(stranded_sibling, "read", boom)

    refused, reply = halted_record_commit._peer_hunks_excluded(repo, RUN, [SHARED], {})

    assert refused == []
    assert reply["own_split_read_failed"][SHARED] == "ValueError: touched-blobs.json unreadable", reply


def test_print_credited_path_no_peer_has_dirty_still_lands_whole(repo, unanswerable):
    _declare(repo, print_blob=False, review_declared=True, peer_recorded=False)
    (repo / SHARED).write_text(OWN_ONLY, encoding="utf-8")
    _print(repo)

    out = _call(repo, LEDGER, **PASSING)

    assert out.get("committed") is True, out
    assert SHARED in _landed(repo, out, "--name-only"), out
    assert _git(["show", f"HEAD:{SHARED}"], repo) == OWN_ONLY
