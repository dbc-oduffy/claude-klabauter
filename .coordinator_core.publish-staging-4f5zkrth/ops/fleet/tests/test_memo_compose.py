"""
Tests for coordinator_core.ops.fleet.memo_compose — memo.compose native UDS op.

C5 test surface (docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C5, AC5):
  - footgun #4: prose-first summary derivation — a heading-first body yields the
    first PROSE sentence as the summary, not the `##` heading line.
  - an explicit `summary` param always wins over derivation.
  - dry_run vs act envelope shape; act rewrites the existing draft in place,
    status stays "draft" (memo.send owns the draft -> open promotion).
  - setup-error when no draft exists yet, or when the existing file's status
    is not "draft" (memo.compose only edits undelivered drafts).
  - store-less-ness architecture test (mirrors AC8/strang-03 C6's test_no_memo_index).

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency.

Spec backlink: pln-memo-tool-rebuild-makima-owns--bd5745 § C5
"""

from __future__ import annotations

import asyncio
import subprocess
import types
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field
from coordinator_core.ops.fleet.memo_compose import (
    _MODE,
    _memo_compose,
    _validate_compose_params,
    derive_prose_summary,
)
from coordinator_core.ops.fleet.memo_draft import _memo_draft
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS, SUMMARY_PLACEHOLDER

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(result):
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=str(repo), capture_output=True, check=check,
    )


