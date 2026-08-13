"""
Unit + containment tests for coordinator_core.ops.memo_transition.

Coverage (post-DR-215 native-port disposition per AC8):
  (1) Action disposition validation — mutual-exclusion guard (_validate_action_disposition).
      Retargeted to native function; checks retained per AC3/AC8.
  (2) Containment gate (AC13/AC3) — git-init tmp repos, ../traversal, absolute escapes,
      legit cross-repo paths in same and sibling git repos.
      Retargeted to call _containment_check directly (no subprocess stubs needed).

Deleted by AC8 (DR-215 discipline — retired machinery, never skipped-to-green):
  - TestBuildArgvClaim, TestBuildArgvAction, TestBuildArgvRelease (build_argv gone)
  - TestExitCodeMapping ({ok,stdout} return shape retired; native returns {exit_code,...})
  - TestCLIContainmentGate (_cli_containment_check removed; cli_path concept retired)
  - TestClaimParamValidation (tested build_argv param validation; retired with build_argv)

Spec backlink: docs/plans/2026-07-06-memo-transition-native-python-port.md § C4, AC8
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import yaml

import coordinator_core.ops.memo_transition as _memo_mod
from coordinator_core.frontmatter.schema_validate import validate_memo_cross_fields
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS
from coordinator_core.ops.memo_transition import (
    _action,
    _claim,
    _containment_check,
    _normalize_oversize_summary,
    _release,
    _resolve,
)

try:
    from coordinator_core.ops.memo_transition import _close
except ImportError:
    _close = None  # not yet implemented — TestClose reproduces the gap


def _fm_dict(memo_path) -> dict:
    """Read back a memo's frontmatter as a parsed dict, for cross-field round-trip."""
    text = Path(memo_path).read_text(encoding="utf-8")
    split = _memo_mod.split_frontmatter(text)
    return yaml.safe_load(split.fm_text) or {}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _git_init(path: Path) -> None:
    """Initialise a bare-minimum git repo so git rev-parse --show-toplevel works."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    # Commit something so the repo is valid (not strictly required for rev-parse but safer).
    (path / ".gitkeep").touch()
    subprocess.run(["git", "-C", str(path), "add", ".gitkeep"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init", "--allow-empty-message"],
        check=True, capture_output=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
             "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _git_track(repo: Path, target: Path) -> None:
    """Stage and commit *target*, mirroring delivery's own single-path commit."""
    rel = str(target.relative_to(repo))
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "add", "--", rel],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "deliver memo", "--", rel],
                   check=True, capture_output=True, env=env)


# ---------------------------------------------------------------------------
# (1) Action disposition validation — mutual-exclusion (retargeted to _action)
# ---------------------------------------------------------------------------

# Review: code-reviewer (F2) — _validate_action_disposition was a dead function never called by
# _action. Its is-not-None semantics (decision="" treated as "supplied") differed from _action's
# live truthy guard (decision="" treated as "not supplied"), and it raised ValueError instead of
# returning _err() (violating AC6). Tests retargeted to _action directly against a real git repo
# + in-progress fixture, mirroring the TestContainmentGate pattern.

