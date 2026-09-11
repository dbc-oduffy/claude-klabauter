"""
Tests for coordinator_core.ops.fleet.memo_heal — memo.heal_inbox MUTATING op
(C5, docs/plans/2026-09-11-memo-deliveries-survive-the-receiver-s-o.md).

Harness: sync handler, called directly (mirrors test_memo_send.py's own
pattern) — one real temp git repo per test, this op run against ITSELF
(receiver = repo_root, no peer repo involved, unlike memo.send).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet import memo_heal as memo_heal_module
from coordinator_core.ops.fleet._memo_anchor import anchor_names, write_anchor
from coordinator_core.ops.fleet.memo_heal import (
    ADOPT_CAP_PER_RUN,
    _MODE,
    _memo_heal_inbox,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FAKE_SHA_A = "a" * 40  # never a real object -- models "commit object gone"


# ---------------------------------------------------------------------------
# Git repo factory + small helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check,
        **no_console_creationflags(),
    )


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _common_dir(repo: Path) -> Path:
    return repo / ".git"


def _run(repo: Path, dry_run: bool) -> dict:
    return _memo_heal_inbox({"dry_run": dry_run}, repo_root=_common_dir(repo))


def _commit_file(repo: Path, relpath: str, content: str, message: str) -> tuple[str, str]:
    """Write+commit `relpath` under `repo`; returns (HEAD sha, blob sha)."""
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    blob = _git(repo, "rev-parse", f"HEAD:{relpath}").stdout.strip()
    return head, blob


def _delete_and_commit(repo: Path, relpath: str, message: str) -> str:
    (repo / relpath).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _anchor_for(common_dir: Path, filename: str):
    return [t for t in anchor_names(common_dir) if t[0] == filename]


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------


def test_dry_run_must_be_bool():
    result = _memo_heal_inbox({"dry_run": "yes"}, repo_root="/tmp/x")
    assert result["exit_code"] == 1


def test_unknown_param_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    result = _memo_heal_inbox({"dry_run": True, "topic": "x"}, repo_root=_common_dir(repo))
    assert result["exit_code"] == 1


def test_missing_repo_root_is_a_setup_error():
    result = _memo_heal_inbox({"dry_run": True}, repo_root=None)
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# No-op path -- zero spawns
# ---------------------------------------------------------------------------


def test_no_op_path_makes_zero_spawns(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    def _forbidden(*a, **k):
        raise AssertionError("no git subprocess expected on the no-op path")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    result = _run(repo, dry_run=False)
    assert result["exit_code"] == 0
    assert result["acted"] == []
    assert result["failed"] == []


# ---------------------------------------------------------------------------
# RESTORE -- anchor's commit object is gone
# ---------------------------------------------------------------------------


def test_restore_anchor_whose_commit_is_gone_is_committed_with_matching_bytes(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    body = "Lost memo body.\n"
    blob_sha = write_anchor(common_dir, "lost.md", _FAKE_SHA_A, body.encode("utf-8"))
    assert blob_sha is not None

    result = _run(repo, dry_run=False)

    assert result["exit_code"] == 0
    assert {"id": "lost.md", "action": "restored"} in result["acted"]
    restored_path = repo / "state" / "cross-repo" / "inbox" / "lost.md"
    assert restored_path.read_text(encoding="utf-8") == body
    # Re-keyed: the old (fake) sha is gone, a new real commit sha anchors it.
    triples = _anchor_for(common_dir, "lost.md")
    assert len(triples) == 1
    assert triples[0][1] != _FAKE_SHA_A
    assert triples[0][2] == blob_sha


# ---------------------------------------------------------------------------
# RETIRE -- reachable from HEAD (receiver removed it on a branch it kept)
# ---------------------------------------------------------------------------


def test_anchor_reachable_from_head_is_retired_not_restored(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    head, blob = _commit_file(repo, "state/cross-repo/inbox/gone.md", "body\n", "deliver gone.md")
    _delete_and_commit(repo, "state/cross-repo/inbox/gone.md", "receiver deleted it")
    write_anchor(common_dir, "gone.md", head, b"body\n")

    result = _run(repo, dry_run=False)

    assert {"id": "gone.md", "action": "retired"} in result["acted"]
    assert not (repo / "state" / "cross-repo" / "inbox" / "gone.md").exists()
    assert _anchor_for(common_dir, "gone.md") == []


# ---------------------------------------------------------------------------
# LEAVE -- unreachable from HEAD, but reachable from another branch
# ---------------------------------------------------------------------------


def test_anchor_unreachable_from_head_but_reachable_from_another_branch_is_left_alone(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    _git(repo, "checkout", "-b", "side")
    head, blob = _commit_file(repo, "state/cross-repo/inbox/side.md", "side body\n", "deliver on side")
    _git(repo, "checkout", "main")
    write_anchor(common_dir, "side.md", head, b"side body\n")

    result = _run(repo, dry_run=False)

    assert result["acted"] == []
    assert result["failed"] == []
    # Anchor untouched -- neither retired nor restored.
    assert _anchor_for(common_dir, "side.md") == [("side.md", head, blob)]


# ---------------------------------------------------------------------------
# RESTORE -- unreachable from HEAD AND from every branch (orphaned)
# ---------------------------------------------------------------------------


def test_anchor_unreachable_from_any_branch_is_restored(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    _git(repo, "checkout", "-b", "side")
    head, blob = _commit_file(repo, "state/cross-repo/inbox/orphan.md", "orphan body\n", "deliver orphan")
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "side")  # drop the only branch carrying `head`
    write_anchor(common_dir, "orphan.md", head, b"orphan body\n")

    result = _run(repo, dry_run=False)

    assert {"id": "orphan.md", "action": "restored"} in result["acted"]
    restored_path = repo / "state" / "cross-repo" / "inbox" / "orphan.md"
    assert restored_path.read_text(encoding="utf-8") == "orphan body\n"


# ---------------------------------------------------------------------------
# RETIRE -- archived-and-not-in-inbox, loose and packed anchor
# ---------------------------------------------------------------------------


def test_archived_and_not_in_inbox_anchor_is_retired_loose(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    head, blob = _commit_file(
        repo, "state/cross-repo/archive/old.md", "archived body\n", "archive old.md",
    )
    write_anchor(common_dir, "old.md", head, b"archived body\n")

    result = _run(repo, dry_run=False)

    assert {"id": "old.md", "action": "retired"} in result["acted"]
    assert _anchor_for(common_dir, "old.md") == []


def test_archived_and_not_in_inbox_anchor_is_retired_packed(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    head, blob = _commit_file(
        repo, "state/cross-repo/archive/packed.md", "packed body\n", "archive packed.md",
    )
    write_anchor(common_dir, "packed.md", head, b"packed body\n")
    _git(repo, "pack-refs", "--all")
    assert _anchor_for(common_dir, "packed.md") == [("packed.md", head, blob)]

    result = _run(repo, dry_run=False)

    assert {"id": "packed.md", "action": "retired"} in result["acted"]
    assert _anchor_for(common_dir, "packed.md") == []


# ---------------------------------------------------------------------------
# ADOPT -- unanchored, tracked, matching-bytes inbox memo; untracked/dirty
# is skipped and counted.
# ---------------------------------------------------------------------------


def test_unanchored_tracked_matching_inbox_memo_is_adopted_with_zero_object_writes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    head, blob = _commit_file(
        repo, "state/cross-repo/inbox/fresh.md", "fresh body\n", "deliver fresh.md",
    )

    from coordinator_core.git import git_objects as git_objects_module

    def _forbidden_write(*a, **k):
        raise AssertionError("adopt must never write a git object")

    monkeypatch.setattr(git_objects_module, "write_object", _forbidden_write)

    result = _run(repo, dry_run=False)

    assert {"id": "fresh.md", "action": "adopted"} in result["acted"]
    assert _anchor_for(common_dir, "fresh.md") == [("fresh.md", head, blob)]


def test_a_memo_git_checked_out_as_crlf_is_still_adopted(tmp_path):
    # Every other adopt test writes LF by hand, so none of them sees what git
    # itself leaves on disk under `core.autocrlf=true`: an LF blob checked out
    # as CRLF. Git calls that file clean; a raw-bytes hash calls it dirty, and
    # on Windows that silently excluded every clone/checkout/reset memo from
    # adoption, leaving it non-durable.
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    rel = "state/cross-repo/inbox/checked-out.md"
    head, blob = _commit_file(repo, rel, "line one\nline two\n", "deliver checked-out.md")
    _git(repo, "config", "core.autocrlf", "true")
    (repo / rel).unlink()
    _git(repo, "checkout", "--", rel)
    assert b"\r\n" in (repo / rel).read_bytes(), "premise: git must have written CRLF"
    assert _git(repo, "status", "--porcelain", "--", rel).stdout.strip() == "", "premise: git calls it clean"

    result = _run(repo, dry_run=False)

    assert {"id": "checked-out.md", "action": "adopted"} in result["acted"]
    assert _anchor_for(common_dir, "checked-out.md") == [("checked-out.md", head, blob)]


def test_ref_illegal_filename_is_refused_without_poisoning_the_other_candidates(tmp_path):
    # Review: code-reviewer F1 -- an on-disk inbox filename is untrusted
    # input to the ref namespace. A single ref-illegal filename in the
    # SAME batch as a legitimate candidate must not fail the whole
    # `update-ref --stdin` transaction; it must be refused on its own and
    # the legitimate candidate must still land.
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    head, blob = _commit_file(
        repo, "state/cross-repo/inbox/good.md", "good body\n", "deliver good.md",
    )
    bad_rel = "state/cross-repo/inbox/bad file.md"
    (repo / bad_rel).write_text("bad body\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "deliver bad file.md")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run(repo, dry_run=False)

    assert {"id": "good.md", "action": "adopted"} in result["acted"]
    assert _anchor_for(common_dir, "good.md") == [("good.md", head, blob)]
    assert any(
        f["id"] == "bad file.md" and "ref path component" in f["reason"]
        for f in result["failed"]
    )
    assert _anchor_for(common_dir, "bad file.md") == []


def test_untracked_inbox_memo_is_not_adopted_and_is_counted(tmp_path):
    repo = _make_repo(tmp_path)
    inbox = repo / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "untracked.md").write_text("stray\n", encoding="utf-8")

    result = _run(repo, dry_run=False)

    assert not any(a.get("action") == "adopted" for a in result["acted"])
    skip_rows = [a for a in result["acted"] if a.get("action") == "adopt-skipped-count"]
    assert skip_rows and skip_rows[0]["count"] == 1


def test_dirty_inbox_memo_is_not_adopted_and_is_counted(tmp_path):
    repo = _make_repo(tmp_path)
    _commit_file(repo, "state/cross-repo/inbox/dirty.md", "original\n", "deliver dirty.md")
    (repo / "state" / "cross-repo" / "inbox" / "dirty.md").write_text(
        "edited locally\n", encoding="utf-8",
    )

    result = _run(repo, dry_run=False)

    assert not any(a.get("action") == "adopted" for a in result["acted"])
    skip_rows = [a for a in result["acted"] if a.get("action") == "adopt-skipped-count"]
    assert skip_rows and skip_rows[0]["count"] == 1


# ---------------------------------------------------------------------------
# LEAVE -- present and anchored (commit object still alive)
# ---------------------------------------------------------------------------


def test_present_and_anchored_memo_is_left_alone(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    head, blob = _commit_file(
        repo, "state/cross-repo/inbox/happy.md", "happy body\n", "deliver happy.md",
    )
    write_anchor(common_dir, "happy.md", head, b"happy body\n")

    result = _run(repo, dry_run=False)

    assert result["acted"] == []
    assert result["failed"] == []
    assert _anchor_for(common_dir, "happy.md") == [("happy.md", head, blob)]


# ---------------------------------------------------------------------------
# dry_run mutates nothing
# ---------------------------------------------------------------------------


def test_dry_run_mutates_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    blob_sha = write_anchor(common_dir, "preview.md", _FAKE_SHA_A, b"preview body\n")

    result = _run(repo, dry_run=True)

    assert result["dry_run"] is True
    assert any(c["id"] == "preview.md" and c["action"] == "restore" for c in result["candidates"])
    assert not (repo / "state" / "cross-repo" / "inbox" / "preview.md").exists()
    assert _anchor_for(common_dir, "preview.md") == [("preview.md", _FAKE_SHA_A, blob_sha)]


# ---------------------------------------------------------------------------
# Declined commit rolls back its own O_EXCL write
# ---------------------------------------------------------------------------


def test_declined_restore_commit_rolls_back_the_written_file(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    # A CR byte makes `commit_authored_new_file` refuse outright (it cannot
    # reproduce git's CRLF clean-direction normalization) -- a real decline,
    # not a mock.
    blob_sha = write_anchor(common_dir, "crlf.md", _FAKE_SHA_A, b"line one\r\nline two\n")

    result = _run(repo, dry_run=False)

    assert result["exit_code"] == 2
    assert any(f["id"] == "crlf.md" for f in result["failed"])
    assert not (repo / "state" / "cross-repo" / "inbox" / "crlf.md").exists()
    # Anchor is untouched -- the restore never landed, so nothing to re-key.
    assert _anchor_for(common_dir, "crlf.md") == [("crlf.md", _FAKE_SHA_A, blob_sha)]


# ---------------------------------------------------------------------------
# EEXIST on the restore write -- "restored by a peer", not a failure
# ---------------------------------------------------------------------------


def test_eexist_on_restore_write_is_reported_restored_by_peer_not_a_failure(tmp_path):
    """The race the full handler cannot express single-threaded: a peer's
    O_EXCL write lands BETWEEN this call's present-set read and its own
    O_EXCL attempt. Exercised directly against `_restore_one`, the unit the
    handler's restore loop calls once per candidate."""
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    write_anchor(common_dir, "peer.md", _FAKE_SHA_A, b"peer body\n")
    inbox = repo / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "peer.md").write_text("peer body\n", encoding="utf-8")  # a peer got there first

    blob_sha = _anchor_for(common_dir, "peer.md")[0][2]
    outcome = memo_heal_module._restore_one(repo, "peer.md", blob_sha, common_dir)

    assert outcome.ok is True
    assert outcome.restored_by_peer is True
    assert outcome.reason is None