def _make_sender_git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@makima.test")
    _git(root, "config", "user.name", "MakimaTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _draft_first(tmp_path, topic="some-topic", **overrides) -> Path:
    """Create a sender repo + a real draft via memo.draft; return the sender root."""
    sender = _make_sender_git_repo(tmp_path)
    common_dir = sender / ".git"
    params = {
        "dry_run": False,
        "topic": topic,
        "to": "project-rag-em",
        "title": "A draft memo",
    }
    params.update(overrides)
    result = _run(_memo_draft(params, repo_root=common_dir))
    assert result["exit_code"] == 0, result
    return sender


# ===========================================================================
# 1. Footgun #4 — prose-first summary derivation
# ===========================================================================

class TestDeriveProseSummary:
    def test_heading_first_body_yields_prose_not_heading(self):
        body = "## Problem statement\n\nThe registry read silently degraded to {}.\nMore detail.\n"
        summary = derive_prose_summary(body)
        assert summary == "The registry read silently degraded to {}."
        assert not summary.startswith("#")

    def test_blank_lines_skipped(self):
        body = "\n\n   \nActual prose sentence here.\n"
        assert derive_prose_summary(body) == "Actual prose sentence here."

    def test_html_comment_only_lines_skipped(self):
        body = "<!-- compose your memo here -->\nReal content starts here.\n"
        assert derive_prose_summary(body) == "Real content starts here."

    def test_multiple_headings_all_skipped(self):
        body = "# Title\n## Subtitle\n### Sub-sub\nFinally, prose.\n"
        assert derive_prose_summary(body) == "Finally, prose."

    def test_no_sentence_punctuation_falls_back_to_whole_line(self):
        body = "no terminal punctuation on this line\nnext line\n"
        assert derive_prose_summary(body) == "no terminal punctuation on this line"

    def test_truncates_long_summary(self):
        long_sentence = ("x" * 150) + "."
        result = derive_prose_summary(long_sentence + "\n")
        assert len(result) == 120
        assert result.endswith("…")

    def test_all_blank_or_structural_body_yields_empty(self):
        body = "## Heading\n<!-- comment -->\n\n"
        assert derive_prose_summary(body) == ""


# ===========================================================================
# 2. Param validation
# ===========================================================================

class TestValidateComposeParams:
    def test_missing_dry_run(self):
        result = _validate_compose_params({"topic": "t", "body": "b"})
        assert result["exit_code"] == 1

    def test_bad_topic(self):
        result = _validate_compose_params({"dry_run": True, "topic": "Bad/../x", "body": "b"})
        assert result["exit_code"] == 1

    def test_missing_body(self):
        result = _validate_compose_params({"dry_run": True, "topic": "t"})
        assert result["exit_code"] == 1

    def test_valid_params(self):
        result = _validate_compose_params({"dry_run": False, "topic": "t", "body": "b", "summary": "s"})
        assert result == (False, "t", "b", "s", None, None)

    def test_explicit_summary_over_cap_warns_and_substitutes(self, capsys):
        """2026-08-07 PM ruling (AC9, supersedes AC7 — docs/plans/2026-08-07-
        memo-summary-cap-warn-at-draft.md § C4): an EXPLICITLY authored
        summary over _SUMMARY_MAX_CHARS no longer fails loud here — it is
        set aside (never truncated) and the summary slot is sentineled to
        None so the handler falls through to derive_prose_summary(body)."""
        long_summary = "S" * (_SUMMARY_MAX_CHARS + 1)
        result = _validate_compose_params(
            {"dry_run": True, "topic": "t", "body": "b", "summary": long_summary}
        )
        dry_run, topic, body, summary, advisory, original = result
        assert (dry_run, topic, body, summary) == (True, "t", "b", None)
        assert advisory is not None
        assert str(_SUMMARY_MAX_CHARS) in advisory
        assert original == long_summary  # echoed back verbatim, in full

    def test_explicit_summary_at_cap_passes(self):
        """A summary exactly AT the cap is valid — the gate is strictly-greater-than."""
        at_cap_summary = "S" * _SUMMARY_MAX_CHARS
        result = _validate_compose_params(
            {"dry_run": False, "topic": "t", "body": "b", "summary": at_cap_summary}
        )
        assert result == (False, "t", "b", at_cap_summary, None, None)

    def test_placeholder_summary_resolves_to_absent(self):
        """AC5: a placeholder-valued summary (memo.draft's ruler) is ABSENT,
        not an explicit value — validation resolves it to None so the handler
        falls through to derive_prose_summary(body) rather than length-checking
        or writing the ruler into a delivered memo."""
        result = _validate_compose_params(
            {"dry_run": False, "topic": "t", "body": "b", "summary": SUMMARY_PLACEHOLDER}
        )
        assert result == (False, "t", "b", None, None, None)


# ===========================================================================
# 3. No existing draft -> setup error
# ===========================================================================

class TestNoExistingDraft:
    def test_compose_without_prior_draft_fails_loud(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_compose(
            {"dry_run": False, "topic": "nope", "body": "hello"}, repo_root=common_dir,
        ))
        assert result["exit_code"] == 1
        assert not (sender / "state" / "memo-outbox" / "nope.md").exists()


# ===========================================================================
# 4. dry_run preview — no write
# ===========================================================================

class TestDryRunPreview:
    def test_dry_run_previews_derived_summary_without_writing(self, tmp_path):
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"
        before = target.read_text(encoding="utf-8")

        body = "## Context\n\nThe fix lands cleanly.\n"
        result = _run(_memo_compose(
            {"dry_run": True, "topic": "some-topic", "body": body}, repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert result["dry_run"] is True
        assert result["candidates"][0]["summary"] == "The fix lands cleanly."
        assert target.read_text(encoding="utf-8") == before  # unchanged


# ===========================================================================
# 5. act path — rewrites body + summary, keeps status: draft
# ===========================================================================

class TestActRewritesDraft:
    def test_act_writes_body_and_derived_summary(self, tmp_path):
        sender = _draft_first(tmp_path, kind="ask")
        common_dir = sender / ".git"

        body = "## Heading\n\nActual prose summary sentence. More body.\n"
        result = _run(_memo_compose(
            {"dry_run": False, "topic": "some-topic", "body": body}, repo_root=common_dir,
        ))
        assert result["exit_code"] == 0

        target = sender / "state" / "memo-outbox" / "some-topic.md"
        content = target.read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert split is not None
        assert read_fm_field(split.fm_text, "status") == "draft"
        assert read_fm_field(split.fm_text, "summary") == '"Actual prose summary sentence."'
        # preserved fields survive the rewrite
        assert read_fm_field(split.fm_text, "title") == '"A draft memo"'
        assert read_fm_field(split.fm_text, "to") == '"project-rag-em"'
        assert read_fm_field(split.fm_text, "kind") == '"ask"'
        assert "Actual prose summary sentence. More body." in split.body_with_leading_newline

    def test_explicit_summary_wins_over_derivation(self, tmp_path):
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"

        body = "## Heading\n\nThis would have been the derived summary.\n"
        result = _run(_memo_compose(
            {
                "dry_run": False, "topic": "some-topic", "body": body,
                "summary": "Explicit summary wins.",
            },
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0

        target = sender / "state" / "memo-outbox" / "some-topic.md"
        split = split_frontmatter(target.read_text(encoding="utf-8"))
        assert read_fm_field(split.fm_text, "summary") == '"Explicit summary wins."'

    def test_explicit_summary_over_cap_substitutes_and_warns(self, tmp_path):
        """End-to-end (2026-08-07 PM ruling, AC9/AC10): _memo_compose SUCCEEDS
        on an over-cap explicit summary — the body-derived summary is written
        in its place, an advisory names the actual length and the cap, and
        the author's original text is echoed back verbatim. Never truncated:
        no prefix of the 163-char original appears in the emitted summary
        (the former `[:_SUMMARY_MAX_CHARS - 1] + "…"` clamp is gone)."""
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"

        body = "Derived summary sentence lands here. More body follows.\n"
        long_summary = "S" * 163
        result = _run(_memo_compose(
            {
                "dry_run": False, "topic": "some-topic", "body": body,
                "summary": long_summary,
            },
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        acted = result["acted"][0]
        assert acted["summary"] == "Derived summary sentence lands here."
        assert acted["summary_cap_advisory"] is not None
        assert str(_SUMMARY_MAX_CHARS) in acted["summary_cap_advisory"]
        assert acted["summary_over_cap_original"] == long_summary

        split = split_frontmatter(target.read_text(encoding="utf-8"))
        written_summary = read_fm_field(split.fm_text, "summary")
        assert written_summary == '"Derived summary sentence lands here."'
        # No truncation — no prefix of the over-cap original ever reaches
        # the emitted summary field (substitution, not clamping).
        assert long_summary[:20] not in written_summary

    def test_explicit_summary_over_cap_and_underivable_body_refuses(self, tmp_path, capsys):
        """C4: over-cap explicit summary + a body with no derivable prose has
        nothing to substitute — refuse (DEC-1), never deliver an empty
        summary, and name BOTH conditions in the message."""
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"
        before = target.read_text(encoding="utf-8")

        long_summary = "S" * (_SUMMARY_MAX_CHARS + 1)
        result = _run(_memo_compose(
            {
                "dry_run": False, "topic": "some-topic",
                "body": "## Heading\n<!-- comment -->\n\n",
                "summary": long_summary,
            },
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 1
        stderr = capsys.readouterr().err
        assert "over cap" in stderr
        assert "no derivable prose" in stderr
        assert target.read_text(encoding="utf-8") == before  # untouched

    def test_status_not_draft_is_setup_error(self, tmp_path):
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"

        content = target.read_text(encoding="utf-8")
        content = content.replace("status: draft", "status: open")
        target.write_text(content, encoding="utf-8")

        result = _run(_memo_compose(
            {"dry_run": False, "topic": "some-topic", "body": "new body"}, repo_root=common_dir,
        ))
        assert result["exit_code"] == 1
        # left untouched
        assert target.read_text(encoding="utf-8") == content

    def test_repeated_compose_calls_keep_status_draft(self, tmp_path):
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"

        for i in range(3):
            result = _run(_memo_compose(
                {"dry_run": False, "topic": "some-topic", "body": f"Body version {i}."},
                repo_root=common_dir,
            ))
            assert result["exit_code"] == 0

        split = split_frontmatter(target.read_text(encoding="utf-8"))
        assert read_fm_field(split.fm_text, "status") == "draft"
        assert read_fm_field(split.fm_text, "summary") == '"Body version 2."'

    def test_resolved_empty_summary_fails_loud_act(self, tmp_path):
        """DEC-1 (2026-07-24 memo-ownership-and-redesign plan): a body with no
        derivable prose and no explicit summary resolves to an empty summary
        — compose must fail loud rather than write an empty summary field."""
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"
        before = target.read_text(encoding="utf-8")

        result = _run(_memo_compose(
            {"dry_run": False, "topic": "some-topic", "body": "## Heading\n<!-- comment -->\n"},
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 1
        assert target.read_text(encoding="utf-8") == before  # untouched

    def test_placeholder_summary_derives_from_derivable_body(self, tmp_path):
        """AC5/AC6 direction 1: placeholder summary + a derivable body →
        derived summary is written, the ruler never reaches the delivered
        draft."""
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"

        result = _run(_memo_compose(
            {
                "dry_run": False, "topic": "some-topic",
                "body": "## Heading\n\nA derivable prose sentence.\n",
                "summary": SUMMARY_PLACEHOLDER,
            },
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0

        target = sender / "state" / "memo-outbox" / "some-topic.md"
        content = target.read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert read_fm_field(split.fm_text, "summary") == '"A derivable prose sentence."'
        assert SUMMARY_PLACEHOLDER not in content

    def test_placeholder_summary_with_underivable_body_still_refuses(self, tmp_path):
        """AC5/AC6 direction 2: placeholder summary + an underivable body still
        hits DEC-1's resolved-empty refusal, unweakened — the sentinel routes
        to derivation, not around DEC-1."""
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"
        target = sender / "state" / "memo-outbox" / "some-topic.md"
        before = target.read_text(encoding="utf-8")

        result = _run(_memo_compose(
            {
                "dry_run": False, "topic": "some-topic",
                "body": "## Heading\n<!-- comment -->\n",
                "summary": SUMMARY_PLACEHOLDER,
            },
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 1
        assert target.read_text(encoding="utf-8") == before  # untouched

    def test_resolved_empty_summary_fails_loud_dry_run(self, tmp_path):
        """Same gate on the preview path — dry_run must not green-light a
        compose that would fail on the real (act) call."""
        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"

        result = _run(_memo_compose(
            {"dry_run": True, "topic": "some-topic", "body": "## Heading\n<!-- comment -->\n"},
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 1


# ===========================================================================
# 6. Store-less-ness architecture test (mirrors AC8/strang-03 C6)
# ===========================================================================

class TestNoMemoIndex:
    def test_no_module_level_mutable_store(self):
        import coordinator_core.ops.fleet.memo_compose as memo_compose_mod

        module_globals = {
            name: val for name, val in vars(memo_compose_mod).items()
            if not name.startswith("__")
        }
        # `MUTATES` is the module's write-surface DECLARATION (c240385d0), a
        # constant manifest of glob patterns read by the tail-declaration sweep —
        # not a store. The invariant under test is "no memo index accumulates in
        # module state", which a declarative list of paths cannot violate.
        declarative_manifests = {"MUTATES"}
        mutable_collections = {
            name: val for name, val in module_globals.items()
            if isinstance(val, (dict, list, set))
            and not isinstance(val, types.ModuleType)
            and name not in declarative_manifests
        }
        assert mutable_collections == {}, (
            f"memo_compose module MUST NOT contain module-level mutable collections "
            f"(dict/list/set) — DR-210 Open-Q §2 store-less-ness invariant. "
            f"Found: {sorted(mutable_collections.keys())}"
        )

    def test_handler_calls_do_not_mutate_module_state(self, tmp_path):
        import coordinator_core.ops.fleet.memo_compose as memo_compose_mod

        def _mutable_names():
            return frozenset(
                name for name, val in vars(memo_compose_mod).items()
                if isinstance(val, (dict, list, set)) and not name.startswith("__")
            )

        sender = _draft_first(tmp_path)
        common_dir = sender / ".git"

        names_before = _mutable_names()
        _run(_memo_compose(
            {"dry_run": True, "topic": "some-topic", "body": "prose."}, repo_root=common_dir,
        ))
        _run(_memo_compose(
            {"dry_run": False, "topic": "some-topic", "body": "prose two."}, repo_root=common_dir,
        ))
        _run(_memo_compose(
            {"dry_run": False, "topic": "no-such-topic", "body": "x"}, repo_root=common_dir,
        ))
        names_after = _mutable_names()

        assert names_after == names_before, (
            f"memo_compose module must not accumulate new module-level mutable state "
            f"across handler calls. New names: {sorted(names_after - names_before)}"
        )


# ===========================================================================
# Carry-through of optional draft fields across a compose rewrite (2026-07-28)
#
# memo.compose re-composes the ENTIRE frontmatter block, so any field it does
# not hand back to compose_draft_frontmatter is silently erased. Before this
# fix, composing a draft that declared scoped_to or in_reply_to returned
# exit_code:0 and delivered a memo without them — the same defect class the
# 2026-07-21 memo.draft fix closed, on the rewrite path instead of create.
# ===========================================================================

class TestCarriedDraftFields:
    def _compose_and_reparse(self, tmp_path, **draft_overrides) -> dict:
        from coordinator_core.frontmatter.primitives import split_frontmatter
        from coordinator_core.frontmatter.schema_validate import parse_yaml

        sender = _draft_first(tmp_path, **draft_overrides)
        result = _run(_memo_compose(
            {
                "dry_run": False,
                "topic": "some-topic",
                "body": "A real prose body sentence.\n",
            },
            repo_root=sender / ".git",
        ))
        assert result["exit_code"] == 0, result
        draft = sender / "state" / "memo-outbox" / "some-topic.md"
        return parse_yaml(split_frontmatter(draft.read_text(encoding="utf-8")).fm_text)

    def test_scoped_to_survives_compose(self, tmp_path):
        scoped_to = {"artifact": "memo_send.py", "sha": "abc1234", "seam": "memo-send"}
        fm = self._compose_and_reparse(tmp_path, scoped_to=scoped_to)
        assert fm["scoped_to"] == scoped_to

    def test_in_reply_to_survives_compose(self, tmp_path):
        fm = self._compose_and_reparse(tmp_path, in_reply_to="2026-07-25-foo.md")
        assert fm["in_reply_to"] == "2026-07-25-foo.md"

    def test_space_survives_compose(self, tmp_path):
        fm = self._compose_and_reparse(tmp_path, space="gate-migration")
        assert fm["space"] == "gate-migration"

    def test_supersedes_list_survives_compose(self, tmp_path):
        fm = self._compose_and_reparse(
            tmp_path, supersedes=["2026-07-20-a.md", "2026-07-21-b.md"],
        )
        assert fm["supersedes"] == ["2026-07-20-a.md", "2026-07-21-b.md"]

    def test_absent_fields_are_not_invented(self, tmp_path):
        fm = self._compose_and_reparse(tmp_path)
        for key in ("scoped_to", "in_reply_to", "space", "supersedes"):
            assert key not in fm

    def test_sent_by_not_carried_through_compose(self, tmp_path):
        """C7 ruling (docs/plans/2026-08-13-session-identity-earns-its-keep.md
        § C7): sent_by is resolved at SEND time, deliberately NOT a
        draft-staged field — draft time is not send time. A draft rewritten
        through _memo_compose must neither ACQUIRE sent_by (it is never in
        `_CARRIED_DRAFT_FIELDS`, so a draft without it stays without it —
        same as every other uncarried field, see
        test_absent_fields_are_not_invented above) NOR LOSE one a caller
        hand-injected onto the draft file directly (compose re-composes the
        WHOLE frontmatter via compose_draft_frontmatter, which has no
        sent_by parameter at all — the field simply cannot round-trip
        through this path, by design, not by omission)."""
        from coordinator_core.frontmatter.primitives import split_frontmatter
        from coordinator_core.frontmatter.schema_validate import parse_yaml

        sender = _draft_first(tmp_path, topic="sent-by-not-carried")
        draft_path = sender / "state" / "memo-outbox" / "sent-by-not-carried.md"

        # Simulate a draft that somehow carries a sent_by line (e.g. hand-
        # edited, or a future regression) — hand-inject it directly onto
        # disk since compose_draft_frontmatter has no sent_by parameter to
        # write one through memo.draft itself.
        original = draft_path.read_text(encoding="utf-8")
        split = split_frontmatter(original)
        fm_text, body = split.fm_text, split.body_with_leading_newline
        assert "sent_by" not in fm_text  # confirms memo.draft itself never wrote one
        injected = fm_text.rstrip("\n") + '\nsent_by: "should-not-survive"\n'
        draft_path.write_text(f"---\n{injected}---{body}", encoding="utf-8")

        parsed_before = parse_yaml(injected)
        assert parsed_before["sent_by"] == "should-not-survive"

        result = _run(_memo_compose(
            {
                "dry_run": False,
                "topic": "sent-by-not-carried",
                "body": "A real prose body sentence.\n",
            },
            repo_root=sender / ".git",
        ))
        assert result["exit_code"] == 0, result

        rewritten = draft_path.read_text(encoding="utf-8")
        rewritten_fm = split_frontmatter(rewritten).fm_text
        assert "sent_by" not in rewritten_fm, (
            "sent_by must not survive a memo.compose rewrite — it is "
            "deliberately excluded from _CARRIED_DRAFT_FIELDS (send-time "
            "field, not a draft-staged one)"
        )