class TestActionDispositionValidation:
    """_action mutual-exclusion guard — retargeted to the live code path.

    Uses a real git repo + in-progress fixture so _action can run through containment
    and reach the disposition guard without monkeypatching.
    """

    _IN_PROGRESS_FIXTURE = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path) -> str:
        """Create a git repo + in-progress memo under cross-repo/inbox/, tracked in HEAD.

        The memo is committed at setup because that is the only state a
        transition verb ever observes in production:
        `coordinator/bin/cross-repo-memo`'s `_commit_delivered_memo` stages and
        commits the delivered memo in the receiver repo before any verb runs
        against it. An untracked fixture describes a state delivery cannot
        produce, and the commit path (`git_native.commit_authored_content`)
        refuses a path absent from HEAD by design — it exists to mutate an
        existing file in place.
        """
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(self._IN_PROGRESS_FIXTURE, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def test_both_decision_and_actioned_note_rejected(self, tmp_path):
        """_action rejects when both --decision and --actioned-note are supplied."""
        memo = self._setup_memo(tmp_path)
        result = _action(memo, {"decision": "accepted", "actioned_note": "done"})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "mutually exclusive" in result["error"]

    def test_neither_rejected(self, tmp_path):
        """_action rejects when neither --decision nor --actioned-note is supplied."""
        memo = self._setup_memo(tmp_path)
        result = _action(memo, {})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "requires either" in result["error"]

    def test_decision_only_applies(self, tmp_path):
        """_action with decision=declined (no realized_by needed) applies — guard passes."""
        memo = self._setup_memo(tmp_path)
        # 'declined' does not require --realized-by, so both the mutual-exclusion check
        # and the realized_by arg check pass; the op applies to the in-progress fixture.
        result = _action(memo, {"decision": "declined"})
        assert result["exit_code"] == 0
        assert result["applied"] is True

    def test_actioned_note_only_applies(self, tmp_path):
        """_action with actioned_note only applies — mutual-exclusion guard passes."""
        memo = self._setup_memo(tmp_path)
        result = _action(memo, {"actioned_note": "noted"})
        assert result["exit_code"] == 0
        assert result["applied"] is True


# ---------------------------------------------------------------------------
# (1b) Multi-line decision_note/actioned_note guard (_validate_action_disposition)
#
# Regression coverage: a multi-line note used to break serialize_yaml_scalar's
# key: value line, truncating the frontmatter and misdirecting the caller
# toward a bogus "realized_by required" error from the cross-field validator.
# The guard fires before any write and before the realized_by check.
# ---------------------------------------------------------------------------

class TestMultilineNoteGuard(TestActionDispositionValidation):
    """_action/--decision-note and --actioned-note reject embedded \\n / \\r.

    Inherits _setup_memo/_IN_PROGRESS_FIXTURE from TestActionDispositionValidation.
    """

    def test_multiline_decision_note_rejected(self, tmp_path):
        memo = self._setup_memo(tmp_path)
        before = Path(memo).read_bytes()

        result = _action(
            memo,
            {"decision": "accepted", "decision_note": "line one\nline two", "realized_by": "abc1234"},
        )

        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "--decision-note" in result["error"]
        assert "single-line" in result["error"]
        # realized_by must NOT be blamed — that was the exact misdirection this fixes.
        assert "realized_by" not in result["error"] and "realized-by" not in result["error"]
        assert Path(memo).read_bytes() == before

    def test_multiline_actioned_note_rejected(self, tmp_path):
        memo = self._setup_memo(tmp_path)
        before = Path(memo).read_bytes()

        result = _action(memo, {"actioned_note": "first line\nsecond line"})

        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "--actioned-note" in result["error"]
        assert "single-line" in result["error"]
        assert Path(memo).read_bytes() == before

    def test_carriage_return_in_decision_note_rejected(self, tmp_path):
        memo = self._setup_memo(tmp_path)
        before = Path(memo).read_bytes()

        result = _action(
            memo,
            {"decision": "accepted", "decision_note": "line one\rline two", "realized_by": "abc1234"},
        )

        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "--decision-note" in result["error"]
        assert Path(memo).read_bytes() == before

    def test_carriage_return_in_actioned_note_rejected(self, tmp_path):
        memo = self._setup_memo(tmp_path)
        before = Path(memo).read_bytes()

        result = _action(memo, {"actioned_note": "first line\rsecond line"})

        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "--actioned-note" in result["error"]
        assert Path(memo).read_bytes() == before

    def test_singleline_note_with_valid_realized_by_still_succeeds(self, tmp_path):
        """Regression guard: the fix must not break the normal single-line path."""
        memo = self._setup_memo(tmp_path)

        result = _action(
            memo,
            {"decision": "accepted", "decision_note": "single line note", "realized_by": "abc1234"},
        )

        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["decision_note"] == "single line note"
        assert fm["realized_by"] == "abc1234"

    def test_literal_backslash_n_sequence_not_rejected(self, tmp_path):
        """A literal two-character backslash-n sequence (not a real newline) is fine."""
        memo = self._setup_memo(tmp_path)

        result = _action(
            memo,
            {"decision": "accepted", "decision_note": r"contains a literal \n sequence", "realized_by": "abc1234"},
        )

        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["decision_note"] == "contains a literal \\n sequence"


# ---------------------------------------------------------------------------
# (2) Containment tests (AC13 / AC3 — _containment_check retained)
#
# Retargeted from the former memo_transition() subprocess stub to calling
# _containment_check directly — the check is retained per AC3 and is native.
# No monkeypatching or node-spawn stubs needed.
# ---------------------------------------------------------------------------

class TestContainmentGate:
    """Memo containment gate tests using real tmp git repos.

    Calls _containment_check(memo_path_str) directly — the native check that
    runs before any frontmatter-primitive call in each verb.
    """

    def test_path_traversal_escape_is_refused(self, tmp_path):
        """A memo path with ../ that escapes cross-repo/ must be refused."""
        repo = tmp_path / "traversal_repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        # Craft a path that resolves OUTSIDE the cross-repo subtree.
        escaped = str(inbox / ".." / ".." / "outside.md")
        # Resolved: <repo>/outside.md — NOT under cross-repo or state.
        with pytest.raises(ValueError, match="outside containment"):
            _containment_check(escaped)

    def test_absolute_etc_passwd_is_refused(self, tmp_path):
        """An absolute path outside any git repo (or wrong subtree) must be refused."""
        with pytest.raises((ValueError, RuntimeError)):
            # /etc/passwd is either not in a git repo at all (ValueError) or in
            # one but not under cross-repo/state (also ValueError).
            _containment_check("/etc/passwd")

    def test_legit_memo_under_cross_repo_inbox_is_accepted(self, tmp_path):
        """A memo under <tmprepo>/cross-repo/inbox/ must pass containment."""
        repo = tmp_path / "legit_repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo-x.md"
        memo.write_text("# memo\n")
        # Must not raise — containment passes for cross-repo/inbox/.
        _containment_check(str(memo))

    def test_legit_memo_under_state_is_accepted(self, tmp_path):
        """A memo under <tmprepo>/state/ must pass containment."""
        repo = tmp_path / "state_repo"
        _git_init(repo)
        state = repo / "state" / "memos"
        state.mkdir(parents=True)
        memo = state / "memo-y.md"
        memo.write_text("# memo\n")
        _containment_check(str(memo))

    def test_sibling_git_repo_cross_repo_is_accepted(self, tmp_path):
        """A memo under a SECOND (sibling) git repo's cross-repo/ must also be accepted.

        Guards against a false-reject that only allows the 'main' claude-klabauter repo — the
        _containment_check is consumer-agnostic (show_top scope).
        """
        sibling = tmp_path / "sibling_repo"
        _git_init(sibling)
        inbox = sibling / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo-y.md"
        memo.write_text("# memo\n")
        _containment_check(str(memo))


# ---------------------------------------------------------------------------
# (3) LockTimeout surfaces as exit_code=1 error result (C2 locked_rmw contract)
#
# Verifies that a LockTimeout raised by locked_rmw inside each verb is caught
# by the try/except (LockTimeout, MutateAbort) → _err(...) wrapper and returned
# as {exit_code: 1, applied: False, error: str} — NOT as an uncaught exception
# that would become a -32603 INTERNAL_ERROR at the IPC layer.
#
# Uses unittest.mock.patch to inject a LockTimeout without needing a subprocess
# lock holder — the goal is to assert the exception-to-error mapping, not to
# re-test the lock mechanics (those live in test_locked_write.py).
# ---------------------------------------------------------------------------

class TestLockTimeoutSurfacesAsErrorResult:
    """LockTimeout from locked_rmw must be caught and returned as exit_code=1 error dict.

    Each verb (_claim, _action, _release) wraps its locked_rmw call in
    try/except (LockTimeout, MutateAbort) → _err(...).  This class asserts that
    the mapping works — i.e. no uncaught exception escapes to the IPC layer.
    """

    _OPEN_FIXTURE = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""
    _IN_PROGRESS_FIXTURE = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path, content: str) -> str:
        """Create a git repo + memo under cross-repo/inbox/, tracked in HEAD.

        Committed at setup for the same reason the module's other fixtures are:
        delivery commits the memo before any verb runs against it, so untracked
        is not a reachable production state.
        """
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def test_claim_lock_timeout_returns_error_dict(self, tmp_path):
        """_claim: LockTimeout from locked_rmw → exit_code=1 error dict, not uncaught exc."""
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE)
        with patch.object(_memo_mod, "locked_rmw", side_effect=LockTimeout("timed out holding lock")):
            result = _claim(memo, "sess-1", "2026-07-06T00:00:00Z")
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "error" in result

    def test_action_lock_timeout_returns_error_dict(self, tmp_path):
        """_action: LockTimeout from locked_rmw → exit_code=1 error dict, not uncaught exc."""
        memo = self._setup_memo(tmp_path, self._IN_PROGRESS_FIXTURE)
        with patch.object(_memo_mod, "locked_rmw", side_effect=LockTimeout("timed out holding lock")):
            result = _action(memo, {"decision": "declined"})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "error" in result

    def test_release_lock_timeout_returns_error_dict(self, tmp_path):
        """_release: LockTimeout from locked_rmw → exit_code=1 error dict, not uncaught exc."""
        memo = self._setup_memo(tmp_path, self._IN_PROGRESS_FIXTURE)
        with patch.object(_memo_mod, "locked_rmw", side_effect=LockTimeout("timed out holding lock")):
            result = _release(memo)
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# (4) Summary-cap truncate-and-warn normalization (Ask 2)
#
# cross-repo/inbox/2026-07-22-claude-central-em-two-asks-installer-seed-and-
# memo-stamp-normalization.md § Ask 2 — an over-cap summary: must be
# truncated-and-warned by the stamp, not hard-failed by _validate_memo_fm.
# ---------------------------------------------------------------------------

