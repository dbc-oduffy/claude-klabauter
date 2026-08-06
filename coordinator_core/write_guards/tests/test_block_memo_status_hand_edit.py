"""Behavioral tests for coordinator_core.write_guards.block_memo_status_hand_edit
-- the memo-status hard-deny guard (see the module's own docstring for the
design and the incident it closes).

End-to-end-through-the-real-entrypoint bar: `state/improvement-queue/
2026-07-25-guards-need-an-end-to-end-real-data-veri-5ca31feb2342.yaml` names
the exact failure mode a guard-that-only-unit-tests-clean avoids -- "a guard
can be present, registered, green and inert... run the guard through its
operator entrypoint against real data and confirm it REFUSES something it
should refuse." Per that bar, the primary test in this module reproduces the
ACTUAL 2026-07-26 mutation -- an Edit changing `status: open` to
`status: actioned` on a realistic inbox memo, driven through `guard.check()`
(the guard's real operator entrypoint, exactly as `write_guards/engine.py`
invokes it) against a memo body shaped like the live corpus (frontmatter
fields: title/from/to/created/status/delivery_mode/summary/kind, per
`cross-repo/inbox/2026-07-26-example-cockpit-repo-em-guard-title-false-positive-and-validator-rehoming.md`)
-- not a bare unit call on an internal helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_memo_status_hand_edit as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_MEMO_STATUS_HAND_EDIT"

# Shaped like the live corpus, e.g.
# cross-repo/inbox/2026-07-26-example-cockpit-repo-em-guard-title-false-positive-and-validator-rehoming.md
_INBOX_MEMO_OPEN = """---
title: "Indirection guard rejects prose in a memo title"
from: "example-cockpit-repo-em"
to: "claude-klabauter-em"
created: 2026-07-26
status: open
delivery_mode: receiver-repo
summary: "Guard matches command text against --title prose."
kind: "ask"
---

Two findings from completing cockpit's shell-script migration today.
"""

_OUTBOX_DRAFT_OPEN = """---
title: "Central-owned bugs in claude-klabauter backlog"
from: "claude-klabauter-em"
to: "claude-central-em"
created: 2026-07-25
status: open
kind: "ask"
---

Draft body, hand-authored before send.
"""


def _resolve_root_for(repo_root: Path):
    def _fake(cwd):
        return str(repo_root)

    return _fake


def _payload(repo_root: Path, rel_file_path: str, tool_name: str = "Edit", **tool_input_extra) -> dict:
    tool_input = {"file_path": rel_file_path}
    tool_input.update(tool_input_extra)
    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(repo_root),
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


@pytest.fixture(autouse=True)
def _default_no_live_claim(monkeypatch):
    """Every test gets "no live claim on this memo" by default (the common
    case in this test suite, which never creates a claim dir) — the reshape
    gate now requires a live claim to reach the deny leg, so a test that
    wants the deny path monkeypatches `guard._has_live_claim` back to
    True explicitly (see TestActualIncidentMutationDenied)."""
    monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: False)


def _write_memo(tmp_path: Path, rel_dir: str, name: str, body: str) -> Path:
    memo_dir = tmp_path / rel_dir
    memo_dir.mkdir(parents=True, exist_ok=True)
    memo_path = memo_dir / name
    memo_path.write_text(body, encoding="utf-8")
    return memo_path


# ---------------------------------------------------------------------------
# 1. The actual 2026-07-26 mutation: open -> actioned via Edit on an inbox
#    memo, driven through the real operator entrypoint.
# ---------------------------------------------------------------------------


class TestActualIncidentMutationDenied:
    def test_inbox_status_open_to_actioned_edit_is_denied_under_a_live_claim(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-cockpit-repo-em-guard-title-false-positive-and-validator-rehoming.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "resolve-memo" in reason

    def test_archive_status_open_to_actioned_edit_is_denied_under_a_live_claim(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path
        memo_name = "2026-07-20-some-sender-some-topic.md"
        _write_memo(tmp_path, "cross-repo/archive", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)

        payload = _payload(
            repo_root,
            f"cross-repo/archive/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# 1b. Reshape (2026-08-06): gate on a LIVE CLAIM. The same mutation, with no
#     live session holding the memo's claim, is no longer blocked — it
#     degrades to an advisory offer instead. Both directions pinned.
# ---------------------------------------------------------------------------


class TestLiveClaimGate:
    def test_inbox_status_edit_with_no_live_claim_is_advisory_not_denied(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        # No live claim (the autouse fixture default) — the over-fire case
        # this reshape closes.

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        result = guard.check(payload)

        assert result is not None
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert "additionalContext" in out
        assert "resolve-memo" in out["additionalContext"]

    def test_inbox_status_edit_with_live_claim_is_denied(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_has_live_claim_reads_the_memo_claims_convention(self, tmp_path, monkeypatch):
        """Real `_has_live_claim` (not monkeypatched): a claim dir that does
        not exist on disk means no live claim, fail-open, regardless of
        `cs_claim_holder_live`."""
        monkeypatch.undo()  # this test exercises the REAL _has_live_claim,
        # not the autouse fixture's stub — undo it before re-patching below.
        common_dir = tmp_path / ".git"
        common_dir.mkdir()
        monkeypatch.setattr(guard, "_resolve_git_common_dir", lambda cwd: str(common_dir))

        assert guard._has_live_claim(str(tmp_path), "some-memo.md") is False

        claim_dir = common_dir / "coordinator-sessions" / "memo-claims" / "some-memo.md"
        claim_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "coordinator_core.liveness.cs_claim_holder_live", lambda claim_path: True
        )
        assert guard._has_live_claim(str(tmp_path), "some-memo.md") is True

        monkeypatch.setattr(
            "coordinator_core.liveness.cs_claim_holder_live", lambda claim_path: False
        )
        assert guard._has_live_claim(str(tmp_path), "some-memo.md") is False

        def _raise(claim_path):
            raise RuntimeError("indeterminate liveness read")

        monkeypatch.setattr("coordinator_core.liveness.cs_claim_holder_live", _raise)
        # Fails open on error — this guard's established discipline.
        assert guard._has_live_claim(str(tmp_path), "some-memo.md") is False


# ---------------------------------------------------------------------------
# 1c. Reshape (2026-08-06): ANCHOR THE MATCHER. A body-prose line that
#     merely starts with "status:" is not a genuine frontmatter touch — it
#     is never a substring of the real on-disk frontmatter block.
# ---------------------------------------------------------------------------


_INBOX_MEMO_WITH_STATUS_LOOKING_BODY_LINE = """---
title: "Body prose that echoes a frontmatter field name"
from: "example-cockpit-repo-em"
to: "claude-klabauter-em"
created: 2026-07-26
status: open
delivery_mode: receiver-repo
summary: "Guard matches command text against --title prose."
kind: "ask"
---

