"""
Tests for the durability contract of `memo.send` + `memo.heal_inbox` against
the receiver's OWN branch gestures (C8, docs/plans/2026-09-11-memo-
deliveries-survive-the-receiver-s-o.md — the prime exit criterion, made
permanent).

The falsifier's instrument (`docs/plans/2026-09-11-memo-deliveries-survive-
the-receiver-s-o.md` § falsifier.how), scripted for real: a tmp_path sender
and receiver, an isolated machine-local registry via
`COORDINATOR_SETTINGS_HOME`, real `memo.send` deliveries, and real git
subprocesses (rename/recreate, hard-reset, branch delete, `gc --prune=now`)
run against the receiver to reproduce the incident before proving the heal
undoes it.

(Review: eng-director F2 — the original sequence archived one memo on the
branch the gestures then destroyed with no heal pass in between, so under
the plan's own reachability rule that memo would have been RESTORED and its
anchor never retired — the assertions would have contradicted the design
they meant to prove. The primary test below heals BEFORE the gestures, adds
a deleted memo (D) to exercise the "reachable from HEAD" retire branch, and
the two extra cases this module also carries are the corrected rule's own
requirement: an archive with no intervening heal restores rather than
retires, and a memo reachable only from a branch the gestures never touched
is neither restored nor retired.)

(Review: apm A1, applied per EM adjudication) `test_restore_then_delete_is_
not_resurrected_a_second_time` covers the observation the falsifier was
missing on its own: a restore whose anchor is not re-keyed would resurrect a
deliberately-deleted memo forever. `memo_heal`'s own re-key step (see
`test_memo_heal.py`) is what this test proves holds under the SAME gesture
family this module's primary test exercises.

Harness: real git subprocesses under `tmp_path` (mirrors `test_memo_send.py`
and `test_memo_heal.py`'s own pattern) — the gestures run from THIS process
because subagent guards refuse commit/reset even in a temp repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict

import pytest

from coordinator_core.ops.fleet._memo_anchor import anchor_names
from coordinator_core.ops.fleet.memo_heal import _memo_heal_inbox
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Git repo + registry factories (mirrors test_memo_send.py / test_memo_heal.py)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check,
        **no_console_creationflags(),
    )


def _rev_parse(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev).stdout.strip()


def _make_sender_repo(tmp_path: Path, name: str = "sender-repo") -> Path:
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


def _make_receiver_repo(tmp_path: Path, name: str = "receiver-repo") -> Path:
    """A root-level `.gitkeep` only -- NOT one inside `cross-repo/inbox/`
    itself (`memo.send` creates that directory on demand). A tracked
    `inbox/.gitkeep` would sit in `heal_inbox`'s present-map as an
    unanchored, tracked file and get swept into ADOPT, where `_memo_anchor`
    refuses it outright (a leading `.` fails `_valid_ref_component`),
    failing the WHOLE adopt+retire+rekey transaction for every real memo
    batched alongside it."""
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init receiver")
    return root


def _make_settings_home(tmp_path: Path, receiver_repos: Dict[str, Path], name: str = "settings-home") -> Path:
    """A COORDINATOR_SETTINGS_HOME root -- `machine-local/registry.toml` +
    `registry.local.toml` directly underneath, no `.coordinator-claude-
    settings` wrapper (that wrapper is what CLAUDE_HOME implies; pointing
    COORDINATOR_SETTINGS_HOME straight at the settings-home root, per this
    plan's falsifier.how, skips it)."""
    home = tmp_path / name
    machine_local = home / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return home


def _write_draft(sender_repo: Path, topic: str, *, to: str = "example-retrieval-repo-em", body: str = "Body prose.\n") -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    content = (
        "---\n"
        f'title: "{topic}"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        "created: 2026-09-11\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "fyi"\n'
        'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n'
        "---\n\n"
        f"{body}"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


def _deliver(sender_repo: Path, topic: str, *, to: str = "example-retrieval-repo-em", body: str = "Body prose.\n") -> dict:
    """Draft + `memo.send`, act mode. Returns the op result; asserts success
    (every call site in this module wants a landed delivery, not a partial
    one to inspect)."""
    _write_draft(sender_repo, topic, to=to, body=body)
    result = _memo_send({"dry_run": False, "topic": topic}, repo_root=sender_repo)
    assert result["exit_code"] == 0, result
    return result


def _delivered_filename(acted_item: dict) -> str:
    return Path(acted_item["id"]).name


def _heal(receiver_repo: Path, *, dry_run: bool = False) -> dict:
    """Every real `memo.heal_inbox` invocation this module models is a
    SEPARATE process (the falsifier's own `how` runs the housekeeping door
    as a fresh subprocess each time) -- unlike this in-process harness,
    which otherwise leaks `git_objects._OBJECT_CACHE`'s by-sha memoization
    across calls in the SAME process. That cache is sound for a sha that
    keeps existing (content-addressed, cannot change under its own key,
    per that module's own negative-spec) but goes stale the moment a call
    IN BETWEEN destroys the very object a prior call cached as present --
    exactly this module's own gesture-then-heal sequence. Clearing it here
    (a test reaching into the module's own private cache, not editing it --
    mirrors `test_pickup_assemble_git_readmodel_parity.py`'s identical
    `go._OBJECT_CACHE.clear()`) makes each `_heal()` call see what a fresh
    process would."""
    from coordinator_core.git import git_objects as _git_objects

    _git_objects._OBJECT_CACHE.clear()
    return _memo_heal_inbox({"dry_run": dry_run}, repo_root=receiver_repo)


def _archive_and_commit(receiver_repo: Path, filename: str) -> None:
    (receiver_repo / "cross-repo" / "archive").mkdir(parents=True, exist_ok=True)
    _git(
        receiver_repo, "mv",
        f"cross-repo/inbox/{filename}", f"cross-repo/archive/{filename}",
    )
    _git(receiver_repo, "commit", "-m", f"archive {filename}")


def _delete_and_commit(receiver_repo: Path, relpath: str) -> None:
    (receiver_repo / relpath).unlink()
    _git(receiver_repo, "add", "-A")
    _git(receiver_repo, "commit", "-m", f"delete {relpath}")


def _inbox_names(receiver_repo: Path) -> set:
    """Both corpus roots, unioned -- matches `memo_heal._present_map`'s own
    convention. Which root is live can change mid-test: `memo.send`'s
    target (`memo_corpus.receiver_inbox_root`) falls back to the LEGACY
    `cross-repo/` when neither root exists yet, but a restore's target
    (`memo_corpus.memo_corpus_root`) mints the NEW `state/cross-repo/` when
    neither exists -- so a delivery lands in one root and a later restore,
    run against a tree a destructive gesture has emptied back to neither-
    exists, lands in the other."""
    names = set()
    for corpus in ("cross-repo", "state/cross-repo"):
        inbox = receiver_repo / corpus / "inbox"
        if inbox.is_dir():
            names.update(p.name for p in inbox.glob("*.md"))
    return names


def _resolved_inbox_path(receiver_repo: Path, filename: str) -> Path:
    """The file's ACTUAL path under whichever corpus root
    `memo_corpus.memo_corpus_root` currently resolves to -- the same
    resolver `memo_heal._inbox_write_target` uses for a restore. Needed
    anywhere this module inspects or mutates a file AFTER a gesture has
    emptied the tree back to neither-root-exists, since a restore that
    follows mints the NEW root even though the original delivery landed
    under the legacy one."""
    from coordinator_core import memo_corpus

    return Path(memo_corpus.memo_corpus_root(str(receiver_repo))) / "inbox" / filename


def _anchor_filenames(common_dir: Path) -> set:
    return {t[0] for t in anchor_names(common_dir)}


def _object_present(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", sha, check=False).returncode == 0


# ---------------------------------------------------------------------------
# The gesture family the prime exit criterion names, each ending on branch
# `main` at `root_sha`, the branch that carried every prior commit gone, and
# unreferenced objects reclaimed.
# ---------------------------------------------------------------------------


def _gesture_rename_and_recreate(repo: Path, root_sha: str) -> None:
    _git(repo, "branch", "-m", "main", "old")
    _git(repo, "branch", "main", root_sha)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "reset", "--hard", root_sha)
    _git(repo, "branch", "-D", "old")
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")


def _gesture_reset_hard_only(repo: Path, root_sha: str) -> None:
    _git(repo, "reset", "--hard", root_sha)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")


def _gesture_branch_delete_after_switch(repo: Path, root_sha: str) -> None:
    _git(repo, "checkout", "-b", "tmp", root_sha)
    _git(repo, "branch", "-D", "main")
    _git(repo, "branch", "-m", "tmp", "main")
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")


_GESTURES = {
    "rename-and-recreate": _gesture_rename_and_recreate,
    "reset-hard-only": _gesture_reset_hard_only,
    "branch-delete-after-switch": _gesture_branch_delete_after_switch,
}


# ---------------------------------------------------------------------------
# The prime exit criterion: deliver, destroy, gc, heal, back -- archived and
# deleted stay gone; a restore's own anchor doesn't resurrect a later delete.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gesture", _GESTURES.values(), ids=_GESTURES.keys())
def test_prime_exit_criterion_deliver_destroy_gc_heal_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gesture: Callable[[Path, str], None],
):
    sender = _make_sender_repo(tmp_path)
    receiver = _make_receiver_repo(tmp_path)
    root_sha = _rev_parse(receiver, "HEAD")
    settings_home = _make_settings_home(tmp_path, {"example_retrieval_repo": receiver})
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    result_a = _deliver(sender, "memo-a")
    result_b = _deliver(sender, "memo-b")
    result_d = _deliver(sender, "memo-d")
    fname_a = _delivered_filename(result_a["acted"][0])
    fname_b = _delivered_filename(result_b["acted"][0])
    fname_d = _delivered_filename(result_d["acted"][0])
    delivery_sha_a = result_a["acted"][0]["delivery_commit_sha"]
    delivered_bytes_a = (receiver / "cross-repo" / "inbox" / fname_a).read_bytes()

    # In the receiver: archive B, delete D, each committed.
    _archive_and_commit(receiver, fname_b)
    _delete_and_commit(receiver, f"cross-repo/inbox/{fname_d}")

    # One heal BEFORE the gestures -- retires B (archived) and D (reachable
    # from HEAD, deliberately removed).
    heal_1 = _heal(receiver)
    assert heal_1["exit_code"] == 0, heal_1
    assert {"id": fname_b, "action": "retired"} in heal_1["acted"]
    assert {"id": fname_d, "action": "retired"} in heal_1["acted"]

    # The gestures + gc -- proves the test destroyed what the incident
    # destroyed.
    gesture(receiver, root_sha)
    assert not _object_present(receiver, delivery_sha_a)

    # A second heal: A comes back; B and D stay gone.
    heal_2 = _heal(receiver)
    assert heal_2["exit_code"] == 0, heal_2
    assert {"id": fname_a, "action": "restored"} in heal_2["acted"]
    # The gesture emptied the tree back to neither-corpus-root-exists, so
    # the restore mints the NEW root -- `state/cross-repo/`, not the
    # `cross-repo/` the original delivery landed under.
    restored_path = _resolved_inbox_path(receiver, fname_a)
    assert restored_path.read_bytes() == delivered_bytes_a
    restored_relpath = restored_path.relative_to(receiver).as_posix()
    show = _git(receiver, "show", f"HEAD:{restored_relpath}")
    assert show.stdout.encode("utf-8").replace(b"\r\n", b"\n") == delivered_bytes_a.replace(b"\r\n", b"\n")

    assert _inbox_names(receiver) == {fname_a}
    anchored = _anchor_filenames(_common_dir(receiver))
    assert fname_a in anchored
    assert fname_b not in anchored
    assert fname_d not in anchored

    # A third heal is a genuine no-op with zero spawns. Patched by hand
    # (not via `monkeypatch`) so restoring it does not also undo the
    # COORDINATOR_SETTINGS_HOME env-var patch this test still needs below.
    def _forbidden(*a, **k):
        raise AssertionError("no git subprocess expected on a converged heal")

    orig_run = subprocess.run
    subprocess.run = _forbidden
    try:
        heal_3 = _heal(receiver)
    finally:
        subprocess.run = orig_run
    assert heal_3["exit_code"] == 0, heal_3
    assert heal_3["acted"] == []
    assert heal_3["failed"] == []

    # (Review: apm A1) restore-then-delete -- A's restore re-keyed the
    # anchor to the restore commit; deleting A on the branch the receiver
    # KEPT (this one -- no further gesture) and healing twice must not bring
    # it back a second time, and must retire its anchor for good. Delete
    # via the resolved path -- the restore minted the NEW corpus root, so
    # A no longer lives under the legacy one the original delivery used.
    _delete_and_commit(receiver, restored_relpath)
    heal_4 = _heal(receiver)
    assert {"id": fname_a, "action": "retired"} in heal_4["acted"]
    heal_5 = _heal(receiver)
    assert heal_5["acted"] == []
    assert heal_5["failed"] == []
    assert not restored_path.exists()
    assert fname_a not in _anchor_filenames(_common_dir(receiver))


def _common_dir(repo: Path) -> Path:
    return repo / ".git"


# ---------------------------------------------------------------------------
# Extra case 1 (Review: eng-director F2): archived on the branch the
# gestures destroy, with NO heal pass in between -- the archiving itself
# was lost along with the branch that carried it, so the corrected rule
# says RESTORE, never retire.
# ---------------------------------------------------------------------------


def test_archived_with_no_heal_before_the_gestures_is_restored_not_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    sender = _make_sender_repo(tmp_path)
    receiver = _make_receiver_repo(tmp_path)
    root_sha = _rev_parse(receiver, "HEAD")
    settings_home = _make_settings_home(tmp_path, {"example_retrieval_repo": receiver})
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    result_b = _deliver(sender, "memo-b")
    fname_b = _delivered_filename(result_b["acted"][0])
    delivered_bytes_b = (receiver / "cross-repo" / "inbox" / fname_b).read_bytes()

    # Archive it, but never heal before the gestures run.
    _archive_and_commit(receiver, fname_b)

    _gesture_reset_hard_only(receiver, root_sha)

    heal = _heal(receiver)
    assert heal["exit_code"] == 0, heal
    assert {"id": fname_b, "action": "restored"} in heal["acted"]
    # The gesture emptied the tree back to neither-corpus-root-exists, so
    # the restore mints the NEW root, not the legacy one the delivery used.
    restored_path = _resolved_inbox_path(receiver, fname_b)
    assert restored_path.read_bytes() == delivered_bytes_b
    assert fname_b in _anchor_filenames(_common_dir(receiver))


# ---------------------------------------------------------------------------
# Extra case 2 (Review: eng-director F2): reachable only from a SECOND live
# branch the gestures never touch -- neither restored (not unreachable from
# every branch) nor retired (not reachable from HEAD).
# ---------------------------------------------------------------------------


def test_reachable_only_from_a_kept_second_branch_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    sender = _make_sender_repo(tmp_path)
    receiver = _make_receiver_repo(tmp_path)
    root_sha = _rev_parse(receiver, "HEAD")
    settings_home = _make_settings_home(tmp_path, {"example_retrieval_repo": receiver})
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    result_e = _deliver(sender, "memo-e")
    fname_e = _delivered_filename(result_e["acted"][0])

    # A second branch, still at the tip carrying E, kept alive throughout.
    _git(receiver, "branch", "side")

    # Gesture ONLY the branch heal_inbox will run from -- `side` is never
    # deleted, so E's delivery commit stays reachable (just not from HEAD).
    _gesture_reset_hard_only(receiver, root_sha)
    assert _rev_parse(receiver, "side") != _rev_parse(receiver, "HEAD")

    heal = _heal(receiver)
    assert heal["exit_code"] == 0, heal
    acted_ids = {item["id"] for item in heal["acted"]}
    assert fname_e not in acted_ids
    assert heal["failed"] == []
    assert fname_e not in _inbox_names(receiver)
    assert fname_e in _anchor_filenames(_common_dir(receiver))