class TestNormalizeOversizeSummaryUnit:
    """_normalize_oversize_summary — direct unit coverage of the helper."""

    def test_over_cap_summary_truncated_to_exactly_cap_with_ellipsis(self):
        """An over-cap summary is truncated to exactly _SUMMARY_MAX_CHARS chars, ellipsis-terminated."""
        long_summary = "x" * (_SUMMARY_MAX_CHARS + 50)
        fm_text = f"kind: fyi\nsummary: {long_summary}\nstatus: open\n"
        result = _normalize_oversize_summary(fm_text, "/tmp/does-not-matter.md")
        new_summary = _memo_mod.read_fm_field_unquoted(result, "summary")
        assert len(new_summary) == _SUMMARY_MAX_CHARS
        assert new_summary == ("x" * (_SUMMARY_MAX_CHARS - 1)) + "…"

    def test_over_cap_summary_emits_stderr_warning(self, capsys):
        """A truncation emits a warning to stderr naming the memo path, original length, and cap fit."""
        long_summary = "y" * (_SUMMARY_MAX_CHARS + 10)
        fm_text = f"kind: fyi\nsummary: {long_summary}\nstatus: open\n"
        _normalize_oversize_summary(fm_text, "/some/memo/path.md")
        captured = capsys.readouterr()
        assert "/some/memo/path.md" in captured.err
        assert str(_SUMMARY_MAX_CHARS + 10) in captured.err
        assert "truncated" in captured.err

    def test_at_cap_summary_left_byte_identical_no_warning(self, capsys):
        """A summary at exactly the cap length is left byte-identical, no warning emitted."""
        at_cap_summary = "z" * _SUMMARY_MAX_CHARS
        fm_text = f"kind: fyi\nsummary: {at_cap_summary}\nstatus: open\n"
        result = _normalize_oversize_summary(fm_text, "/tmp/x.md")
        assert result == fm_text
        assert capsys.readouterr().err == ""

    def test_under_cap_summary_left_byte_identical_no_warning(self, capsys):
        """A summary under the cap is left byte-identical, no warning emitted."""
        fm_text = "kind: fyi\nsummary: A short summary.\nstatus: open\n"
        result = _normalize_oversize_summary(fm_text, "/tmp/x.md")
        assert result == fm_text
        assert capsys.readouterr().err == ""

    def test_missing_summary_is_noop(self, capsys):
        """Absent summary: field is a no-op — frontmatter unchanged, no warning."""
        fm_text = "kind: fyi\nstatus: open\n"
        result = _normalize_oversize_summary(fm_text, "/tmp/x.md")
        assert result == fm_text
        assert capsys.readouterr().err == ""

    # -- P2-2 (2026-07-22 crossrepo-two-asks-review): double-quoted summary: --
    # is the shape memo.send/memo.compose actually emit (memo_send.py:~808,
    # memo_compose.py:~246-249 via `_yaml_quote` — always double-quotes).
    # `read_fm_field_unquoted` strips the double-quote pair via
    # `unquote_yaml_scalar`; these tests exercise that path directly rather than
    # relying on the plain-scalar tests above to stand in for it.

    def test_double_quoted_over_cap_summary_truncated(self):
        """A double-quoted over-cap summary (memo.send/compose emitted shape) truncates."""
        long_summary = "x" * (_SUMMARY_MAX_CHARS + 30)
        fm_text = f'kind: fyi\nsummary: "{long_summary}"\nstatus: open\n'
        result = _normalize_oversize_summary(fm_text, "/tmp/does-not-matter.md")
        new_summary = _memo_mod.read_fm_field_unquoted(result, "summary")
        assert len(new_summary) == _SUMMARY_MAX_CHARS
        assert new_summary == ("x" * (_SUMMARY_MAX_CHARS - 1)) + "…"

        # Round-trips through validate_memo_cross_fields as valid YAML.
        fm_dict = yaml.safe_load(result)
        assert len(fm_dict["summary"]) == _SUMMARY_MAX_CHARS
        assert validate_memo_cross_fields(fm_dict) == []

    def test_double_quoted_at_cap_summary_left_byte_identical_no_warning(self, capsys):
        """A double-quoted at-cap summary is left byte-identical, no warning emitted."""
        at_cap_summary = "z" * _SUMMARY_MAX_CHARS
        fm_text = f'kind: fyi\nsummary: "{at_cap_summary}"\nstatus: open\n'
        result = _normalize_oversize_summary(fm_text, "/tmp/x.md")
        assert result == fm_text
        assert capsys.readouterr().err == ""

    # -- P2-1 (2026-07-22 crossrepo-two-asks-review): block-scalar `summary:` --
    # (`|` / `>`) previously silently no-opped — read_fm_field_unquoted only ever
    # saw the bare block-scalar indicator token off the key's own line (length 1),
    # so an over-cap block-scalar summary strands the memo at
    # _memo_cf_summary_length_cap downstream instead of being truncated here.

    def test_block_scalar_literal_over_cap_truncated_and_flattened(self):
        """An over-cap literal block scalar (`summary: |`) is decoded, flattened, and truncated."""
        fm_text = (
            'title: "Test"\n'
            "summary: |\n"
            "  Some very long text that goes here and keeps going on and on and on and on\n"
            "  across two lines and more content padding padding padding padding padding\n"
            "  to push it comfortably past the one hundred twenty character cap for sure\n"
            "decision: accepted\n"
        )
        result = _normalize_oversize_summary(fm_text, "/tmp/does-not-matter.md")
        fm_dict = yaml.safe_load(result)
        assert len(fm_dict["summary"]) == _SUMMARY_MAX_CHARS
        assert fm_dict["summary"].endswith("…")
        # Flattened — no embedded newline survives into the single-line replacement.
        assert "\n" not in fm_dict["summary"]
        # Other fields untouched.
        assert fm_dict["title"] == "Test"
        assert fm_dict["decision"] == "accepted"
        assert validate_memo_cross_fields(fm_dict) == []

    def test_block_scalar_folded_over_cap_truncated(self):
        """An over-cap folded block scalar (`summary: >`) is decoded, flattened, and truncated."""
        fm_text = (
            "summary: >\n"
            "  Some very long text that goes here and keeps going on and on and on and on\n"
            "  across two lines and more content padding padding padding padding padding\n"
            "  to push it comfortably past the one hundred twenty character cap for sure\n"
            "status: open\n"
        )
        result = _normalize_oversize_summary(fm_text, "/tmp/does-not-matter.md")
        fm_dict = yaml.safe_load(result)
        assert len(fm_dict["summary"]) == _SUMMARY_MAX_CHARS
        assert fm_dict["summary"].endswith("…")
        assert fm_dict["status"] == "open"
        assert validate_memo_cross_fields(fm_dict) == []

    def test_block_scalar_over_cap_emits_stderr_warning(self, capsys):
        """A block-scalar truncation emits a warning naming the memo, decoded length, and cap fit."""
        fm_text = (
            "summary: |\n"
            "  Some very long text that goes here and keeps going on and on and on and on\n"
            "  across two lines and more content padding padding padding padding padding\n"
            "  to push it comfortably past the one hundred twenty character cap for sure\n"
            "status: open\n"
        )
        _normalize_oversize_summary(fm_text, "/some/memo/path.md")
        captured = capsys.readouterr()
        assert "/some/memo/path.md" in captured.err
        assert "truncated" in captured.err

    def test_block_scalar_under_cap_left_byte_identical_no_warning(self, capsys):
        """An under-cap block scalar is left in block-scalar form, byte-identical, no warning.

        The cross-field cap check measures the yaml.safe_load-decoded length, not
        on-disk shape — an under-cap block scalar already passes validation as-is,
        so this helper must not touch it.
        """
        fm_text = "summary: |\n  short block scalar\nstatus: open\n"
        result = _normalize_oversize_summary(fm_text, "/tmp/x.md")
        assert result == fm_text
        assert capsys.readouterr().err == ""
        fm_dict = yaml.safe_load(result)
        assert validate_memo_cross_fields(fm_dict) == []


