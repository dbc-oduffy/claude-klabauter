"""
distill_fate / in_repo_capture parity tests for coordinator_core.ops.memo_transition._action.

Purpose: closes a scope-drop found while porting example-doctrine-repo coordinator/bin/memo-transition.js
(BIG_PORT item memo-transition, Wave B). The pre-existing native port
(coordinator_core/ops/memo_transition.py, landed under
docs/plans/2026-07-06-memo-transition-native-python-port.md) predates the JS oracle's
distill_fate/in_repo_capture stamp-at-source fields (Finding #11, C3,
docs/plans/2026-07-12-distill-rebuild-claude-klabauter-reliant.md § C3) — the native op silently
dropped those two fields even though the example-doctrine-repo-side strangler facade
(coordinator/lib/memo-transition-facade.sh) already forwarded them in its params JSON.
This file independently re-derives parity against the JS oracle's exact field-write and
cross-field-validation behavior (memo-transition.js:423-453 write logic;
coordinator/bin/lib/schema.js:2236-2280 cross-field rule) rather than re-asserting this
session's own transcription.

Parity oracle: example-doctrine-repo coordinator/bin/memo-transition.js + coordinator/bin/lib/schema.js
Spec backlink: docs/plans/2026-07-12-distill-rebuild-claude-klabauter-reliant.md § C3
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import validate_memo_cross_fields
from coordinator_core.ops.memo_transition import _action


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
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


def _setup_memo(tmp_path: Path, content: str = _IN_PROGRESS_FIXTURE) -> str:
    """Create a git repo + memo under cross-repo/inbox/, tracked in HEAD.

    The memo is committed at setup because that is the only state a transition
    verb ever observes in production: `coordinator/bin/cross-repo-memo`'s
    `_commit_delivered_memo` stages and commits the delivered memo in the
    receiver repo before any verb can run against it. An untracked fixture
    describes a state delivery cannot produce, and the commit path
    (`git_native.commit_authored_content`) refuses a path absent from HEAD by
    design — it exists to mutate an existing file in place.
    """
    repo = tmp_path / "repo"
    _git_init(repo)
    inbox = repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    memo = inbox / "memo.md"
    memo.write_text(content, encoding="utf-8")
    _git_track(repo, memo)
    return str(memo)


# ---------------------------------------------------------------------------
# (1) _action writes distill_fate / in_repo_capture atomically alongside decision
# ---------------------------------------------------------------------------

class TestActionWritesDistillFate:
    def test_decision_shape_writes_distill_fate_after_realized_by(self, tmp_path):
        """Port of memo-transition.js:431-443 anchor logic (decision + realized_by present)."""
        memo = _setup_memo(tmp_path)
        result = _action(memo, {
            "decision": "accepted",
            "realized_by": "inline",
            "distill_fate": "commitment",
        })
        assert result["exit_code"] == 0
        assert result["applied"] is True

        text = Path(memo).read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.startswith(("realized_by:", "distill_fate:"))]
        assert lines == ["realized_by: inline", "distill_fate: commitment"], (
            "distill_fate must be anchored immediately after realized_by (last field written "
            "in the decision+realized_by shape), matching memo-transition.js's anchor chain"
        )

    def test_note_shape_writes_distill_fate_after_actioned_note(self, tmp_path):
        """Port of memo-transition.js's consult/fyi anchor branch (anchor = 'actioned_note')."""
        memo = _setup_memo(tmp_path)
        result = _action(memo, {"actioned_note": "noted", "distill_fate": "ephemeral"})
        assert result["exit_code"] == 0

        text = Path(memo).read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.startswith(("actioned_note:", "distill_fate:"))]
        assert lines == ["actioned_note: noted", "distill_fate: ephemeral"]

    def test_in_repo_capture_anchored_after_distill_fate_when_both_present(self, tmp_path):
        """Port of memo-transition.js:444-453: in_repo_capture anchors after distill_fate
        when both are supplied, not after the decision-chain field directly."""
        memo = _setup_memo(tmp_path)
        result = _action(memo, {
            "decision": "declined",
            "distill_fate": "ratification",
            "in_repo_capture": "docs/decisions/DR-999-fixture.md",
        })
        assert result["exit_code"] == 0

        text = Path(memo).read_text(encoding="utf-8")
        lines = [
            l for l in text.splitlines()
            if l.startswith(("decision:", "distill_fate:", "in_repo_capture:"))
        ]
        assert lines == [
            "decision: declined",
            "distill_fate: ratification",
            "in_repo_capture: docs/decisions/DR-999-fixture.md",
        ]

    def test_in_repo_capture_alone_anchored_after_decision_chain(self, tmp_path):
        """in_repo_capture without distill_fate falls back to the decision-chain anchor
        (port of memo-transition.js:445-447 ternary fallback)."""
        memo = _setup_memo(tmp_path)
        result = _action(memo, {
            "decision": "declined",
            "in_repo_capture": "docs/wiki/some-topic.md",
        })
        assert result["exit_code"] == 0

        text = Path(memo).read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.startswith(("decision:", "in_repo_capture:"))]
        assert lines == ["decision: declined", "in_repo_capture: docs/wiki/some-topic.md"]

    def test_absent_distill_fate_and_in_repo_capture_write_nothing(self, tmp_path):
        """Negative-spec: when the caller omits both C3 fields, neither key is written —
        the op must not silently backfill defaults."""
        memo = _setup_memo(tmp_path)
        result = _action(memo, {"decision": "declined"})
        assert result["exit_code"] == 0

        text = Path(memo).read_text(encoding="utf-8")
        assert "distill_fate:" not in text
        assert "in_repo_capture:" not in text


