"""
Tests for `_memo_compose.body_opens_frontmatter` and its refusal wiring at
memo.compose (`memo_compose._validate_compose_params`) and memo.send
(`memo_send._memo_send`) — C1,
docs/plans/2026-09-11-memo-send-path-fail-loud.md.

Pins the observed defect shape: a caller pastes a WHOLE memo draft (its own
`---` frontmatter plus body) as the `body` wire param, instead of only the
prose that belongs below the closing `---` — memo.draft owns the
frontmatter, so a body that opens its own frontmatter block delivers a memo
whose body carries a spurious second YAML document. Both fixtures are real
memos observed in this shape (`state/cross-repo/archive/2026-07-31-market-
intelligence-em-x-seven-day-horizon-delta.md`,
`state/cross-repo/archive/2026-08-01-example-market-data-repo-em-enabler-d-
corpus-read-contract.md`), copied byte-for-byte.

Negative-spec: does NOT test the CLI (`cross-repo-memo compose --open`) —
`_cmd_compose`'s existing `except RuntimeError` arm already maps any
memo.compose setup-error to exit 1, so no CLI edit is needed for this
refusal to reach the CLI as a non-zero exit (see memo_compose.py's own
comment near its RuntimeError-raising call site, and the plan row body's
point (g)).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.fleet._memo_compose import body_opens_frontmatter
from coordinator_core.ops.fleet.memo_compose import _memo_compose
from coordinator_core.ops.fleet.memo_send import _memo_send

from .test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "doubled_frontmatter"

_FIXTURE_FILES = (
    "2026-07-31-example-market-data-repo-em-x-seven-day-horizon-delta.md.txt",
    "2026-08-01-example-market-data-repo-em-enabler-d-corpus-read-contract.md.txt",
)


def _fixture_body(name: str) -> str:
    """The parse_frontmatter BODY of a fixture memo — the text below the
    receiver's own delivery header, which is itself a second, spurious
    frontmatter block (the observed defect shape)."""
    text = (_FIXTURE_DIR / name).read_text(encoding="utf-8")
    return parse_frontmatter(text)["body"]


# ---------------------------------------------------------------------------
# (d) Fixture bodies — positive cases, LF and CRLF
# ---------------------------------------------------------------------------

class TestFixtureBodiesOpenFrontmatter:
    @pytest.mark.parametrize("name", _FIXTURE_FILES)
    def test_fixture_body_lf_is_true(self, name):
        assert body_opens_frontmatter(_fixture_body(name)) is True

    @pytest.mark.parametrize("name", _FIXTURE_FILES)
    def test_fixture_body_crlf_is_true(self, name):
        crlf_body = _fixture_body(name).replace("\n", "\r\n")
        assert body_opens_frontmatter(crlf_body) is True


# ---------------------------------------------------------------------------
# (e) Regression cases — written BEFORE the positive predicate assertions
# above in review order, each False AND composing cleanly through
# _memo_compose (exit_code 0).
# ---------------------------------------------------------------------------

class TestRegressionBodiesDoNotOpenFrontmatter:
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("Some prose.\n\n---\n\nMore prose after a mid-doc rule.\n", id="mid_doc_rule"),
            pytest.param(
                "```yaml\ntitle: not real frontmatter\n```\n\nProse follows.\n",
                id="fenced_yaml_block",
            ),
            pytest.param("\n\n\nProse after blank lines.\n", id="blank_lines_then_prose"),
            pytest.param(
                "---\n\nProse paragraph, not a YAML mapping.\n\n---\n\nMore prose.\n",
                id="rule_prose_rule",
            ),
            pytest.param("", id="empty_body"),
        ],
    )
    def test_predicate_is_false(self, body):
        assert body_opens_frontmatter(body) is False

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("Some prose.\n\n---\n\nMore prose after a mid-doc rule.\n", id="mid_doc_rule"),
            pytest.param(
                "```yaml\ntitle: not real frontmatter\n```\n\nProse follows.\n",
                id="fenced_yaml_block",
            ),
            pytest.param("\n\n\nProse after blank lines.\n", id="blank_lines_then_prose"),
            pytest.param(
                "---\n\nProse paragraph, not a YAML mapping.\n\n---\n\nMore prose.\n",
                id="rule_prose_rule",
            ),
            pytest.param("", id="empty_body"),
        ],
    )
    def test_composes_cleanly_through_memo_compose(self, tmp_path, monkeypatch, body):
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "regression-topic")

        params = {"dry_run": False, "topic": "regression-topic", "body": body}
        if not body:
            # DEC-1 (memo.compose's own unrelated gate) requires a non-empty
            # resolved summary — an empty body has no derivable prose, so an
            # explicit summary is supplied here to isolate THIS test from
            # that gate; this body still reaches (and clears)
            # body_opens_frontmatter's own check first.
            params["summary"] = "an explicit summary for an empty body"

        result = _memo_compose(params, repo_root=sender_repo)
        assert result["exit_code"] == 0, result


# ---------------------------------------------------------------------------
# (f) Op-level refusal — memo.compose and memo.send
# ---------------------------------------------------------------------------

pytestmark_op_level = [pytest.mark.cadence, pytest.mark.spawns_process]


class TestMemoComposeRefusesDoubledFrontmatterBody:
    pytestmark = pytestmark_op_level

    @pytest.mark.parametrize("name", _FIXTURE_FILES)
    def test_compose_refuses_and_draft_unchanged(self, tmp_path, monkeypatch, name):
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        draft_path = _write_draft(sender_repo, "doubled-fm-topic")
        before = draft_path.read_text(encoding="utf-8")

        result = _memo_compose(
            {
                "dry_run": False,
                "topic": "doubled-fm-topic",
                "body": _fixture_body(name),
            },
            repo_root=sender_repo,
        )

        assert result["exit_code"] == 1
        assert draft_path.read_text(encoding="utf-8") == before


class TestMemoSendRefusesDoubledFrontmatterBody:
    pytestmark = pytestmark_op_level

    @pytest.mark.parametrize("name", _FIXTURE_FILES)
    def test_send_refuses_and_no_receiver_file(self, tmp_path, monkeypatch, name):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "doubled-fm-send-topic", body=_fixture_body(name))

        result = _memo_send(
            {"dry_run": False, "topic": "doubled-fm-send-topic"},
            repo_root=sender_repo,
        )

        assert result["exit_code"] == 1
        inbox_dir = receiver_repo / "cross-repo" / "inbox"
        delivered = [p for p in inbox_dir.iterdir() if p.name != ".gitkeep"]
        assert delivered == []