class TestSummaryCapNormalizationPerVerb:
    """Integration: an over-cap summary: no longer hard-fails claim/action/release.

    Before this fix, _validate_memo_fm's cross-field cap check
    (schema_validate._memo_cf_summary_length_cap) would abort every one of
    these transitions with a MutateAbort. Each test here asserts the
    transition now SUCCEEDS and the on-disk summary is truncated to the cap,
    round-tripped through validate_memo_cross_fields to prove the normalized
    frontmatter is itself valid.
    """

    _OVER_CAP_SUMMARY = "S" * (_SUMMARY_MAX_CHARS + 30)

    _OPEN_FIXTURE_TEMPLATE = """\
---
kind: fyi
status: open
from: sender-session
summary: {summary}
created: 2026-06-01
---
"""

    _IN_PROGRESS_FIXTURE_TEMPLATE = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
from: sender-session
summary: {summary}
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path, content: str) -> str:
        """Create a git repo + memo under cross-repo/inbox/, tracked in HEAD.

        Committed at setup for the same reason the module's other fixtures are:
        delivery commits the memo before any verb runs against it, so untracked
        is not a reachable production state.
        """
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def _fm_dict(self, memo_path: str) -> dict:
        """Read back the memo's frontmatter as a parsed dict, for cross-field round-trip."""
        text = Path(memo_path).read_text(encoding="utf-8")
        split = _memo_mod.split_frontmatter(text)
        return yaml.safe_load(split.fm_text) or {}

    def test_claim_succeeds_where_it_previously_aborted(self, tmp_path):
        """claim: an over-cap summary: no longer aborts; on-disk summary is truncated."""
        memo = self._setup_memo(
            tmp_path, self._OPEN_FIXTURE_TEMPLATE.format(summary=self._OVER_CAP_SUMMARY)
        )
        result = _claim(memo, "sess-1", "2026-07-22T00:00:00Z")
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm_dict = self._fm_dict(memo)
        assert len(fm_dict["summary"]) == _SUMMARY_MAX_CHARS
        assert fm_dict["summary"].endswith("…")
        assert validate_memo_cross_fields(fm_dict) == []

    def test_action_succeeds_where_it_previously_aborted(self, tmp_path):
        """action: an over-cap summary: no longer aborts; on-disk summary is truncated."""
        memo = self._setup_memo(
            tmp_path, self._IN_PROGRESS_FIXTURE_TEMPLATE.format(summary=self._OVER_CAP_SUMMARY)
        )
        result = _action(memo, {"decision": "declined"})
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm_dict = self._fm_dict(memo)
        assert len(fm_dict["summary"]) == _SUMMARY_MAX_CHARS
        assert fm_dict["summary"].endswith("…")
        assert validate_memo_cross_fields(fm_dict) == []

    def test_release_succeeds_where_it_previously_aborted(self, tmp_path):
        """release: an over-cap summary: no longer aborts; on-disk summary is truncated."""
        memo = self._setup_memo(
            tmp_path, self._IN_PROGRESS_FIXTURE_TEMPLATE.format(summary=self._OVER_CAP_SUMMARY)
        )
        result = _release(memo)
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm_dict = self._fm_dict(memo)
        assert len(fm_dict["summary"]) == _SUMMARY_MAX_CHARS
        assert fm_dict["summary"].endswith("…")
        assert validate_memo_cross_fields(fm_dict) == []

    def test_claim_at_cap_summary_left_untouched_no_warning(self, tmp_path, capsys):
        """claim with an at-cap summary succeeds with no truncation warning."""
        at_cap = "A" * _SUMMARY_MAX_CHARS
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE_TEMPLATE.format(summary=at_cap))
        result = _claim(memo, "sess-1", "2026-07-22T00:00:00Z")
        assert result["exit_code"] == 0
        fm_dict = self._fm_dict(memo)
        assert fm_dict["summary"] == at_cap
        assert "truncated" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# (5) resolve verb (C1) — atomic claim+action for open memos, disposition required
#
# Spec backlink: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C1
#
# resolve moves open -> actioned in ONE locked_rmw closure — no intermediate
# in_progress state is ever visible on disk. These tests assert the disk-truth
# outcome (fields present, single write) AND directly assert locked_rmw is
# invoked exactly once per resolve call — the atomicity claim would otherwise
# be unfalsifiable from field-content assertions alone.
# ---------------------------------------------------------------------------

class TestResolve:
    """resolve verb — open -> actioned atomic transition (C1)."""

    _OPEN_FIXTURE = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""
    _IN_PROGRESS_OTHER_SESSION_FIXTURE = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: some-other-session
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""
    _ACTIONED_FIXTURE = """\
---
kind: fyi
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
decision: declined
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path, content: str) -> str:
        """Create a git repo + memo under cross-repo/inbox/, tracked in HEAD.

        Committed at setup for the same reason the module's other fixtures are:
        delivery commits the memo before any verb runs against it, so untracked
        is not a reachable production state.
        """
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def _fm_dict(self, memo_path: str) -> dict:
        """Read back the memo's frontmatter as a parsed dict."""
        text = Path(memo_path).read_text(encoding="utf-8")
        split = _memo_mod.split_frontmatter(text)
        return yaml.safe_load(split.fm_text) or {}

    def test_open_memo_with_disposition_resolves_to_actioned_in_one_write(self, tmp_path):
        """open + disposition -> actioned; claim fields (picked_up_at/by) present too."""
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE)
        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z",
            {"decision": "accepted", "realized_by": "abc1234"},
        )
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm_dict = self._fm_dict(memo)
        assert fm_dict["status"] == "actioned"
        assert fm_dict["decision"] == "accepted"
        assert fm_dict["realized_by"] == "abc1234"
        assert fm_dict["picked_up_by"] == "sess-1"
        assert fm_dict["picked_up_at"] == "2026-07-26T00:00:00Z"
        assert validate_memo_cross_fields(fm_dict) == []

    def test_no_disposition_fails_loud_with_no_write(self, tmp_path):
        """resolve with no decision/actioned_note fails loud; memo left untouched."""
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _resolve(memo, "sess-1", "2026-07-26T00:00:00Z", {})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "requires either" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_live_peer_claim_refuses_with_no_write(self, tmp_path):
        """resolve refuses (does not steal) when another session holds in_progress."""
        memo = self._setup_memo(tmp_path, self._IN_PROGRESS_OTHER_SESSION_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _resolve(
            memo, "sess-1", "2026-07-26T00:00:00Z", {"actioned_note": "noted"},
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "in_progress" in result["error"]
        assert "some-other-session" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_already_actioned_different_disposition_refuses(self, tmp_path):
        """The existing re-action guard still fires: already-actioned with a
        DIFFERENT disposition is refused, not silently re-actioned."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _resolve(
            memo, "session-test", "2026-07-26T00:00:00Z", {"decision": "accepted", "realized_by": "x"},
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "cannot re-action" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_already_actioned_same_disposition_is_idempotent_noop(self, tmp_path):
        """Already actioned at the EXACT target disposition -> no-op, applied=False."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        result = _resolve(
            memo, "session-test", "2026-07-26T00:00:00Z", {"decision": "declined"},
        )
        assert result["exit_code"] == 0
        assert result["applied"] is False

    def test_resolve_invokes_locked_rmw_exactly_once(self, tmp_path):
        """AC1: resolve is ONE locked_rmw closure, never two (no intermediate
        in_progress write is ever visible on disk between claim and action)."""
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE)
        with patch.object(_memo_mod, "locked_rmw", wraps=_memo_mod.locked_rmw) as spy:
            result = _resolve(
                memo, "sess-1", "2026-07-26T00:00:00Z",
                {"decision": "accepted", "realized_by": "abc1234"},
            )
        assert result["exit_code"] == 0
        assert spy.call_count == 1

    def test_resolve_lock_timeout_returns_error_dict(self, tmp_path):
        """resolve: LockTimeout from locked_rmw -> exit_code=1 error dict, not uncaught exc."""
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE)
        with patch.object(_memo_mod, "locked_rmw", side_effect=LockTimeout("timed out holding lock")):
            result = _resolve(memo, "sess-1", "2026-07-26T00:00:00Z", {"decision": "declined"})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# Duplicate-key guard, CRLF (2026-07-28 code review, finding 2)