# ---------------------------------------------------------------------------
# (2) Post-write self-verify guards the two new C3 keys (dup-key corruption class)
# ---------------------------------------------------------------------------

class TestActionSelfVerifiesDistillFateKeyCount:
    def test_reapplying_same_disposition_with_distill_fate_is_idempotent_no_op(self, tmp_path):
        """Re-invoking action() with the SAME disposition (including distill_fate) on an
        already-actioned memo is a no-op — mirrors the oracle's idempotency branch, and the
        self-verify counters must not misfire on the no-op path (new_text == old_text)."""
        memo = _setup_memo(tmp_path)
        first = _action(memo, {"decision": "declined", "distill_fate": "ephemeral"})
        assert first["exit_code"] == 0
        second = _action(memo, {"decision": "declined", "distill_fate": "ephemeral"})
        assert second["exit_code"] == 0
        assert second["applied"] is False


# ---------------------------------------------------------------------------
# (3) Cross-field validation — port of schema.js:2236-2280 (memo_cf_distill_fate)
# ---------------------------------------------------------------------------

class TestDistillFateCrossFieldValidation:
    def test_invalid_enum_value_rejected(self):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "bogus",
        })
        assert any(e["field"] == "distill_fate" for e in errors)

    def test_absent_distill_fate_is_valid(self):
        errors = validate_memo_cross_fields({"created": "2026-06-01", "status": "open"})
        assert not any(e.get("field") == "distill_fate" for e in errors)

    def test_ephemeral_imposes_no_in_repo_capture_requirement(self):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "ephemeral",
        })
        assert errors == []

    def test_commitment_imposes_no_in_repo_capture_requirement(self):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "commitment",
        })
        assert errors == []

    def test_ratification_without_in_repo_capture_rejected(self):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "ratification",
        })
        assert any(e["field"] == "in_repo_capture" and "required" in e["error"] for e in errors)

    def test_ratification_with_claude_home_pointer_rejected(self):
        """Finding #12: a ~/.claude memory pointer is never durable capture."""
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "ratification",
            "in_repo_capture": "~/.claude/state/lessons/foo.yaml",
        })
        assert any(e["field"] == "in_repo_capture" and "malformed" in e["error"] for e in errors)

    def test_ratification_with_absolute_dotclaude_path_rejected(self):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "ratification",
            "in_repo_capture": "/Users/someone/.claude/state/lessons/foo.yaml",
        })
        assert any(e["field"] == "in_repo_capture" for e in errors)

    @pytest.mark.parametrize("capture", [
        "docs/decisions/DR-999-fixture.md",
        "docs/wiki/some-topic.md",
        "state/cross-repo-commitments/foo.md",
        "docs/plans/2026-07-12-foo.md",
        "coordinator/docs/decisions/DR-1-fixture.md",
        "coordinator/docs/wiki/some-topic.md",
        "coordinator/docs/plans/2026-07-12-foo.md",
    ])
    def test_ratification_with_valid_in_repo_homes_accepted(self, capture):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "ratification",
            "in_repo_capture": capture,
        })
        assert errors == []

    def test_ratification_with_non_home_path_rejected(self):
        errors = validate_memo_cross_fields({
            "created": "2026-06-01", "status": "open", "distill_fate": "ratification",
            "in_repo_capture": "random/scratch/notes.md",
        })
        assert any(e["field"] == "in_repo_capture" for e in errors)

    def test_grandfathered_memo_skips_distill_fate_rule_entirely(self):
        """created < 2026-05-22 grandfather cutoff skips ALL cross-field rules, including
        an otherwise-invalid distill_fate enum value (port of schema.js's __skip__ sentinel)."""
        errors = validate_memo_cross_fields({
            "created": "2026-05-01", "status": "open", "distill_fate": "bogus",
        })
        assert errors == []