status: quoted from the incident transcript, not a frontmatter field.
"""


class TestMatcherAnchoredToRealFrontmatter:
    def test_body_line_that_starts_with_status_colon_is_not_denied_or_advised(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(
            tmp_path,
            "cross-repo/inbox",
            memo_name,
            _INBOX_MEMO_WITH_STATUS_LOOKING_BODY_LINE,
        )
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        # Even with a live claim present, an unanchored body-prose match
        # must not fire at all — this is an anchoring fix, not a class fix.
        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: quoted from the incident transcript, not a frontmatter field.",
            new_string="status: quoted from the incident transcript (revised), not a frontmatter field.",
        )

        assert guard.check(payload) is None

    def test_frontmatter_block_helper_excludes_delimiters_and_body(self):
        text = "---\na: 1\nstatus: open\n---\nbody text\nstatus: not-real\n"
        block = guard._frontmatter_block(text)
        assert block == "a: 1\nstatus: open\n"
        assert "body text" not in block
        assert "not-real" not in block

    def test_frontmatter_block_helper_none_when_no_fence(self):
        assert guard._frontmatter_block("no frontmatter here\nstatus: open\n") is None
        assert guard._frontmatter_block(None) is None


# ---------------------------------------------------------------------------
# 2. Design-as-offers: the deny leads with the op, spells out the
#    disposition flags.
# ---------------------------------------------------------------------------


class TestDenyOffersOp:
    def test_deny_reason_offers_resolve_verb_and_disposition_flags(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        # The message is capped to 220 measured prose bytes
        # (docs/plans/2026-08-02-guard-message-size-discipline.md); it keeps
        # only the runnable alternative, not the full disposition-flag menu.
        assert "resolve-memo" in reason
        assert "--actioned-note" in reason

    def test_deny_reason_names_a_subcommand_archive_stamp_cli_actually_advertises(
        self, tmp_path, monkeypatch
    ):
        """Regression guard for the 2026-08-01 cross-repo memo finding: the deny
        text named `memo.transition` verb `resolve` with no runnable CLI binding
        (archive-stamp-cli trampolined claim/action/release but not resolve).
        Asserts the deny text's runnable command line -- `archive-stamp-cli
        resolve-memo ...` -- names a subcommand the CLI's own advertised
        _SUBCOMMANDS string actually lists, so remediation stays reachable if
        either side drifts."""
        import importlib.util
        import sys
        from importlib.machinery import SourceFileLoader

        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        assert "archive-stamp-cli resolve-memo" in reason

        cli_path = (
            Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "archive-stamp-cli"
        )
        assert cli_path.is_file(), f"archive-stamp-cli not found at {cli_path}"
        loader = SourceFileLoader("archive_stamp_cli_under_test", str(cli_path))
        spec = importlib.util.spec_from_file_location(
            "archive_stamp_cli_under_test", cli_path, loader=loader
        )
        assert spec is not None and spec.loader is not None
        cli_mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cli_mod
        try:
            spec.loader.exec_module(cli_mod)
            assert "resolve-memo" in cli_mod._SUBCOMMANDS
            assert "resolve-memo" in cli_mod._SUBCOMMAND_USAGE
        finally:
            sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# 3. Negatives: outbox draft edit passes, body-only edit passes.
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_outbox_draft_status_edit_passes(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "central-owned-bugs-in-claude-klabauter-backlog.md"
        _write_memo(tmp_path, "state/memo-outbox", memo_name, _OUTBOX_DRAFT_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        payload = _payload(
            repo_root,
            f"state/memo-outbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        assert guard.check(payload) is None

    def test_body_only_edit_on_inbox_memo_passes(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="Two findings from completing cockpit's shell-script migration today.",
            new_string="Two findings from completing cockpit's shell-script migration today, revised.",
        )

        assert guard.check(payload) is None

    def test_new_memo_creation_passes(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        (repo_root / "cross-repo" / "inbox").mkdir(parents=True)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        payload = _payload(
            repo_root,
            "cross-repo/inbox/2026-07-26-brand-new-memo.md",
            tool_name="Write",
            content=_INBOX_MEMO_OPEN,
        )

        assert guard.check(payload) is None

    def test_multiedit_status_touch_is_denied_and_body_only_multiedit_passes(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        memo_path = _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        denied_payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "edits": [
                    {
                        "file_path": f"cross-repo/inbox/{memo_name}",
                        "old_string": "status: open",
                        "new_string": "status: actioned",
                    }
                ]
            },
            "cwd": str(repo_root),
        }
        assert guard.check(denied_payload) is not None

        passthrough_payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "edits": [
                    {
                        "file_path": f"cross-repo/inbox/{memo_name}",
                        "old_string": "kind: \"ask\"",
                        "new_string": "kind: \"fyi\"",
                    }
                ]
            },
            "cwd": str(repo_root),
        }
        # `kind:` is not `status:` — out of this guard's field scope.
        assert guard.check(passthrough_payload) is None

    def test_write_with_unchanged_status_value_passes(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        new_content = _INBOX_MEMO_OPEN.replace(
            "Two findings from completing cockpit's shell-script migration today.",
            "Two findings from completing cockpit's shell-script migration today, revised.",
        )
        assert "status: open" in new_content

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Write",
            content=new_content,
        )

        assert guard.check(payload) is None

    def test_write_with_changed_status_value_is_denied(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        monkeypatch.setattr(guard, "_has_live_claim", lambda cwd, memo_filename: True)
        new_content = _INBOX_MEMO_OPEN.replace("status: open", "status: actioned")

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Write",
            content=new_content,
        )

        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_case_varied_inbox_path_is_still_in_scope(self):
        """macOS (APFS default) and Windows are case-insensitive filesystems;
        Path.resolve(strict=False) does not case-normalise a non-existent
        path, so a candidate typed with different casing must still pass the
        path-shape and containment gates or the guard is silently bypassed
        by casing alone (the actual write still lands on the real memo)."""
        git_root = "/repo"

        gated = guard._normalize_and_gate("Cross-Repo/Inbox/2026-07-26-x.md", git_root)
        assert gated is not None

    def test_memo_record_regex_is_case_insensitive(self):
        assert guard._MEMO_RECORD_RE.search("Cross-Repo/Inbox/2026-07-26-x.md")
        assert guard._MEMO_RECORD_RE.search("cross-repo/ARCHIVE/2026-07-26-x.md")

    def test_override_env_set_passes_through(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        memo_name = "2026-07-26-example-memo.md"
        _write_memo(tmp_path, "cross-repo/inbox", memo_name, _INBOX_MEMO_OPEN)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv(_OVERRIDE_ENV, "1")

        payload = _payload(
            repo_root,
            f"cross-repo/inbox/{memo_name}",
            tool_name="Edit",
            old_string="status: open",
            new_string="status: actioned",
        )

        assert guard.check(payload) is None