#
# `_count_status_keys` is the C5 duplicate-key guard. Its boundary lookahead
# was `(?=[ \t]|$)`, which rejects the `\r` of a present-but-empty
# `status:\r\n` — so on a Windows-authored memo the guard silently UNDER-counts
# and fails OPEN on exactly the corruption it exists to catch. Widened to
# `(?=[ \t]|\r?$)`, matching frontmatter/primitives.py's key-resolution rule.
#
# Every case is asserted under BOTH endings: an LF-only assertion passes
# against the unfixed regex and proves nothing.
# ---------------------------------------------------------------------------


class TestCountStatusKeysCRLF:
    """`status:` counting must be identical under LF and CRLF."""

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_present_but_empty_status_is_counted(self, eol):
        fm = f"title: t{eol}status:{eol}other: v{eol}"
        assert _memo_mod._count_status_keys(fm) == 1

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_duplicate_status_keys_are_both_counted_when_one_is_empty(self, eol):
        """The guard's whole purpose: a second `status:` key must be visible
        even when it is the empty one."""
        fm = f"title: t{eol}status: open{eol}status:{eol}"
        assert _memo_mod._count_status_keys(fm) == 2

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_valued_status_is_counted(self, eol):
        fm = f"title: t{eol}status: open{eol}"
        assert _memo_mod._count_status_keys(fm) == 1

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_status_message_is_not_counted(self, eol):
        """The boundary guarantee the lookahead exists for, preserved under
        CRLF and against the newly-visible EMPTY `status_message:` shape."""
        fm = f"status_message: detail{eol}status_message:{eol}"
        assert _memo_mod._count_status_keys(fm) == 0

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_statusopen_without_separator_is_not_counted(self, eol):
        fm = f"status:open{eol}"
        assert _memo_mod._count_status_keys(fm) == 0


# ---------------------------------------------------------------------------
# Block-scalar span locator, CRLF (2026-07-28 — surfaced by the finding-2 sweep,
# same CRLF-blindness family, different regex position).
#
# `_replace_block_scalar_span`'s terminators were bare `\n`, so the span never
# matched a CRLF-authored memo: the locator returned None and
# `_normalize_block_scalar_summary` left an OVER-CAP summary on disk — a
# fail-open on the very cap that path enforces.
# ---------------------------------------------------------------------------


class TestBlockScalarSpanCRLF:

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_span_replace_locates_a_block_scalar(self, eol):
        fm = (
            f"title: t{eol}"
            f"summary: |{eol}"
            f"  line one{eol}"
            f"  line two{eol}"
            f"status: open{eol}"
        )
        out = _memo_mod._replace_block_scalar_span(fm, "summary", 'summary: "flat"\n')
        assert out is not None, "span not located"
        assert 'summary: "flat"' in out
        assert "line one" not in out and "line two" not in out
        assert f"status: open{eol}" in out
        # No mixed endings introduced by the splice.
        assert "\n" not in out.replace(eol, "")

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_span_replace_consumes_blank_continuation_lines(self, eol):
        """A blank continuation line has no `.*` to absorb its `\\r`."""
        fm = f"summary: |{eol}  a{eol}{eol}  b{eol}status: open{eol}"
        out = _memo_mod._replace_block_scalar_span(fm, "summary", 'summary: "flat"\n')
        assert out is not None
        assert "a" not in out.replace('summary: "flat"', "").replace("status: open", "")
        assert f"status: open{eol}" in out

    @pytest.mark.parametrize("eol", ["\n", "\r\n"])
    def test_over_cap_block_scalar_summary_is_actually_capped(self, eol):
        """End-to-end through the caller: the cap must fire on both endings."""
        long_value = "x" * (_memo_mod._SUMMARY_MAX_CHARS + 50)
        fm = f"title: t{eol}summary: |{eol}  {long_value}{eol}status: open{eol}"
        out = _memo_mod._normalize_block_scalar_summary(fm, "memo.md")
        assert out != fm, "over-cap block scalar left untouched"
        parsed = yaml.safe_load(out.replace("\r\n", "\n"))
        assert len(parsed["summary"]) <= _memo_mod._SUMMARY_MAX_CHARS
        assert parsed["status"] == "open"


# ---------------------------------------------------------------------------
# --correct-realization (params["correct_realization"]) — narrow, opt-in
# re-action of an already-actioned memo whose decision: is UNCHANGED, fixing
# the fail-loud that had no legitimate escape hatch for correcting a stale
# realized_by (e.g. a commit later reverted).
#
# Covers both call sites (_action and _resolve) per AC4 (shared logic exists
# exactly once — _handle_already_actioned / _apply_realization_correction).
# ---------------------------------------------------------------------------

class TestCorrectRealization:
    """--correct-realization: evidence correction under an unchanged verdict."""

    _OLD_SHA = "257448d7775761d25859998f23a776fdd0da64f0"
    _NEW_SHA = "c7b2484aa32e4f4a0fcbb821f395567940c6f29f"

    _ACTIONED_FIXTURE = f"""\
---
kind: ask
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
decision: accepted
decision_note: "Some prior rationale."
realized_by: {_OLD_SHA}
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path, content: str) -> str:
        """Create a git repo + memo under cross-repo/inbox/, tracked in HEAD.

        Committed at setup for the same reason the module's other fixtures are:
        delivery commits the memo before any verb runs against it, so untracked
        is not a reachable production state.
        """
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    # -- _action --------------------------------------------------------

    def test_action_correction_succeeds_unchanged_decision(self, tmp_path):
        """decision: unchanged + --correct-realization: realized_by moves, the
        superseded SHA is visible in decision_note, no new frontmatter key."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        result = _action(
            memo,
            {
                "decision": "accepted",
                "realized_by": self._NEW_SHA,
                "correct_realization": True,
            },
        )
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm = _fm_dict(memo)
        assert fm["decision"] == "accepted"
        assert fm["realized_by"] == self._NEW_SHA
        assert self._OLD_SHA in fm["decision_note"]
        assert "correction" in fm["decision_note"]
        # No new frontmatter key introduced (AC3).
        assert set(fm.keys()) == {
            "kind", "status", "picked_up_at", "picked_up_by", "decision",
            "decision_note", "realized_by", "from", "summary", "created",
        }
        assert validate_memo_cross_fields(fm) == []

    def test_action_correction_with_changed_decision_still_fails_loud(self, tmp_path):
        """--correct-realization does NOT unlock a verdict change — the existing
        fail-loud message still fires, byte-for-byte."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _action(
            memo,
            {
                "decision": "declined",
                "realized_by": self._NEW_SHA,
                "correct_realization": True,
            },
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "already actioned with a different disposition — cannot re-action" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_action_absent_flag_fail_loud_unchanged(self, tmp_path):
        """Regression guard: absent --correct-realization, behaviour is
        byte-identical to before the flag existed — still fails loud, no write."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _action(memo, {"decision": "accepted", "realized_by": self._NEW_SHA})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "already actioned with a different disposition — cannot re-action" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_action_idempotent_noop_still_works(self, tmp_path):
        """Exact-match re-action is still a no-op, correct_realization or not."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        result = _action(
            memo,
            {
                "decision": "accepted",
                "realized_by": self._OLD_SHA,
                "decision_note": "Some prior rationale.",
            },
        )
        assert result["exit_code"] == 0
        assert result["applied"] is False

    def test_action_correct_realization_requires_decision(self, tmp_path):
        """--correct-realization without --decision fails loud (no realized_by to
        correct on an actioned_note-shape memo)."""
        actioned_note_fixture = """\
