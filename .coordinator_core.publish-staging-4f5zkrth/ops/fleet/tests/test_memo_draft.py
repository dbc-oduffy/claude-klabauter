"""
Tests for coordinator_core.ops.fleet.memo_draft — memo.draft native UDS op.

C5 test surface (docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C5, AC5):
  - dry_run vs act envelope shape (exit_code, mode, dry_run, candidates/acted/failed keys)
  - writes into the CALLING repo's own state/memo-outbox/<topic>.md (local-tree write)
  - O_EXCL fail-loud on an existing draft with the same topic (no clobber)
  - setup-error envelope on bad params (missing required fields, bad types, bad kind)
  - store-less-ness architecture test (mirrors AC8/strang-03 C6's test_no_memo_index)

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency.

Spec backlink: pln-memo-tool-rebuild-makima-owns--bd5745 § C5
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import types
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.primitives import (
    split_frontmatter,
    read_fm_field,
    read_fm_field_unquoted,
)
from coordinator_core.ops.fleet.memo_draft import (
    _BODY_PLACEHOLDER,
    _MODE,
    REJECTION_CLASS_AMBIGUOUS_RECEIVER,
    REJECTION_CLASS_PUBLISH_TARGET,
    REJECTION_CLASS_REGISTRY_ERROR,
    REJECTION_CLASS_UNKNOWN_RECEIVER,
    _classify_receiver_for_draft,
    _memo_draft,
    _validate_draft_params,
    _validate_scoped_to,
    compose_draft_frontmatter,
)
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.memo_send import _SUMMARY_MAX_CHARS as _SEND_SUMMARY_MAX_CHARS
from coordinator_core.ops.fleet._memo_resolver import resolve_receiver_inbox, unique_nearest_receiver
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
    """Minimal git repo to serve as the caller repo (common_dir -> worktree)."""
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


def _base_params(**overrides) -> dict:
    params = {
        "dry_run": True,
        "topic": "some-topic",
        "to": "project-rag-em",
        "title": "A draft memo",
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# Registry fixture factory — mirrors test_memo_send.py's _make_claude_home,
# extended with an optional publish.mirrors.* table (mirror C5's classify_receiver
# surface, which needs both repos.* AND publish.mirrors.* fixtures).
# ---------------------------------------------------------------------------

def _make_claude_home(
    tmp_path: Path,
    receiver_repos: dict[str, Path] | None = None,
    mirror_owners: dict[str, str] | None = None,
) -> Path:
    """Create a fake CLAUDE_HOME with a machine-local registry.

    receiver_repos: {registry_key_suffix: repo_path} -> writes "repos.<suffix>" =
    <path> in registry.local.toml (mirrors test_memo_send.py's fixture).
    mirror_owners: {mirror_key: owner_em_id} -> writes a [publish.mirrors.<key>]
    table with owner = "<owner_em_id>" in registry.toml (the tracked-baseline
    home for .owner per the real registry.toml convention — see
    _memo_resolver.read_publish_mirror_owners docstring).
    """
    receiver_repos = receiver_repos or {}
    mirror_owners = mirror_owners or {}
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)

    baseline_lines = ["schema = 1"]
    for mirror_key, owner in mirror_owners.items():
        baseline_lines.append(f"\n[publish.mirrors.{mirror_key}]")
        baseline_lines.append(f'owner = "{owner}"')
    (machine_local / "registry.toml").write_text(
        "\n".join(baseline_lines) + "\n", encoding="utf-8",
    )

    local_lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        local_lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(local_lines) + "\n", encoding="utf-8",
    )

    return claude_home


# ===========================================================================
# 1. Param validation
# ===========================================================================

class TestValidateDraftParams:
    def test_missing_dry_run(self):
        result = _validate_draft_params({"topic": "t", "to": "x", "title": "T"})
        assert result["exit_code"] == 1

    def test_bad_topic_slug(self):
        result = _validate_draft_params(_base_params(topic="Bad Topic/../x"))
        assert result["exit_code"] == 1

    def test_missing_to(self):
        params = _base_params()
        del params["to"]
        result = _validate_draft_params(params)
        assert result["exit_code"] == 1

    def test_missing_title(self):
        params = _base_params()
        del params["title"]
        result = _validate_draft_params(params)
        assert result["exit_code"] == 1

    def test_invalid_kind(self):
        result = _validate_draft_params(_base_params(kind="not-a-kind"))
        assert result["exit_code"] == 1

    def test_valid_params_pass_through(self):
        result = _validate_draft_params(_base_params(dry_run=False, kind="ask", summary="hi"))
        assert result == (
            False, "some-topic", "project-rag-em", "A draft memo", "hi", "ask", None, False,
            None, None, None, None,
        )

    def test_bad_classify_receiver_type(self):
        result = _validate_draft_params(_base_params(classify_receiver="yes"))
        assert result["exit_code"] == 1

    def test_explicit_summary_over_cap_warns_but_still_returns_summary(self):
        """2026-08-07 warn-at-draft split (docs/plans/2026-08-07-memo-summary-
        cap-warn-at-draft.md § C2): an EXPLICITLY authored over-cap summary no
        longer fails draft-param-validation loud (memo.compose/memo.send
        still hard-refuse, unchanged) — it validates, the original text is
        returned unchanged (the handler decides how to keep it out of
        `summary:`), and the last tuple element carries a non-None advisory
        naming the actual length and the cap."""
        long_summary = "S" * (_SUMMARY_MAX_CHARS + 1)
        result = _validate_draft_params(_base_params(dry_run=False, summary=long_summary))
        assert result == (
            False, "some-topic", "project-rag-em", "A draft memo", long_summary,
            None, None, False, None, None, None, result[11],
        )
        advisory = result[11]
        assert advisory is not None
        assert str(len(long_summary)) in advisory
        assert str(_SUMMARY_MAX_CHARS) in advisory

    def test_explicit_summary_at_cap_passes(self):
        """A summary exactly AT the cap is valid — the gate is strictly-greater-than."""
        at_cap_summary = "S" * _SUMMARY_MAX_CHARS
        result = _validate_draft_params(_base_params(dry_run=False, summary=at_cap_summary))
        assert result == (
            False, "some-topic", "project-rag-em", "A draft memo", at_cap_summary,
            None, None, False, None, None, None, None,
        )

    def test_draft_and_send_agree_on_the_summary_cap(self):
        """The 120-char cap must exist exactly once, not as two magic numbers
        that could drift apart — memo_draft and memo_send both import
        _SUMMARY_MAX_CHARS from the shared _memo_summary module, so this
        would fail if either op ever hardcoded its own value instead of
        importing the single source of truth."""
        assert _SUMMARY_MAX_CHARS == _SEND_SUMMARY_MAX_CHARS


# ===========================================================================
# 1b. scoped_to — presence-triggered completeness (2026-07-21 break-class fix)
#
# Source break-class finding: memo.draft silently dropped scoped_to,
# exit_code:0 — same defect class memo.send's C9/A11 fix closed. These tests
# assert memo.draft is now total over scoped_to: it either arrives in the
# draft file (as a real nested YAML mapping) or the call fails loud.
# ===========================================================================

class TestScopedToValidation:
    def test_absent_scoped_to_passes(self):
        assert _validate_scoped_to(True, None) is None

    def test_complete_triple_with_version_passes(self):
        assert _validate_scoped_to(
            True, {"artifact": "coordinator_core", "version": "1.2.3", "seam": "memo_draft"},
        ) is None

    def test_complete_triple_with_sha_passes(self):
        assert _validate_scoped_to(
            True, {"artifact": "coordinator_core", "sha": "deadbeef", "seam": "memo_draft"},
        ) is None

    def test_partial_triple_missing_seam_fails_loud(self):
        result = _validate_scoped_to(
            True, {"artifact": "coordinator_core", "version": "1.2.3"},
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_missing_artifact_fails_loud(self):
        result = _validate_scoped_to(
            True, {"version": "1.2.3", "seam": "memo_draft"},
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_both_version_and_sha_fails_loud(self):
        result = _validate_scoped_to(
            True,
            {"artifact": "coordinator_core", "version": "1.2.3", "sha": "deadbeef", "seam": "x"},
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_unknown_subkey_fails_loud(self):
        result = _validate_scoped_to(
            True, {"artifact": "x", "seam": "y", "bogus": "z"},
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_not_a_mapping_fails_loud(self):
        result = _validate_scoped_to(True, "not-a-dict")
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_validate_draft_params_rejects_partial_scoped_to(self):
        result = _validate_draft_params(_base_params(
            scoped_to={"artifact": "coordinator_core", "version": "1.2.3"},
        ))
        assert result["exit_code"] == 1

    def test_validate_draft_params_accepts_complete_scoped_to(self):
        scoped_to = {"artifact": "coordinator_core", "version": "1.2.3", "seam": "memo_draft"}
        result = _validate_draft_params(_base_params(scoped_to=scoped_to))
        assert result == (
            True, "some-topic", "project-rag-em", "A draft memo", None, None, scoped_to, False,
            None, None, None, None,
        )


class TestScopedToEndToEnd:
    def test_complete_scoped_to_round_trips_as_nested_mapping(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        scoped_to = {"artifact": "coordinator_core", "version": "1.2.3", "seam": "memo_draft"}

        result = _run(_memo_draft(
            _base_params(dry_run=False, topic="scoped-to-test", scoped_to=scoped_to),
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0, f"scoped_to draft should succeed: {result}"

        target = sender / "state" / "memo-outbox" / "scoped-to-test.md"
        content = target.read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert split is not None
        frontmatter = yaml.safe_load(split.fm_text)
        assert frontmatter["scoped_to"] == scoped_to, (
            f"scoped_to must round-trip as an equal nested mapping; got: "
            f"{frontmatter.get('scoped_to')!r}"
        )
        assert "scoped_to:\n" in content, (
            "scoped_to must render as a bare mapping key, not a quoted scalar line"
        )
        assert not re.search(r'scoped_to:\s*"', content), (
            "scoped_to must not be flattened into a double-quoted scalar"
        )

    def test_partial_scoped_to_handler_exits_nonzero_and_writes_no_file(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"

        result = _run(_memo_draft(
            _base_params(
                dry_run=False, topic="partial-scoped-to-test",
                scoped_to={"artifact": "coordinator_core", "seam": "memo_draft"},
            ),
            repo_root=common_dir,
        ))
        assert result["exit_code"] != 0
        assert not (sender / "state" / "memo-outbox" / "partial-scoped-to-test.md").exists()


# ===========================================================================
# 2. dry_run preview — no write
# ===========================================================================

class TestDryRunPreview:
    def test_dry_run_no_write(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(_base_params(dry_run=True), repo_root=common_dir))

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        assert result["dry_run"] is True
        assert result["acted"] == []
        assert not (sender / "state" / "memo-outbox" / "some-topic.md").exists()
        assert result["candidates"][0]["collision"] is False

    def test_dry_run_reports_collision(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        outbox = sender / "state" / "memo-outbox"
        outbox.mkdir(parents=True)
        (outbox / "some-topic.md").write_text("existing", encoding="utf-8")

        result = _run(_memo_draft(_base_params(dry_run=True), repo_root=common_dir))
        assert result["candidates"][0]["collision"] is True


# ===========================================================================
# 3. act path — writes a schema-shaped draft
# ===========================================================================

class TestActWritesDraft:
    def test_act_writes_draft_file(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(_base_params(dry_run=False, kind="ask"), repo_root=common_dir))

        assert result["exit_code"] == 0
        target = sender / "state" / "memo-outbox" / "some-topic.md"
        assert target.exists()

        content = target.read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert split is not None
        assert read_fm_field(split.fm_text, "status") == "draft"
        assert read_fm_field(split.fm_text, "title") == '"A draft memo"'
        assert read_fm_field(split.fm_text, "to") == '"project-rag-em"'
        assert read_fm_field(split.fm_text, "kind") == '"ask"'
        # from: defaults to the engine actor id when from_id is not supplied.
        assert read_fm_field(split.fm_text, "from") == '"makima-engine"'

    def test_act_no_repo_root_is_setup_error(self):
        result = _run(_memo_draft(_base_params(dry_run=False), repo_root=None))
        assert result["exit_code"] == 1

    def test_act_collision_refuses_no_clobber(self, tmp_path):
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        outbox = sender / "state" / "memo-outbox"
        outbox.mkdir(parents=True)
        existing = outbox / "some-topic.md"
        existing.write_text("do-not-touch", encoding="utf-8")

        result = _run(_memo_draft(_base_params(dry_run=False), repo_root=common_dir))
        assert result["exit_code"] == 2
        assert result["failed"]
        assert existing.read_text(encoding="utf-8") == "do-not-touch"

    def test_scaffolded_body_states_the_summary_cap(self, tmp_path):
        """2026-07-26 draft-time-discoverability fix: the cap must be visible
        in the file the author is actually editing. Lives in the body
        placeholder (NOT a trailing YAML comment on the summary: line —
        that shape corrupts the field on re-parse, see _BODY_PLACEHOLDER's
        module-level comment for the verified-by-running rejection)."""
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(_base_params(dry_run=False), repo_root=common_dir))
        assert result["exit_code"] == 0

        target = sender / "state" / "memo-outbox" / "some-topic.md"
        content = target.read_text(encoding="utf-8")
        assert str(_SUMMARY_MAX_CHARS) in content

        # And critically: the summary: frontmatter field itself must round-
        # trip as the placeholder ruler (AC3/AC4, 2026-08-07 — was '""'),
        # not corrupted by the body notice, brackets/double-space intact.
        split = split_frontmatter(content)
        assert split is not None
        assert read_fm_field_unquoted(split.fm_text, "summary") == SUMMARY_PLACEHOLDER

    def test_no_receiver_validation_at_draft_time(self, tmp_path):
        """An unresolved/unregistered 'to' still drafts (C2/C4 own receiver validation)."""
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(
            _base_params(dry_run=False, to="totally-unregistered-em"), repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert (sender / "state" / "memo-outbox" / "some-topic.md").exists()

    def test_over_cap_summary_warns_writes_and_keeps_text_out_of_summary_field(
        self, tmp_path,
    ):
        """2026-08-07 warn-at-draft split (AC1, AC2): an over-cap explicit
        summary no longer blocks the draft. The draft is written, the acted
        item carries a non-None `summary_cap_advisory` naming the actual
        length and the cap, the original text is recoverable (preserved
        verbatim in the body), and — critically — it never lands in the
        `summary:` frontmatter field (that would be the silent truncation
        this whole surface exists to prevent)."""
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        over_cap_summary = "S" * (_SUMMARY_MAX_CHARS + 1)

        result = _run(_memo_draft(
            _base_params(dry_run=False, summary=over_cap_summary), repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert len(result["acted"]) == 1
        advisory = result["acted"][0]["summary_cap_advisory"]
        assert advisory is not None
        assert str(len(over_cap_summary)) in advisory
        assert str(_SUMMARY_MAX_CHARS) in advisory

        target = sender / "state" / "memo-outbox" / "some-topic.md"
        content = target.read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert split is not None

        # NOT silently written/truncated into summary: — placeholder instead.
        assert read_fm_field_unquoted(split.fm_text, "summary") == SUMMARY_PLACEHOLDER
        assert over_cap_summary not in split.fm_text

        # Recoverable: the original over-cap text survives in the body.
        assert over_cap_summary in content

    def test_clean_summary_advisory_is_none(self, tmp_path):
        """The advisory field is additive and None on an ordinary in-cap draft."""
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(
            _base_params(dry_run=False, summary="A short summary."), repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert result["acted"][0]["summary_cap_advisory"] is None

    def test_dry_run_over_cap_summary_advisory_present_no_write(self, tmp_path):
        """dry_run preview surfaces the same advisory without writing anything."""
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        over_cap_summary = "S" * (_SUMMARY_MAX_CHARS + 1)

        result = _run(_memo_draft(
            _base_params(dry_run=True, summary=over_cap_summary), repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        advisory = result["candidates"][0]["summary_cap_advisory"]
        assert advisory is not None
        assert not (sender / "state" / "memo-outbox" / "some-topic.md").exists()


# ===========================================================================
# 3b. classify_receiver — OPTIONAL receiver classification (C5 AC5 addition)
#
# Reuses the SAME resolution authority memo.send uses (_memo_resolver). These
# tests lock memo.draft's classification to memo.send's/_memo_resolver's own
# resolution so the two verbs can never disagree about whether `to` resolves.
# ===========================================================================

class TestClassifyReceiver:
    def test_absent_classify_receiver_still_portable_draft(self, tmp_path, monkeypatch):
        """(a) classify_receiver absent -> unresolved `to` still drafts (default intact)."""
        claude_home = _make_claude_home(tmp_path)  # empty registry
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(
            _base_params(dry_run=False, to="totally-unregistered-em"),
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert (sender / "state" / "memo-outbox" / "some-topic.md").exists()

    def test_false_classify_receiver_still_portable_draft(self, tmp_path, monkeypatch):
        """(a) classify_receiver explicitly False -> same portable-draft default."""
        claude_home = _make_claude_home(tmp_path)  # empty registry
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(
            _base_params(
                dry_run=False, topic="false-flag", to="totally-unregistered-em",
                classify_receiver=False,
            ),
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert (sender / "state" / "memo-outbox" / "false-flag.md").exists()

    def test_publish_target_rejected_no_file_written(self, tmp_path, monkeypatch, caplog):
        """(b) classify_receiver: True + publish-target `to` -> fail loud, no write."""
        claude_home = _make_claude_home(
            tmp_path, mirror_owners={"coordinator_claude": "claude-central-em"},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="mirror-test", to="coordinator-claude-em",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["exit_code"] == 1
        assert "PUBLISH-TARGET" in caplog.text
        assert "claude-central-em" in caplog.text
        assert not (sender / "state" / "memo-outbox" / "mirror-test.md").exists()

    def test_unknown_receiver_rejected_with_nearest_match(self, tmp_path, monkeypatch, caplog):
        """(c) classify_receiver: True + genuinely unresolvable `to` (no candidate
        clears the similarity cutoff) -> fail loud, no write, no did-you-mean.

        Uses a wildly-unrelated `to` rather than 'makima-em' -> the latter now
        AUTO-ACCEPTS via the unique-did-you-mean fix (2026-07-24 papercut fix,
        see test_unique_did_you_mean_auto_accepted below) instead of rejecting.
        """
        receiver_repo = tmp_path / "project-makima-repo"
        receiver_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path, receiver_repos={"project_makima": receiver_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="unknown-test", to="xyz-completely-unrelated-zzz",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["exit_code"] == 1
        assert "UNKNOWN RECEIVER" in caplog.text
        assert not (sender / "state" / "memo-outbox" / "unknown-test.md").exists()

    def test_unique_did_you_mean_auto_accepted(self, tmp_path, monkeypatch):
        """(c2) 2026-07-24 papercut fix: classify_receiver: True + `to` that does
        NOT resolve as typed but has EXACTLY ONE registered did-you-mean
        candidate ('makima-em' -> 'project-makima-em') -> drafts normally,
        with the RESOLVED id written into the draft's `to:` frontmatter and
        the acted-envelope `to` field (never the caller's unresolved literal).
        """
        receiver_repo = tmp_path / "project-makima-repo"
        receiver_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path, receiver_repos={"project_makima": receiver_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(
            _base_params(
                dry_run=False, topic="alias-test", to="makima-em",
                classify_receiver=True,
            ),
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert result["acted"][0]["to"] == "project-makima-em"
        draft_path = sender / "state" / "memo-outbox" / "alias-test.md"
        assert draft_path.exists()
        split = split_frontmatter(draft_path.read_text(encoding="utf-8"))
        assert read_fm_field_unquoted(split.fm_text, "to") == "project-makima-em"

    def test_ambiguous_did_you_mean_still_rejected(self, tmp_path, monkeypatch, caplog):
        """(c3) Review: code-reviewer — Finding 1 (2026-07-24): confirms the OTHER
        half of the uniqueness contract `unique_nearest_receiver` implements —
        TWO registered candidates equidistant/close enough to both clear the
        0.5 cutoff must NOT auto-accept. Unlike test_unique_did_you_mean_auto_accepted
        (exactly one candidate -> auto-accept), this registers two similarly-named
        receivers so the typo'd `to` resolves 2 candidates via
        difflib.get_close_matches(..., n=2, cutoff=0.5); `unique_nearest_receiver`
        must return None on len(matches) != 1, and _memo_draft must still hard-fail
        with UNKNOWN RECEIVER rather than silently picking either candidate.
        """
        alpha_repo = tmp_path / "project-alpha-repo"
        alpha_repo.mkdir()
        beta_repo = tmp_path / "project-beta-repo"
        beta_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            receiver_repos={
                "project_alpha": alpha_repo,
                "project_beta": beta_repo,
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Direct unit-level confirmation: unique_nearest_receiver returns None
        # when two candidates both clear the cutoff (ambiguous, not unique).
        all_repos = {
            "repos.project_alpha": str(alpha_repo),
            "repos.project_beta": str(beta_repo),
        }
        assert unique_nearest_receiver("project-em", all_repos) is None

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="ambiguous-test", to="project-em",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["exit_code"] == 1
        assert "UNKNOWN RECEIVER" in caplog.text
        assert not (sender / "state" / "memo-outbox" / "ambiguous-test.md").exists()

    def test_valid_receiver_drafts_normally(self, tmp_path, monkeypatch):
        """(d) classify_receiver: True + valid receiver -> drafts normally."""
        receiver_repo = tmp_path / "project-rag-repo"
        receiver_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path, receiver_repos={"project_rag": receiver_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        result = _run(_memo_draft(
            _base_params(dry_run=False, topic="valid-test", classify_receiver=True),
            repo_root=common_dir,
        ))
        assert result["exit_code"] == 0
        assert (sender / "state" / "memo-outbox" / "valid-test.md").exists()

    def test_classification_matches_memo_resolver_directly(self, tmp_path, monkeypatch):
        """(e) classification verdict matches _memo_resolver.resolve_receiver_inbox
        directly for the same `to` — locks memo.draft's classification to the
        SAME authority memo.send/memo.list consume, so the two can never
        disagree about whether `to` resolves.
        """
        receiver_repo = tmp_path / "project-rag-repo"
        receiver_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path, receiver_repos={"project_rag": receiver_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Resolved case: _classify_receiver_for_draft returns None (proceed)
        # exactly when _memo_resolver.resolve_receiver_inbox resolves.
        inbox_dir, receiver_repo_path, _ = resolve_receiver_inbox("project-rag-em")
        assert inbox_dir is not None
        assert _classify_receiver_for_draft("project-rag-em", dry_run=False) is None

        # Unknown case: _classify_receiver_for_draft rejects exactly when
        # resolve_receiver_inbox returns a zero-match.
        inbox_dir, _, _ = resolve_receiver_inbox("no-such-receiver-em")
        assert inbox_dir is None
        classify_result = _classify_receiver_for_draft("no-such-receiver-em", dry_run=False)
        assert isinstance(classify_result, dict)
        assert classify_result["exit_code"] == 1

    def test_classification_agrees_with_memo_send_dry_run(self, tmp_path, monkeypatch):
        """(e) end-to-end lock: a `to` memo.draft classifies clean for also
        dry-runs clean via memo.send — the two verbs never disagree.
        """
        receiver_repo = tmp_path / "project-rag-repo"
        receiver_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path, receiver_repos={"project_rag": receiver_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        assert _classify_receiver_for_draft("project-rag-em", dry_run=True) is None

        send_result = _run(_memo_send({
            "dry_run": True,
            "topic": "agreement-check",
            "to": "project-rag-em",
            "title": "T",
            "body": "B",
            "kind": "fyi",
            "summary": "Agreement check.",
        }))
        assert send_result["exit_code"] == 0


# ===========================================================================
# 3c. rejection_class — cross-repo wire field (2026-07-21, DoE consult)
#
# DoE claude-central-em's CLI previously mapped classify_receiver rejections
# to three distinct process exit codes (publish-target: 1, unknown receiver:
# 2, registry error: 3); collapsing to a single exit_code:1 setup-error
# envelope lost that split. rejection_class restores it on the wire as a
# stable, greppable string — additive, non-breaking, present iff the
# envelope came from _classify_receiver_for_draft. See memo_draft.py's
# REJECTION_CLASS_* constants and _memo_draft's Returns docstring section.
# ===========================================================================

_BASE_ENVELOPE_KEYS = {"exit_code", "mode", "dry_run", "candidates", "acted", "skipped", "failed"}


def _assert_base_envelope_unchanged(result: dict) -> None:
    """Assert the pre-existing setup-error envelope fields are unaffected.

    Every field build_setup_error_result has always produced keeps its
    current name/type/value; rejection_class is purely additive.
    """
    assert result["exit_code"] == 1
    assert result["mode"] == _MODE
    assert result["dry_run"] is False
    assert result["candidates"] == []
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


class TestRejectionClass:
    def test_publish_target_rejection_class(self, tmp_path, monkeypatch, caplog):
        """(a) publish-target rejection -> rejection_class="publish_target_rejected"."""
        claude_home = _make_claude_home(
            tmp_path, mirror_owners={"coordinator_claude": "claude-central-em"},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="mirror-rc-test", to="coordinator-claude-em",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["rejection_class"] == REJECTION_CLASS_PUBLISH_TARGET
        assert set(result.keys()) == _BASE_ENVELOPE_KEYS | {"rejection_class"}
        _assert_base_envelope_unchanged(result)

    def test_unknown_receiver_rejection_class(self, tmp_path, monkeypatch, caplog):
        """(b) unknown-receiver rejection -> rejection_class="unknown_receiver".

        Uses a wildly-unrelated `to` rather than 'makima-em' -> the latter now
        AUTO-ACCEPTS via the unique-did-you-mean fix (2026-07-24 papercut fix)
        instead of reaching this rejection branch at all.
        """
        receiver_repo = tmp_path / "project-makima-repo"
        receiver_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path, receiver_repos={"project_makima": receiver_repo},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="unknown-rc-test", to="xyz-completely-unrelated-zzz",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["rejection_class"] == REJECTION_CLASS_UNKNOWN_RECEIVER
        assert set(result.keys()) == _BASE_ENVELOPE_KEYS | {"rejection_class"}
        _assert_base_envelope_unchanged(result)

    def test_registry_error_rejection_class(self, tmp_path, monkeypatch, caplog):
        """(c) registry-unreadable rejection -> rejection_class="registry_error".

        Writes a syntactically-invalid registry.local.toml so
        _memo_resolver.read_registry_repos() raises RegistryReadError, which
        _classify_receiver_for_draft must translate into a
        registry_error-tagged setup-error envelope (mirrors DoE's prior
        exit-3 registry-error class).
        """
        claude_home = _make_claude_home(tmp_path)  # empty-but-present registry
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        (machine_local / "registry.local.toml").write_text(
            "this is not valid toml [[[\n", encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="registry-rc-test", to="any-receiver-em",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["rejection_class"] == REJECTION_CLASS_REGISTRY_ERROR
        assert set(result.keys()) == _BASE_ENVELOPE_KEYS | {"rejection_class"}
        _assert_base_envelope_unchanged(result)

    def test_ambiguous_receiver_rejection_class(self, tmp_path, monkeypatch, caplog):
        """(e) ambiguous-receiver rejection -> rejection_class="ambiguous_receiver".

        Mirrors test_memo_resolver.py's TestAmbiguousCentralReceiver fixture:
        two distinct central ids both registered to DIFFERENT repos.* keys is
        a genuine registry/manifest disagreement that resolve_receiver_inbox
        raises AmbiguousReceiverError for. This IS a receiver-classification
        rejection (the caller cannot proceed without disambiguating `to`), so
        it must carry rejection_class exactly like the other three branches
        — leaving it bare would falsify the "present iff classification
        rejection" invariant.
        """
        claude_home = _make_claude_home(
            tmp_path,
            receiver_repos={
                "central": tmp_path / "central-repo",
                "doe_claude": tmp_path / "doe-claude-repo",
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # repos.doe_claude is registered, so the DR-071 doe-root ladder's
        # registry rung resolves the DoE root — the manifest must live there,
        # and the pointer file goes on the durable settings-home rung.
        doe_root = tmp_path / "doe-claude-repo"
        schemas_dir = doe_root / "coordinator" / "schemas"
        schemas_dir.mkdir(parents=True, exist_ok=True)
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True, exist_ok=True)
        (machine_local / ".doe-root").write_text(str(doe_root), encoding="utf-8")
        import json as _json

        (schemas_dir / "coordinator-registry.manifest.json").write_text(
            _json.dumps(
                {
                    "identity": {
                        "centralReceiverIds": ["central-em", "doe-claude-em"],
                        "repoAliases": [],
                    }
                }
            ),
            encoding="utf-8",
        )

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        with caplog.at_level("ERROR"):
            result = _run(_memo_draft(
                _base_params(
                    dry_run=False, topic="ambiguous-rc-test", to="central-em",
                    classify_receiver=True,
                ),
                repo_root=common_dir,
            ))
        assert result["rejection_class"] == REJECTION_CLASS_AMBIGUOUS_RECEIVER
        assert set(result.keys()) == _BASE_ENVELOPE_KEYS | {"rejection_class"}
        _assert_base_envelope_unchanged(result)

    def test_param_validation_setup_error_has_no_rejection_class(self):
        """(d) negative: an ordinary param-validation setup error (bad type on
        classify_receiver itself, never reaching _classify_receiver_for_draft)
        must NOT carry rejection_class — presence is meaningful, and this
        path is not a receiver-classification rejection.
        """
        result = _validate_draft_params(_base_params(classify_receiver="yes"))
        assert isinstance(result, dict)
        assert result["exit_code"] == 1
        assert "rejection_class" not in result
        assert set(result.keys()) == _BASE_ENVELOPE_KEYS

    def test_handler_missing_repo_root_setup_error_has_no_rejection_class(self):
        """(d) negative: the no-repo_root setup error (a plumbing failure, not
        a receiver-classification rejection) must NOT carry rejection_class.
        """
        result = _run(_memo_draft(_base_params(dry_run=False), repo_root=None))
        assert result["exit_code"] == 1
        assert "rejection_class" not in result
        assert set(result.keys()) == _BASE_ENVELOPE_KEYS


# ===========================================================================
# 4. compose_draft_frontmatter — direct unit coverage
# ===========================================================================

class TestComposeDraftFrontmatter:
    def test_status_is_always_draft(self):
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind=None,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        assert read_fm_field(split.fm_text, "status") == "draft"

    def test_summary_key_present_but_placeholder_when_none(self):
        """2026-08-07 AC3: no usable summary writes SUMMARY_PLACEHOLDER, not ''."""
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind=None,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        assert read_fm_field_unquoted(split.fm_text, "summary") == SUMMARY_PLACEHOLDER

    def test_kind_omitted_when_none(self):
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind=None,
        )
        assert "kind:" not in fm

    def test_scoped_to_omitted_when_none(self):
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind=None, scoped_to=None,
        )
        assert "scoped_to" not in fm

    def test_scoped_to_renders_as_nested_mapping(self):
        scoped_to = {"artifact": "coordinator_core", "version": "1.2.3", "seam": "memo_draft"}
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind="ask", scoped_to=scoped_to,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        parsed = yaml.safe_load(split.fm_text)
        assert parsed["scoped_to"] == scoped_to
        assert "scoped_to:\n" in fm
        assert not re.search(r'scoped_to:\s*"', fm)

    def test_summary_absent_is_a_no_op(self):
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind=None,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        assert read_fm_field_unquoted(split.fm_text, "summary") == SUMMARY_PLACEHOLDER

    def test_summary_at_cap_passes_through_unchanged(self):
        at_cap = "x" * _SUMMARY_MAX_CHARS
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=at_cap, kind=None,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        assert read_fm_field(split.fm_text, "summary") == f'"{at_cap}"'

    def test_summary_under_cap_passes_through_unchanged(self):
        under_cap = "short summary"
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=under_cap, kind=None,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        assert read_fm_field(split.fm_text, "summary") == f'"{under_cap}"'

    def test_summary_over_cap_is_truncated_with_ellipsis(self):
        over_cap = "y" * (_SUMMARY_MAX_CHARS + 50)
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=over_cap, kind=None,
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        expected = over_cap[: _SUMMARY_MAX_CHARS - 1] + "…"
        assert len(expected) == _SUMMARY_MAX_CHARS
        assert read_fm_field(split.fm_text, "summary") == f'"{expected}"'


# ===========================================================================
# 5. Store-less-ness architecture test (mirrors AC8/strang-03 C6)
# ===========================================================================

class TestInReplyToDraft:
    """in_reply_to (2026-07-25 write-side addition) — normalized at draft
    time, NOT existence-checked (that gate is send-time only, see
    memo_send._validate_in_reply_to_exists / TestInReplyToExistenceGate in
    test_memo_send.py)."""

    def test_omitted_emits_no_key_in_draft(self):
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind=None,
        )
        assert "in_reply_to" not in fm

    def test_present_renders_as_scalar(self):
        fm = compose_draft_frontmatter(
            from_id="makima-engine", to="project-rag-em", title="T",
            today="2026-07-21", summary=None, kind="ask",
            in_reply_to="2026-07-25-foo.md",
        )
        split = split_frontmatter(fm + "\n\nbody\n")
        assert read_fm_field(split.fm_text, "in_reply_to") == '"2026-07-25-foo.md"'

    def test_act_normalizes_path_to_basename(self, tmp_path):
        """_memo_draft normalizes a caller-supplied path (not just a bare
        basename) via memo_send._normalize_in_reply_to before it ever reaches
        compose_draft_frontmatter."""
        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        params = _base_params(dry_run=False, kind="ask")
        params["in_reply_to"] = "cross-repo/inbox/2026-07-25-foo.md"

        result = _run(_memo_draft(params, repo_root=common_dir))
        assert result["exit_code"] == 0

        target = sender / "state" / "memo-outbox" / "some-topic.md"
        content = target.read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert read_fm_field(split.fm_text, "in_reply_to") == '"2026-07-25-foo.md"'

    def test_draft_then_send_lifecycle_preserves_field(self, tmp_path, monkeypatch):
        """draft -> send: the field staged into the outbox draft survives
        through to the delivered memo (mirrors the CLI's own draft->send
        flow, engine-side)."""
        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"

        # A receiver repo for the eventual send.
        receiver = tmp_path / "receiver-repo"
        receiver.mkdir()
        _git(receiver, "init", "-b", "main")
        _git(receiver, "config", "user.email", "test@makima.test")
        _git(receiver, "config", "user.name", "MakimaTest")
        _git(receiver, "config", "commit.gpgsign", "false")
        (receiver / "cross-repo" / "inbox").mkdir(parents=True)
        (receiver / "cross-repo" / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
        _git(receiver, "add", "-A")
        _git(receiver, "commit", "-m", "init receiver")

        claude_home = _make_claude_home(tmp_path, {"project_rag": receiver})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Sender's own inbox holds the memo we're replying to (send-time
        # existence gate reads THIS repo's own cross-repo/inbox/).
        inbound_dir = sender / "cross-repo" / "inbox"
        inbound_dir.mkdir(parents=True)
        (inbound_dir / "2026-07-25-inbound.md").write_text("x", encoding="utf-8")

        draft_params = _base_params(dry_run=False, kind="consult", to="project-rag-em")
        draft_params["in_reply_to"] = "2026-07-25-inbound.md"
        draft_result = _run(_memo_draft(draft_params, repo_root=common_dir))
        assert draft_result["exit_code"] == 0

        draft_path = sender / "state" / "memo-outbox" / "some-topic.md"
        draft_content = draft_path.read_text(encoding="utf-8")
        draft_split = split_frontmatter(draft_content)
        assert read_fm_field(draft_split.fm_text, "in_reply_to") == '"2026-07-25-inbound.md"'

        send_result = _run(memo_send_mod._memo_send(
            {
                "dry_run": False,
                "topic": "some-topic",
                "to": "project-rag-em",
                "title": "A draft memo",
                "body": "Reply body.",
                "kind": "consult",
                "summary": "Reply summary.",
                "in_reply_to": "2026-07-25-inbound.md",
            },
            repo_root=common_dir,
        ))
        assert send_result["exit_code"] == 0, send_result
        delivered_content = Path(send_result["acted"][0]["id"]).read_text(encoding="utf-8")
        assert 'in_reply_to: "2026-07-25-inbound.md"' in delivered_content


# ===========================================================================
# 5b. space / supersedes — shared validation with memo.send (2026-07-28)
#
# Review: code-reviewer (Finding 2, slice 1) — memo.draft duplicated
# memo_send's space/supersedes validation with no direct unit test of its
# own (only exercised incidentally, via happy-path overrides, in
# test_memo_compose.py::TestCarriedDraftFields). These tests exercise the
# draft path directly via _validate_draft_params, mirroring
# test_memo_send.py's TestSpaceParam / TestSupersedesListForm.
#
# Unified rule (EM correction, 2026-07-28): the two pre-extraction callers
# disagreed on the bare-string branch — memo.draft used to strip-and-reject
# a blank string; memo.send treated it as absence. That divergence was an
# authoring-time oversight, not a deliberate choice, so memo_send._validate_
# supersedes_param now unifies both callers on: a bare blank/whitespace-only
# string normalizes to None (absence), while a blank/non-string ENTRY INSIDE
# A LIST still fails loud (that's a malformed list, not absence).
# ===========================================================================

class TestSpaceParam:
    def test_omitted_space_is_none(self):
        result = _validate_draft_params(_base_params(dry_run=False))
        assert result == (
            False, "some-topic", "project-rag-em", "A draft memo", None, None, None, False,
            None, None, None, None,
        )

    def test_space_is_stripped(self):
        result = _validate_draft_params(_base_params(dry_run=False, space="  gate-migration  "))
        assert result[9] == "gate-migration"

    def test_blank_space_rejected(self):
        result = _validate_draft_params(_base_params(dry_run=False, space="   "))
        assert isinstance(result, dict)
        assert result["exit_code"] == 1


class TestSupersedesListForm:
    def test_single_element_list_collapses_to_string(self):
        result = _validate_draft_params(_base_params(
            dry_run=False, supersedes=["2026-07-20-a-em-old.md"],
        ))
        assert result[10] == "2026-07-20-a-em-old.md"

    def test_multi_element_list_preserved(self):
        result = _validate_draft_params(_base_params(
            dry_run=False, supersedes=["2026-07-20-a.md", "  2026-07-21-b.md  "],
        ))
        assert result[10] == ["2026-07-20-a.md", "2026-07-21-b.md"]

    def test_blank_list_entry_fails_loud_rather_than_pruning(self):
        result = _validate_draft_params(_base_params(
            dry_run=False, supersedes=["2026-07-20-a.md", ""],
        ))
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_non_string_supersedes_rejected(self):
        result = _validate_draft_params(_base_params(dry_run=False, supersedes=17))
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_blank_bare_string_normalizes_to_absent(self):
        """Unified rule: a bare blank/whitespace-only string is ABSENCE, not
        an error."""
        result = _validate_draft_params(_base_params(dry_run=False, supersedes="   "))
        assert result[10] is None

    def test_bare_string_is_stripped(self):
        result = _validate_draft_params(_base_params(
            dry_run=False, supersedes="  2026-07-20-a.md  ",
        ))
        assert result[10] == "2026-07-20-a.md"


class TestNoMemoIndex:
    def test_no_module_level_mutable_store(self):
        import coordinator_core.ops.fleet.memo_draft as memo_draft_mod

        module_globals = {
            name: val for name, val in vars(memo_draft_mod).items()
            if not name.startswith("__")
        }
        # `MUTATES` is the module's write-surface DECLARATION (c240385d0) — a
        # constant manifest of glob patterns, not a store. See the same carve-out
        # in test_memo_compose.py::TestNoMemoIndex.
        declarative_manifests = {"MUTATES"}
        mutable_collections = {
            name: val for name, val in module_globals.items()
            if isinstance(val, (dict, list, set))
            and not isinstance(val, types.ModuleType)
            and name not in declarative_manifests
        }
        assert mutable_collections == {}, (
            f"memo_draft module MUST NOT contain module-level mutable collections "
            f"(dict/list/set) — DR-210 Open-Q §2 store-less-ness invariant. "
            f"Found: {sorted(mutable_collections.keys())}"
        )

    def test_handler_calls_do_not_mutate_module_state(self, tmp_path):
        import coordinator_core.ops.fleet.memo_draft as memo_draft_mod

        def _mutable_names():
            return frozenset(
                name for name, val in vars(memo_draft_mod).items()
                if isinstance(val, (dict, list, set)) and not name.startswith("__")
            )

        names_before = _mutable_names()

        sender = _make_sender_git_repo(tmp_path)
        common_dir = sender / ".git"
        _run(_memo_draft(_base_params(dry_run=True, topic="state-check-1"), repo_root=common_dir))
        _run(_memo_draft(_base_params(dry_run=False, topic="state-check-2"), repo_root=common_dir))
        _run(_memo_draft(_base_params(dry_run=True, kind="bogus"), repo_root=common_dir))

        names_after = _mutable_names()
        assert names_after == names_before, (
            f"memo_draft module must not accumulate new module-level mutable state "
            f"across handler calls. New names: {sorted(names_after - names_before)}"
        )