# ---------------------------------------------------------------------------
# A refused update_refs_stdin transaction is reported, not retried -- the
# NEXT invocation of this op converges (module negative-spec). Review:
# overengineering-reviewer -- the prior retry-once + re-derive layer was
# redundant with that same convergence guarantee, and was removed.
# ---------------------------------------------------------------------------


def test_refused_transaction_is_reported_without_retrying(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    _commit_file(repo, "state/cross-repo/inbox/refused.md", "body\n", "deliver refused.md")

    calls = {"n": 0}

    def _always_refuse(cwd, commands):
        calls["n"] += 1
        # Review: code-reviewer F4 -- pin the boundary contract mechanically:
        # `update_refs_stdin` is documented as `Sequence[Tuple[str, str, str]]`,
        # not the formatted-string shape an intermediate commit in this
        # slice's own history briefly regressed to (Finding 3).
        assert all(isinstance(c, tuple) and len(c) == 3 for c in commands)
        return git_native.GitResult(returncode=128, stdout="", stderr="lock contention")

    monkeypatch.setattr(git_native, "update_refs_stdin", _always_refuse)

    result = _run(repo, dry_run=False)

    assert calls["n"] == 1, "expected exactly one attempt, no retry"
    assert any(f["id"] == "refused.md" for f in result["failed"])
    assert not any(a.get("action") == "adopted" for a in result["acted"])


# ---------------------------------------------------------------------------
# Review: apm A1 (EM-adjudicated) -- a restore re-keys its anchor: restore
# -> receiver deletes the restored memo and commits -> heal twice. The memo
# must not be restored a second time, and its anchor must be gone.
# ---------------------------------------------------------------------------


def test_restore_rekeys_so_a_deliberate_later_delete_is_retired_not_restored_again(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    write_anchor(common_dir, "twice.md", _FAKE_SHA_A, b"twice body\n")

    first = _run(repo, dry_run=False)
    assert {"id": "twice.md", "action": "restored"} in first["acted"]
    restored_path = repo / "state" / "cross-repo" / "inbox" / "twice.md"
    assert restored_path.exists()
    triples = _anchor_for(common_dir, "twice.md")
    assert len(triples) == 1
    real_restore_sha = triples[0][1]
    assert real_restore_sha != _FAKE_SHA_A

    # The receiver now deliberately deletes the restored memo, on the same
    # branch, and commits that deletion.
    _delete_and_commit(repo, "state/cross-repo/inbox/twice.md", "receiver deleted the restored memo")

    second = _run(repo, dry_run=False)

    assert not any(a["id"] == "twice.md" and a["action"] == "restored" for a in second["acted"])
    assert {"id": "twice.md", "action": "retired"} in second["acted"]
    assert not restored_path.exists()
    assert _anchor_for(common_dir, "twice.md") == []


# ---------------------------------------------------------------------------
# ADOPT_CAP_PER_RUN
# ---------------------------------------------------------------------------


def test_adopt_cap_leaves_the_remainder_for_a_later_run(tmp_path):
    repo = _make_repo(tmp_path)
    common_dir = _common_dir(repo)
    n = ADOPT_CAP_PER_RUN + 5
    for i in range(n):
        path = repo / "state" / "cross-repo" / "inbox" / f"m{i:03d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"body {i}\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "deliver many")

    result = _run(repo, dry_run=False)

    adopted = [a for a in result["acted"] if a.get("action") == "adopted"]
    assert len(adopted) == ADOPT_CAP_PER_RUN