---
kind: fyi
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
actioned_note: "noted"
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""
        memo = self._setup_memo(tmp_path, actioned_note_fixture)
        result = _action(memo, {"actioned_note": "different note", "correct_realization": True})
        assert result["exit_code"] == 1
        assert result["applied"] is False

    # -- _resolve ---------------------------------------------------------

    def test_resolve_correction_succeeds_unchanged_decision(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        result = _resolve(
            memo, "session-test", "2026-07-28T17:00:00Z",
            {
                "decision": "accepted",
                "realized_by": self._NEW_SHA,
                "correct_realization": True,
            },
        )
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm = _fm_dict(memo)
        assert fm["decision"] == "accepted"
        assert fm["realized_by"] == self._NEW_SHA
        assert self._OLD_SHA in fm["decision_note"]
        assert validate_memo_cross_fields(fm) == []

    def test_resolve_correction_with_changed_decision_still_fails_loud(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _resolve(
            memo, "session-test", "2026-07-28T17:00:00Z",
            {
                "decision": "declined",
                "realized_by": self._NEW_SHA,
                "correct_realization": True,
            },
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "already actioned with a different disposition — cannot re-action" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_resolve_absent_flag_fail_loud_unchanged(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _resolve(
            memo, "session-test", "2026-07-28T17:00:00Z",
            {"decision": "accepted", "realized_by": self._NEW_SHA},
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "already actioned with a different disposition — cannot re-action" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_resolve_idempotent_noop_still_works(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_FIXTURE)
        result = _resolve(
            memo, "session-test", "2026-07-28T17:00:00Z",
            {
                "decision": "accepted",
                "realized_by": self._OLD_SHA,
                "decision_note": "Some prior rationale.",
            },
        )
        assert result["exit_code"] == 0
        assert result["applied"] is False

    # -- Live repro (AC1) --------------------------------------------------
    #
    # Reproduces the real 2026-07-28-example-cockpit-repo-em-... memo's shape
    # (decision: accepted, realized_by citing a commit later reverted) against
    # a fixture COPY, never the tracked memo in cross-repo/inbox/ (that memo
    # must not be mutated by this test run — the EM applies the real
    # correction after verification).

    _LIVE_REPRO_FIXTURE = f"""\
---
title: "Addendum to the open thread: a third required property"
from: "example-cockpit-repo-em"
to: "claude-klabauter-em"
created: 2026-07-28
status: actioned
decision: accepted
decision_note: 'Addendum to an open thread; recorded as contract constraints, no code change.'
realized_by: {_OLD_SHA}
picked_up_at: '2026-07-28T15:46:24Z'
picked_up_by: session-test
delivery_mode: receiver-repo
summary: "Third required property for Ask 2, plus one boundary line."
kind: "ask"
in_reply_to: "2026-07-28-claude-klabauter-em-sat-01-store-home-answer.md"
---
"""

    def test_live_repro_realized_by_corrected_from_reverted_to_actual_commit(self, tmp_path):
        """AC1: the live defect — realized_by cites a commit (257448d7...) later
        reverted by c7b2484a... — is fixed via --correct-realization against a
        fixture copy of the real memo's frontmatter shape."""
        memo = self._setup_memo(tmp_path, self._LIVE_REPRO_FIXTURE)
        result = _action(
            memo,
            {
                "decision": "accepted",
                "realized_by": self._NEW_SHA,
                "correct_realization": True,
            },
        )
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm = _fm_dict(memo)
        assert fm["realized_by"] == self._NEW_SHA
        assert fm["decision"] == "accepted"
        assert self._OLD_SHA in fm["decision_note"]


# ---------------------------------------------------------------------------
# supersede-disposition (--supersede-note/--supersede-realized-by) — the
# append-only correction path for a REVERSED verdict, distinct from
# --correct-realization (evidence-only, unchanged decision). Fixes the
# no-escape-hatch gap: cross-repo/inbox/2026-08-12-example-retrieval-repo-em-git-
# index-lock-reaper.md was actioned with a `negotiate` disposition later
# reversed by PM ruling, with the existing "cannot re-action" fail-loud
# correctly refusing to silently overwrite it.
# ---------------------------------------------------------------------------

class TestSupersedeDisposition:
    """--supersede-note/--supersede-realized-by: append-only reversal record."""

    _ACTIONED_NOTE_FIXTURE = """\
---
kind: consult
status: actioned
picked_up_at: '2026-08-12T15:26:19Z'
picked_up_by: session-test
actioned_note: "reaper already exists; no new op; asked sender which command wedged"
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    _IN_PROGRESS_FIXTURE = """\
---
kind: consult
status: in_progress
picked_up_at: '2026-08-12T15:26:19Z'
picked_up_by: session-test
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    _OPEN_FIXTURE = """\
---
kind: consult
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path, content: str) -> str:
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def test_supersede_on_actioned_memo_succeeds_and_preserves_original(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        result = _action(
            memo,
            {
                "supersede_note": "deny removed rather than re-messaged",
                "supersede_realized_by": "5fcece54e172",
                "supersede_at": "2026-08-12T16:08:00Z",
            },
        )
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm = _fm_dict(memo)
        # Original disposition preserved verbatim.
        assert fm["status"] == "actioned"
        assert fm["actioned_note"] == (
            "reaper already exists; no new op; asked sender which command wedged"
        )
        # Superseding record present.
        assert fm["disposition_superseded"] is True
        assert fm["superseding_note"] == "deny removed rather than re-messaged"
        assert fm["superseding_realized_by"] == "5fcece54e172"
        assert fm["superseded_at"] == "2026-08-12T16:08:00Z"
        assert validate_memo_cross_fields(fm) == []

        # Current truth reads first: superseding_* fields precede actioned_note on disk.
        text = Path(memo).read_text(encoding="utf-8")
        assert text.index("disposition_superseded") < text.index("actioned_note")

    def test_supersede_on_never_actioned_open_memo_is_refused(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._OPEN_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _action(
            memo,
            {"supersede_note": "n", "supersede_realized_by": "r"},
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "there is no disposition yet to supersede" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_supersede_on_in_progress_memo_is_refused(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._IN_PROGRESS_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _action(
            memo,
            {"supersede_note": "n", "supersede_realized_by": "r"},
        )
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "there is no disposition yet to supersede" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_re_action_without_supersede_still_refused_exactly_as_today(self, tmp_path):
        """The pre-existing fail-loud is untouched by the new mechanism."""
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        before = Path(memo).read_text(encoding="utf-8")
        result = _action(memo, {"actioned_note": "a different note entirely"})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "already actioned with a different disposition — cannot re-action" in result["error"]
        assert Path(memo).read_text(encoding="utf-8") == before

    def test_supersede_note_requires_realized_by(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        result = _action(memo, {"supersede_note": "n only"})
        assert result["exit_code"] == 1
        assert "--supersede-realized-by" in result["error"]

    def test_supersede_realized_by_requires_note(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        result = _action(memo, {"supersede_realized_by": "r only"})
        assert result["exit_code"] == 1
        assert "--supersede-note" in result["error"]

    def test_supersede_mutually_exclusive_with_decision(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        result = _action(
            memo,
            {
                "supersede_note": "n", "supersede_realized_by": "r",
                "decision": "accepted", "realized_by": "inline",
            },
        )
        assert result["exit_code"] == 1
        assert "mutually exclusive" in result["error"]

    def test_supersede_idempotent_retry_is_noop(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        params = {
            "supersede_note": "deny removed rather than re-messaged",
            "supersede_realized_by": "5fcece54e172",
            "supersede_at": "2026-08-12T16:08:00Z",
        }
        first = _action(memo, params)
        assert first["exit_code"] == 0 and first["applied"] is True

        second = _action(memo, dict(params))
        assert second["exit_code"] == 0
        assert second["applied"] is False

    def test_double_supersede_with_different_note_fails_loud(self, tmp_path):
        memo = self._setup_memo(tmp_path, self._ACTIONED_NOTE_FIXTURE)
        first = _action(
            memo,
            {
                "supersede_note": "first reversal",
                "supersede_realized_by": "aaa1111",
                "supersede_at": "2026-08-12T16:08:00Z",
            },
        )
        assert first["exit_code"] == 0

        before = Path(memo).read_text(encoding="utf-8")
        second = _action(
            memo,
            {
                "supersede_note": "second, different reversal",
                "supersede_realized_by": "bbb2222",
                "supersede_at": "2026-08-12T17:00:00Z",
            },
        )
        assert second["exit_code"] == 1
        assert "cannot supersede twice" in second["error"]
        assert Path(memo).read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# (X) Receiver tolerance for an off-enum `kind` — see
# _demote_kind_enum_finding/_validate_memo_fm in memo_transition.py.
#
# Three real cross-repo/inbox/ memos carry kind: defect / kind: blocked-request-
# correction, outside the four-value VALID_KINDS enum, and were undrainable
# through claim/action/release/resolve. This demotes the field=='kind' finding
# to a stderr warning at the RECEIVER only — authoring still hard-rejects.
# ---------------------------------------------------------------------------

class TestKindEnumReceiverTolerance:
    """An off-enum `kind:` must not strand a memo at any transition verb."""

    _OPEN_UNENUMERATED_KIND_FIXTURE = """\
---
kind: defect
status: open
from: sender-session
summary: A test memo with an unenumerated kind.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path, content: str | None = None) -> str:
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content or self._OPEN_UNENUMERATED_KIND_FIXTURE, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def test_authoring_still_rejects_unenumerated_kind(self):
        """Authoring-side gate is untouched: validate_memo_cross_fields still errors."""
        errors = validate_memo_cross_fields({"kind": "defect"})
        assert any(e.get("field") == "kind" for e in errors)

    def test_memo_cf_kind_enum_still_rejects_directly(self):
        """_memo_cf_kind_enum itself is untouched by the receiver-side demotion."""
        from coordinator_core.frontmatter.schema_validate import _memo_cf_kind_enum
        err = _memo_cf_kind_enum({"kind": "defect"})
        assert err is not None
        assert err["field"] == "kind"

    def test_claim_succeeds_on_unenumerated_kind_and_warns(self, tmp_path, capsys):
        """claim: an off-enum kind claims successfully, kind stays byte-identical, warns."""
        memo = self._setup_memo(tmp_path)
        before = Path(memo).read_text(encoding="utf-8")
        assert "kind: defect" in before

        result = _claim(memo, "sess-1", "2026-08-11T00:00:00Z")

        assert result["exit_code"] == 0
        assert result["applied"] is True

        after = Path(memo).read_text(encoding="utf-8")
        assert "kind: defect" in after  # byte-identical kind: field

        fm = _fm_dict(memo)
        assert fm["status"] == "in_progress"
        assert fm["picked_up_by"] == "sess-1"
        assert fm["picked_up_at"] == "2026-08-11T00:00:00Z"
        assert fm["kind"] == "defect"

        captured = capsys.readouterr()
        assert "kind" in captured.err
        assert "unrecognized" in captured.err
        assert "defaulted to 'ask'" in captured.err

    def test_action_succeeds_on_unenumerated_kind(self, tmp_path):
        """action: an in_progress memo with an off-enum kind can still be actioned."""
        in_progress_fixture = """\
---
kind: blocked-request-correction
status: in_progress
picked_up_at: '2026-08-11T00:00:00Z'
picked_up_by: sess-1
from: sender-session
summary: A test memo with an unenumerated kind.
created: 2026-06-01
---
"""
        memo = self._setup_memo(tmp_path, in_progress_fixture)
        result = _action(memo, {"decision": "declined"})
        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["kind"] == "blocked-request-correction"
        assert fm["status"] == "actioned"

    def test_release_succeeds_on_unenumerated_kind(self, tmp_path):
        """release: an in_progress memo with an off-enum kind can still be released."""
        in_progress_fixture = """\
---
kind: defect
status: in_progress
picked_up_at: '2026-08-11T00:00:00Z'
picked_up_by: sess-1
from: sender-session
summary: A test memo with an unenumerated kind.
created: 2026-06-01
---
"""
        memo = self._setup_memo(tmp_path, in_progress_fixture)
        result = _release(memo)
        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["kind"] == "defect"
        assert fm["status"] == "open"
        assert "picked_up_by" not in fm

    def test_resolve_succeeds_on_unenumerated_kind(self, tmp_path):
        """resolve: an open memo with an off-enum kind can go straight to actioned."""
        memo = self._setup_memo(tmp_path)
        result = _resolve(
            memo, "sess-1", "2026-08-11T00:00:00Z", {"decision": "declined"},
        )
        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["kind"] == "defect"
        assert fm["status"] == "actioned"


# ---------------------------------------------------------------------------
# --superseded-by (receiver-side supersession pair)
#
# Spec: docs/plans/2026-08-11-receiver-side-supersession-pair-a-writab.md
# C2, AC1/AC3/AC6.
# ---------------------------------------------------------------------------

class TestSupersededBy:
    """_action's superseded_by branch — status: superseded, pointer validation,
    idempotency, and byte-identical actioned-path regression (AC6)."""

    _IN_PROGRESS_FIXTURE = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_repo_with_memo(self, tmp_path: Path, content: str | None = None) -> tuple[Path, str]:
        """Create a git repo + in-progress memo under cross-repo/inbox/, tracked
        in HEAD. Returns (repo_root, memo_path)."""
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(content or self._IN_PROGRESS_FIXTURE, encoding="utf-8")
        _git_track(repo, memo)
        return repo, str(memo)

    def _seed_pointer_target(self, repo: Path, name: str, in_archive: bool = False) -> None:
        """Seed a memo the superseded_by pointer can resolve against, in either
        cross-repo/inbox/ or cross-repo/archive/ (per AC3's two legal locations)."""
        subdir = "archive" if in_archive else "inbox"
        target_dir = repo / "cross-repo" / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        target.write_text(
            "---\nkind: fyi\nstatus: open\nfrom: peer\nsummary: pointer target.\ncreated: 2026-06-01\n---\n",
            encoding="utf-8",
        )
        _git_track(repo, target)

    def test_superseded_by_writes_pair_and_validates(self, tmp_path):
        """AC1: writes status: superseded + superseded_by in one locked_rmw
        closure; the result round-trips through the real schema validator."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        self._seed_pointer_target(repo, "successor.md")

        result = _action(memo, {"superseded_by": "successor.md"})
        assert result["exit_code"] == 0
        assert result["applied"] is True

        fm = _fm_dict(memo)
        assert fm["status"] == "superseded"
        assert fm["superseded_by"] == "successor.md"
        assert validate_memo_cross_fields(fm) == []

    def test_superseded_by_accepts_a_path_normalized_to_basename(self, tmp_path):
        """AC3 shape: a path (not a bare basename) resolves the same way
        memo_send._normalize_in_reply_to accepts either shape — the emitted
        frontmatter value is always the basename."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        self._seed_pointer_target(repo, "successor.md", in_archive=True)

        result = _action(memo, {"superseded_by": "cross-repo/archive/successor.md"})
        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["superseded_by"] == "successor.md"

    def test_superseded_by_nonexistent_pointer_fails_loud_no_write(self, tmp_path):
        """AC3: a --superseded-by value naming a memo in neither inbox nor
        archive fails loud (exit 1) before any write."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        before = Path(memo).read_bytes()

        result = _action(memo, {"superseded_by": "typo-does-not-exist.md"})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "does not match any memo" in result["error"]
        assert Path(memo).read_bytes() == before

    def test_superseded_by_rerun_same_pointer_idempotent(self, tmp_path):
        """Re-running with the SAME pointer is idempotent — no-op, exit 0."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        self._seed_pointer_target(repo, "successor.md")

        first = _action(memo, {"superseded_by": "successor.md"})
        assert first["exit_code"] == 0
        assert first["applied"] is True

        second = _action(memo, {"superseded_by": "successor.md"})
        assert second["exit_code"] == 0
        assert second["applied"] is False

    def test_superseded_by_rerun_different_pointer_fails_loud(self, tmp_path):
        """Re-running with a DIFFERENT pointer fails loud, mirroring the
        existing already-actioned-with-a-different-disposition raise."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        self._seed_pointer_target(repo, "successor.md")
        self._seed_pointer_target(repo, "other-successor.md")

        first = _action(memo, {"superseded_by": "successor.md"})
        assert first["exit_code"] == 0

        second = _action(memo, {"superseded_by": "other-successor.md"})
        assert second["exit_code"] == 1
        assert "cannot re-action" in second["error"]
        fm = _fm_dict(memo)
        assert fm["superseded_by"] == "successor.md"  # unchanged

    def test_superseded_by_and_decision_mutually_exclusive(self, tmp_path):
        """_validate_action_disposition refuses the combination at the op
        boundary too (archive_stamp.py's CLI layer already refuses this
        earlier — this is the same discipline applied here)."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        self._seed_pointer_target(repo, "successor.md")
        before = Path(memo).read_bytes()

        result = _action(memo, {"superseded_by": "successor.md", "decision": "accepted"})
        assert result["exit_code"] == 1
        assert result["applied"] is False
        assert "mutually exclusive" in result["error"]
        assert Path(memo).read_bytes() == before

    def test_actioned_path_unaffected_by_superseded_by_addition(self, tmp_path):
        """AC6 regression: an ordinary --decision action, with no superseded_by
        param at all, is unaffected — still writes status: actioned."""
        repo, memo = self._setup_repo_with_memo(tmp_path)
        result = _action(memo, {"decision": "declined"})
        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["status"] == "actioned"
        assert "superseded_by" not in fm


# ---------------------------------------------------------------------------
# (5) close verb — DEFECT 1 repro (2026-08-12 inbox-blitz-dominant-verify-wave-b,
# item 6§3 / audit item 3): the memo status enum
# (coordinator_core/contract/emit_memo_schema.py) permits "closed", but no verb
# in this module ever writes it — claim/action/release/resolve top out at
# "actioned"/"superseded". The `closed`-only-counts-as-closed downstream gate
# can therefore never be satisfied via any sanctioned mutation path.
#
# _close (actioned -> closed) makes the state reachable: it requires status ==
# "actioned", and stamps closed_at + action_taken_at (backfilled from --at when
# absent) + preserves decision, satisfying
# schema_validate._memo_cf_closed_requires_companions.
# ---------------------------------------------------------------------------

class TestClose:
    """_close: actioned -> closed, the previously-unreachable terminal state."""

    _ACTIONED_FIXTURE = """\
---
kind: fyi
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-test
decision: accepted
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

    def _setup_memo(self, tmp_path: Path) -> str:
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(self._ACTIONED_FIXTURE, encoding="utf-8")
        _git_track(repo, memo)
        return str(memo)

    def test_close_verb_exists(self):
        """Reproduces DEFECT 1: prior to the fix, _close does not exist at all."""
        assert _close is not None, (
            "coordinator_core.ops.memo_transition._close is missing — the memo "
            "status enum's 'closed' value has no writer, so the closed state "
            "is unreachable via any sanctioned op (DEFECT 1)."
        )

    def test_close_actioned_memo_reaches_closed(self, tmp_path):
        if _close is None:
            pytest.skip("_close not implemented yet — see test_close_verb_exists")
        memo = self._setup_memo(tmp_path)
        result = _close(memo, "2026-08-12T00:00:00Z")
        assert result["exit_code"] == 0
        assert result["applied"] is True
        fm = _fm_dict(memo)
        assert fm["status"] == "closed"
        assert fm["closed_at"] == "2026-08-12T00:00:00Z"
        assert fm["action_taken_at"] == "2026-08-12T00:00:00Z"
        assert fm["decision"] == "accepted"

    def test_close_passes_cross_field_validation(self, tmp_path):
        """The written frontmatter must satisfy
        schema_validate._memo_cf_closed_requires_companions (closed_at,
        action_taken_at, decision all present when status=closed)."""
        if _close is None:
            pytest.skip("_close not implemented yet — see test_close_verb_exists")
        memo = self._setup_memo(tmp_path)
        _close(memo, "2026-08-12T00:00:00Z")
        fm = _fm_dict(memo)
        errors = validate_memo_cross_fields(fm)
        assert errors == []

    def test_close_non_actioned_memo_fails_loud(self, tmp_path):
        if _close is None:
            pytest.skip("_close not implemented yet — see test_close_verb_exists")
        repo = tmp_path / "repo"
        _git_init(repo)
        inbox = repo / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        memo = inbox / "memo.md"
        memo.write_text(
            "---\nkind: fyi\nstatus: open\nfrom: sender-session\n"
            "summary: A test memo.\ncreated: 2026-06-01\n---\n",
            encoding="utf-8",
        )
        _git_track(repo, memo)
        result = _close(str(memo), "2026-08-12T00:00:00Z")
        assert result["exit_code"] == 1
        assert result["applied"] is False

    def test_close_idempotent_when_already_closed(self, tmp_path):
        if _close is None:
            pytest.skip("_close not implemented yet — see test_close_verb_exists")
        memo = self._setup_memo(tmp_path)
        first = _close(memo, "2026-08-12T00:00:00Z")
        assert first["exit_code"] == 0
        second = _close(memo, "2026-08-12T00:00:00Z")
        assert second["exit_code"] == 0
        assert second["applied"] is False
