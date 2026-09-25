"""
Tests for memo.send's `cc:` DELIVERY leg — klabauter#46 second half.

The `notice` kind (klabauter#46's other half) is already fully wired
(`memo_kinds.VALID_KINDS` + `coordinator/bin/cross-repo-memo.py`'s mirror) and
is out of scope here. `_stamp_cc`/`_validate_cc` (the "already stamped into
the to: copy" cosmetic half) already ship in `memo_send.py`; this file covers
what they did NOT cover — a cc'd receiver's OWN inbox actually receives a
copy, resolved the same way `to:` is resolved, never silently dropped.

Reproduction confirmed by reading `memo_send.py` before this fix: `_stamp_cc`
only edits the frontmatter of the ONE file already being written to
`_resolve_receiver_inbox(to)`'s target; `_memo_send` called
`_resolve_receiver_inbox` exactly once (for `to`) and opened exactly one
`target_file` via O_EXCL — no second receiver's inbox was ever resolved or
written, so a cc'd peer's inbox got nothing. This file's tests fail against
that code (no second receiver-repo file materializes) and pass against the
fix in this same PR (`_resolve_cc_targets` / `_deliver_cc_copy`).

Harness/fixtures follow `test_memo_send.py`'s pattern exactly (real temp git
repos, CLAUDE_HOME-monkeypatched machine-local registry) — reused via direct
import rather than duplicated, since this file's only job is the cc leg.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_send import _MODE, _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _write_draft_with_cc(
    sender_repo: Path, topic: str, *,
    to: str = "example-retrieval-repo-em", cc=None, title: str = "A cc'd memo",
    summary: str = "a one-line summary", kind: str = "fyi",
    sent_by: str = "d218a65c-2c5b-472e-879c-ae9ed1747030",
    body: str = "Body prose.\n",
) -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    cc_line = ""
    if cc is not None:
        if isinstance(cc, str):
            cc_line = f'cc: "{cc}"\n'
        else:
            rendered = ", ".join(f'"{c}"' for c in cc)
            cc_line = f"cc: [{rendered}]\n"
    content = (
        "---\n"
        f'title: "{title}"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        f"{cc_line}"
        "created: 2026-08-25\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        f'summary: "{summary}"\n'
        f'kind: "{kind}"\n'
        f'sent_by: "{sent_by}"\n'
        "---\n\n"
        f"{body}"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


def _inbox_files(receiver_repo: Path) -> list[Path]:
    return [
        p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
        if p.name != ".gitkeep"
    ]


class TestCcActuallyDelivers:
    def test_single_cc_receiver_gets_its_own_inbox_copy(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
        claude_home = _make_claude_home(
            tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft_with_cc(
            sender_repo, "cc-topic", to="example-retrieval-repo-em", cc="example-cockpit-repo-em",
        )

        result = _memo_send({"dry_run": False, "topic": "cc-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        assert result["failed"] == []

        to_files = _inbox_files(to_repo)
        cc_files = _inbox_files(cc_repo)
        assert len(to_files) == 1, "to: receiver must still get its copy"
        assert len(cc_files) == 1, (
            "the cc: receiver's OWN inbox must receive a copy — this is the "
            "symptom klabauter#46's second half reports: it did not, before "
            "this fix"
        )

        to_content = to_files[0].read_text(encoding="utf-8")
        cc_content = cc_files[0].read_text(encoding="utf-8")
        assert 'cc: "example-cockpit-repo-em"' in to_content or "cc: example-cockpit-repo-em" in to_content
        # The cc receiver's own copy carries the same delivered content.
        assert cc_content == to_content

        # Committed durably in the cc receiver's own tree, not merely written.
        show = _git(cc_repo, "show", f"HEAD:cross-repo/inbox/{cc_files[0].name}")
        assert show.stdout.decode("utf-8") == cc_content

        acted = result["acted"][0]
        assert "cc_delivered" in acted
        assert len(acted["cc_delivered"]) == 1
        assert acted["cc_delivered"][0]["to"] == "example-cockpit-repo-em"
        assert acted["cc_delivered"][0]["committed"] is True

    def test_multiple_cc_receivers_each_get_a_copy(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        cc_repo_a = _make_receiver_git_repo(tmp_path, name="cc-repo-a")
        cc_repo_b = _make_receiver_git_repo(tmp_path, name="cc-repo-b")
        claude_home = _make_claude_home(
            tmp_path,
            {
                "project_rag": to_repo,
                "example_cockpit_repo": cc_repo_a,
                "example_store_repo": cc_repo_b,
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft_with_cc(
            sender_repo, "cc-multi-topic", to="example-retrieval-repo-em",
            cc=["example-cockpit-repo-em", "example-store-repo-em"],
        )

        result = _memo_send(
            {"dry_run": False, "topic": "cc-multi-topic"}, repo_root=sender_repo
        )

        assert result["exit_code"] == 0, result
        assert len(_inbox_files(to_repo)) == 1
        assert len(_inbox_files(cc_repo_a)) == 1
        assert len(_inbox_files(cc_repo_b)) == 1
        assert len(result["acted"][0]["cc_delivered"]) == 2


class TestUnresolvableCcRefusesTheWholeSendBeforeAnyWrite:
    def test_unresolvable_cc_name_refuses_loud_and_writes_nothing(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        claude_home = _make_claude_home(tmp_path, {"project_rag": to_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft_with_cc(
            sender_repo, "cc-unresolvable-topic",
            to="example-retrieval-repo-em", cc="nobody-registered-em",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "cc-unresolvable-topic"}, repo_root=sender_repo
        )

        assert result["exit_code"] == 1, result
        assert result["mode"] == _MODE
        # Nothing was written anywhere -- to:'s own inbox included, matching
        # to:'s own UNKNOWN RECEIVER discipline: an unresolvable receiver
        # refuses BEFORE any byte moves, never a partial send.
        assert _inbox_files(to_repo) == []
        assert (sender_repo / "state" / "memo-outbox" / "cc-unresolvable-topic.md").exists(), (
            "an unresolvable cc: must refuse before the draft is moved/sent"
        )

    def test_unresolvable_cc_never_silently_dropped(self, tmp_path, monkeypatch, capsys=None):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        claude_home = _make_claude_home(tmp_path, {"project_rag": to_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft_with_cc(
            sender_repo, "cc-unresolvable-topic-2",
            to="example-retrieval-repo-em", cc="also-nobody-em",
        )

        result = _memo_send(
            {"dry_run": True, "topic": "cc-unresolvable-topic-2"}, repo_root=sender_repo
        )

        # dry_run is a preview too -- the refusal must fire there as well
        # (mirrors to:'s own UNKNOWN RECEIVER firing before the dry_run
        # branch), never silently previewing a send that will drop cc.
        assert result["exit_code"] == 1, result


class TestCcDeliveryFailureAfterToLandedIsPartialNotSilent:
    def test_cc_inbox_collision_reports_partial_failure_but_to_still_delivers(
        self, tmp_path, monkeypatch,
    ):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
        claude_home = _make_claude_home(
            tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft_with_cc(
            sender_repo, "cc-collision-topic",
            to="example-retrieval-repo-em", cc="example-cockpit-repo-em",
        )

        # Pre-seed the exact filename memo.send will derive in the cc
        # receiver's own inbox, forcing the cc leg's own collision refusal —
        # independent of to:'s own no-clobber check.
        today = __import__("datetime").date.today().isoformat()
        collide_name = f"{today}-claude-klabauter-engine-cc-collision-topic.md"
        (cc_repo / "cross-repo" / "inbox" / collide_name).write_text(
            "pre-existing\n", encoding="utf-8",
        )
        _git(cc_repo, "add", "-A")
        _git(cc_repo, "commit", "-m", "pre-seed collision")

        result = _memo_send(
            {"dry_run": False, "topic": "cc-collision-topic"}, repo_root=sender_repo
        )

        # to: still lands -- a downstream cc failure must never undo an
        # already-landed to: delivery.
        assert len(_inbox_files(to_repo)) == 1
        assert result["exit_code"] == 2, result
        assert len(result["failed"]) == 1
        assert "collision" in result["failed"][0]["reason"]
        assert result["failed"][0]["to"] == "example-cockpit-repo-em"


class TestCcNeverReachesAPublishMirror:
    def test_cc_naming_an_unregistered_publish_mirror_style_name_refuses_as_unknown_receiver(
        self, tmp_path, monkeypatch,
    ):
        """A publish mirror is excluded from `repos.*` by construction
        (module docstring's registry note) -- `_resolve_cc_targets` reuses
        the exact same `_resolve_receiver_inbox` `to:` uses, so a cc name
        that only exists as a `publish.mirrors.*` entry (never `repos.*`)
        resolves to `(None, None, ...)` here exactly as it would for `to:`,
        and refuses the whole send loud rather than ever writing into it.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        claude_home = _make_claude_home(tmp_path, {"project_rag": to_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft_with_cc(
            sender_repo, "cc-mirror-topic",
            to="example-retrieval-repo-em", cc="coordinator-claude-em",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "cc-mirror-topic"}, repo_root=sender_repo
        )

        assert result["exit_code"] == 1, result
        assert _inbox_files(to_repo) == []


class TestPublishMirrorAddressesRouteToTheOwner:
    """A publish mirror is not a receiver. It is registered in `repos.*` so tools can
    find it, and `to:`/`cc:` naming it used to deliver into its own inbox, where the
    next publish clobbers it. Both legs deliver to the mirror's owner instead."""

    def _home_with_mirror(self, tmp_path, monkeypatch, owner_repo, mirror_repo, extra=None):
        repos = {"example_cockpit_repo": owner_repo, "claude_klabauter": mirror_repo, **(extra or {})}
        claude_home = _make_claude_home(tmp_path, repos)
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        mirror_path = str(mirror_repo).replace("\\", "\\\\").replace('"', '\\"')
        (machine_local / "registry.toml").write_text(
            "schema = 1\n\n[publish.mirrors.claude_klabauter]\n"
            f'owner = "example-cockpit-repo-em"\npath = "{mirror_path}"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    def test_to_a_mirror_lands_in_the_owner_inbox(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        owner_repo = _make_receiver_git_repo(tmp_path, name="owner-repo")
        mirror_repo = _make_receiver_git_repo(tmp_path, name="mirror-repo")
        self._home_with_mirror(tmp_path, monkeypatch, owner_repo, mirror_repo)
        _write_draft_with_cc(sender_repo, "mirror-to", to="claude-klabauter-em")

        result = _memo_send({"dry_run": False, "topic": "mirror-to"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        assert len(_inbox_files(owner_repo)) == 1
        assert _inbox_files(mirror_repo) == []

    def test_cc_a_mirror_lands_in_the_owner_inbox(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        owner_repo = _make_receiver_git_repo(tmp_path, name="owner-repo")
        mirror_repo = _make_receiver_git_repo(tmp_path, name="mirror-repo")
        self._home_with_mirror(
            tmp_path, monkeypatch, owner_repo, mirror_repo, extra={"project_rag": to_repo},
        )
        _write_draft_with_cc(sender_repo, "mirror-cc", to="example-retrieval-repo-em", cc="claude-klabauter-em")

        result = _memo_send({"dry_run": False, "topic": "mirror-cc"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        assert len(_inbox_files(to_repo)) == 1
        assert len(_inbox_files(owner_repo)) == 1
        assert _inbox_files(mirror_repo) == []
