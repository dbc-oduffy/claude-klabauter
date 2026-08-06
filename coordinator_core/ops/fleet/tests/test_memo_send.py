"""
Tests for coordinator_core.ops.fleet.memo_send — memo.send MUTATING UDS op.

C2 test surface only (per strang-03 plan § C2):
  - dry_run vs act envelope shape (exit_code, mode, dry_run, candidates/acted/failed keys)
  - read-before-write ordering (AC7: collision state read before any write)
  - setup-error envelope on bad params (missing required fields, bad types)
  - main_worktree_root derivation (path resolves against real worktree, not .git/ dir)
  - path-traversal rejection (absolute-override + ../ both rejected before any write)
  - registry-enumerated allowed-set (unregistered receiver → rejected before any write)

C4 collision test added (same-day/same-topic → refuse, no clobber).
NOT in this file: no-index architecture test (C6).

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency.
Pattern: real temp git repos for receiver (git check-ignore needs an actual git repo);
         CLAUDE_HOME monkeypatched for registry isolation.

Spec backlink: docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md § C2
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.fleet.memo_send as memo_send_module
import coordinator_core.pickup_assemble as pickup_assemble
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    canonical_receiver_id as _canonical_receiver_id,
)
from coordinator_core.ops.fleet.memo_send import (
    _ENGINE_ACTOR_ID,
    _KNOWN_PARAM_KEYS,
    _MODE,
    _SENT_LEDGER_FILENAME,
    _VALID_KINDS,
    SendParams,
    _append_sent_ledger,
    _commit_delivered_memo,
    _compose_memo,
    _containment_check,
    _git_check_ignore,
    _memo_filename,
    _memo_send,
    _normalize_in_reply_to,
    _portable_delivered_to_form,
    _read_central_receiver_ids,
    _read_receiver_aliases,
    _read_registry_repos,
    _receiver_em_to_repo_key,
    _redelivery_filename,
    _resolve_receiver_inbox,
    _sender_sent_ledger_path,
    _sender_slug,
    _validate_in_reply_to_exists,
    _validate_send_params,
    _write_memo_file,
    resolve_sender_id,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------

def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio dependency."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Git repo factories
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=check,
    )


def _make_sender_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo to serve as the sender (for common_dir → worktree tests)."""
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    # Initial commit so the repo has a HEAD
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_receiver_git_repo(tmp_path: Path, name: str = "receiver-repo") -> Path:
    """Create a minimal git repo to serve as the receiver (with cross-repo/inbox/).

    The repo is git-init'd and has an initial commit so that:
    - git check-ignore works (needs a git repo)
    - cross-repo/inbox/ exists for delivery writes
    """
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init receiver")
    return root


# ---------------------------------------------------------------------------
# Registry fixture factory
# ---------------------------------------------------------------------------

def _make_claude_home(tmp_path: Path, receiver_repos: dict[str, Path]) -> Path:
    """Create a fake CLAUDE_HOME with a machine-local registry pointing to receiver_repos.

    receiver_repos: {registry_key_suffix: repo_path} e.g. {"example_retrieval_repo": Path("/...")}
    → writes "repos.example_retrieval_repo" = <path> in registry.local.toml.
    Uses double-quoted TOML string values for cross-platform path safety.

    The registry files live under `<claude_home>/.coordinator-claude-settings/machine-local/`,
    NOT `<claude_home>/machine-local/` directly — `_registry_home()` (site 9 repoint,
    docs/plans/2026-07-11-coordinator-core-home-claude-read-repoint.md § C2) now resolves
    via `_settings_home.machine_local_dir()`, whose CLAUDE_HOME fallback rung is
    `<CLAUDE_HOME>/.coordinator-claude-settings/machine-local` (no COORDINATOR_SETTINGS_HOME
    override set in these tests). `.doe-root` (read directly off CLAUDE_HOME by
    `_read_receiver_aliases`, out of scope for this repoint) stays at `<claude_home>/.doe-root`.
    """
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)

    # Minimal baseline registry.toml (schema only — no repos.* here).
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")

    # Local registry with repos.* pointers.
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        # Double-quote and escape the path for cross-platform TOML correctness.
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    return claude_home


def _make_doe_manifest(
    tmp_path: Path,
    claude_home: Path,
    aliases: list[dict],
    central_ids: list[str] | None = None,
    redirect_aliases: list[str] | None = None,
) -> Path:
    """Write .doe-root sentinel and a fake coordinator-registry.manifest.json.

    Creates a doe-root dir, writes the manifest with the given aliases, and
    points the DR-071 doe-root ladder at it. Mirrors the shape ratified in example-doctrine-repo
    consult 2026-07-05 strang-03 follow-up, Q2.

    The doe-root is this fixture's own `repos.example_doctrine_repo` when registered —
    exactly as on a real machine, where the ladder's canonical registry rung
    outranks the pointer file and the two agree — else a standalone dir. The
    pointer itself lands on the durable `<settings-home>/machine-local/.doe-root`
    rung, not the pre-2026-07-28 `<CLAUDE_HOME>/.doe-root`, which no writer has
    written since `ops.gen_doe_root_pointer` moved it under the settings home.

    `redirect_aliases` populates `identity.redirectAliases` (e.g. `.claude-em`,
    `claude-home`, `coordinator-claude`, `coordinator-claude-em`) — omitted by
    default (`[]`) so existing callers are unaffected.

    Returns the doe_root Path for callers that need it.
    """
    if central_ids is None:
        central_ids = ["claude-central-em", "central-em", "central", "example-doctrine-repo-em"]
    if redirect_aliases is None:
        redirect_aliases = []
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    doe_root = _registered_example_doctrine_repo(machine_local) or (tmp_path / "fake-doe-root")
    schema_dir = doe_root / "coordinator" / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "identity": {
            "repoAliases": aliases,
            "centralReceiverIds": central_ids,
            "redirectAliases": redirect_aliases,
        }
    }
    (schema_dir / "coordinator-registry.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    machine_local.mkdir(parents=True, exist_ok=True)
    (machine_local / ".doe-root").write_text(str(doe_root), encoding="utf-8")
    return doe_root


def _registered_example_doctrine_repo(machine_local: Path) -> Path | None:
    """Return this fixture's `repos.example_doctrine_repo` path, or None if unregistered.

    Handles both spellings `_make_claude_home` and hand-written fixtures use:
    the flat quoted-dotted key (`"repos.example_doctrine_repo" = "..."`) and the nested
    `[repos]` table.
    """
    import tomllib

    for fname in ("registry.local.toml", "registry.toml"):
        path = machine_local / fname
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        value = data.get("repos.example_doctrine_repo") or (data.get("repos") or {}).get("example_doctrine_repo")
        if value:
            return Path(str(value))
    return None


# ---------------------------------------------------------------------------
# Params helpers
# ---------------------------------------------------------------------------

def _base_params(
    *,
    to: str = "example-retrieval-repo-em",
    dry_run: bool = True,
    topic: str = "test-topic",
    kind: str = "fyi",
    summary: str = "Test summary.",
) -> dict:
    """Return a minimal valid params dict for memo.send.

    kind defaults to "fyi" — kind is a required field per DR-214 D4 / D2-6 affirmation
    (Review: code-reviewer F1 — kind: required, not optional). summary defaults to a
    non-empty string — summary is ALSO a required field as of DEC-1 (2026-07-24
    memo-ownership-and-redesign plan): omit-and-derive via memo.send is retired.
    """
    return {
        "dry_run": dry_run,
        "topic": topic,
        "to": to,
        "title": "Test Memo",
        "body": "This is a test memo body.",
        "kind": kind,
        "summary": summary,
    }


# ===========================================================================
# 1. setup-error envelope on bad params
# ===========================================================================

class TestSetupErrorEnvelope:
    """Verify that malformed params return exit_code:1 setup-error envelopes."""

    def test_dry_run_not_bool(self):
        result = _validate_send_params(
            {"dry_run": "true", "topic": "t", "to": "x-em", "title": "T", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_topic_missing(self):
        result = _validate_send_params(
            {"dry_run": True, "to": "x-em", "title": "T", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_topic_empty_string(self):
        result = _validate_send_params(
            {"dry_run": True, "topic": "", "to": "x-em", "title": "T", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    @pytest.mark.parametrize("bad_topic", [
        "../etc",
        "/absolute",
        "Has_Underscore",
        "has spaces",
        "a.b.c",
        "UPPERCASE",
        "-starts-with-dash",
    ])
    def test_topic_invalid_slug(self, bad_topic):
        """Topics with path chars, uppercase, or invalid start are rejected."""
        result = _validate_send_params(
            {"dry_run": True, "topic": bad_topic, "to": "x-em", "title": "T", "body": ""}
        )
        assert isinstance(result, dict), f"expected error for topic={bad_topic!r}"
        assert result["exit_code"] == 1

    def test_to_missing(self):
        result = _validate_send_params(
            {"dry_run": True, "topic": "t", "title": "T", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_title_missing(self):
        result = _validate_send_params(
            {"dry_run": True, "topic": "t", "to": "x-em", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_body_not_string_rejected(self):
        """body=None is rejected — body must be a string."""
        result = _validate_send_params(
            {"dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": None}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_body_empty_string_is_valid(self):
        """body='' is a valid param — empty memo body is permitted."""
        result = _validate_send_params(
            {
                "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
                "kind": "fyi", "summary": "Empty-body memo.",
            }
        )
        assert isinstance(result, SendParams), "empty body should pass validation"
        assert result.body == ""

    def test_kind_missing_rejected(self):
        """kind is required — absence returns exit_code:1 setup-error (DR-214 D4 / D2-6).

        Review: code-reviewer F1 — kind was previously accepted as Optional; now
        enforced as required so handler never emits a schema-invalid memo missing kind:.
        """
        result = _validate_send_params(
            {"dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_kind_empty_string_rejected(self):
        """kind='' is rejected — empty string is not a valid kind value."""
        result = _validate_send_params(
            {"dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "", "kind": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_setup_error_envelope_has_standard_shape(self):
        """exit_code:1 envelope has mode, dry_run, and empty arrays (frozen wire shape)."""
        result = _validate_send_params(
            {"dry_run": True, "topic": "INVALID!", "to": "x-em", "title": "T", "body": ""}
        )
        assert isinstance(result, dict)
        assert result["exit_code"] == 1
        assert result["mode"] == _MODE
        assert result["dry_run"] is True
        assert result["candidates"] == []
        assert result["acted"] == []
        assert result["skipped"] == []
        assert result["failed"] == []

    def test_handler_returns_setup_error_on_bad_topic(self, tmp_path, monkeypatch):
        """End-to-end: handler returns exit_code:1 when topic slug is invalid."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(
            {"dry_run": True, "topic": "INVALID!!", "to": "x-em", "title": "T", "body": ""},        ))
        assert result["exit_code"] == 1
        assert result["mode"] == _MODE


# ===========================================================================
# 1b. C9 (A11) — total emission, nested-mapping support, fail-loud unknown params
# ===========================================================================

class TestTotalEmissionAndScopedTo:
    """C9: closes the silent frontmatter-drop hole (A11).

    Source: cross-repo/inbox/2026-07-21-claude-central-em-memo-send-drops-
    unknown-frontmatter-keys.md. A send carrying an extra key must either
    arrive in the delivered memo or fail the send loudly — never vanish with
    exit_code:0. `scoped_to` is the declared nested-mapping extra param; it must
    round-trip as a real YAML mapping, not flatten into a quoted scalar.
    """

    def test_unknown_param_rejected_setup_error(self):
        """A completely unrecognized param key is rejected, not silently dropped."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "fyi", "not_a_real_param": "should-not-be-accepted",
        })
        assert isinstance(result, dict), "unknown param must be rejected"
        assert result["exit_code"] == 1

    def test_unknown_param_handler_exits_nonzero_and_writes_no_file(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: memo.send with an unrecognized param exits non-zero and
        writes NO file — this is the exact defect the central-em memo reported
        (scoped_to previously vanished with exit_code:0)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = {**_base_params(dry_run=False, topic="unknown-param-test"),
                  "totally_unrecognized_key": "value"}
        result = _run(_memo_send(params))

        assert result["exit_code"] != 0
        inbox = receiver_repo / "cross-repo" / "inbox"
        written = [f for f in inbox.iterdir() if f.name != ".gitkeep"]
        assert written == [], "unknown param must not result in a written memo"

    def test_scoped_to_round_trips_as_nested_mapping(self, tmp_path, monkeypatch):
        """scoped_to round-trips as a real YAML mapping, parses back equal via yaml.safe_load."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        scoped_to = {"artifact": "coordinator_core", "version": "1.2.3", "seam": "memo_send"}
        params = {**_base_params(dry_run=False, topic="scoped-to-test"), "scoped_to": scoped_to}
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0, f"scoped_to send should succeed: {result}"
        content = Path(result["acted"][0]["id"]).read_text(encoding="utf-8")
        split = split_frontmatter(content)
        assert split is not None
        frontmatter = yaml.safe_load(split.fm_text)
        assert frontmatter["scoped_to"] == scoped_to, (
            f"scoped_to must round-trip as an equal nested mapping; got: "
            f"{frontmatter.get('scoped_to')!r}"
        )
        # Structural guard against flattening: scoped_to must be a bare block-mapping
        # key (its own line, no trailing quoted-scalar value), never a single
        # double-quoted scalar covering the whole dict.
        assert "scoped_to:\n" in content, (
            "scoped_to must render as a bare mapping key, not a quoted scalar line"
        )
        assert not re.search(r'scoped_to:\s*"', content), (
            "scoped_to must not be flattened into a double-quoted scalar"
        )

    def test_scoped_to_missing_artifact_rejected(self):
        """scoped_to present but missing required 'artifact' sub-key fails loud."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "fyi", "scoped_to": {"version": "1.0.0", "seam": "memo_send"},
        })
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_scoped_to_unknown_subkey_rejected(self):
        """scoped_to with an undeclared sub-key fails loud (same rule as top-level unknown)."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "fyi", "scoped_to": {"artifact": "x", "bogus_subkey": "y"},
        })
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_scoped_to_not_a_mapping_rejected(self):
        """scoped_to given as a non-mapping (e.g. string) fails loud rather than being coerced."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "fyi", "scoped_to": "not-a-dict",
        })
        assert isinstance(result, dict)
        assert result["exit_code"] == 1


# ===========================================================================
# 1c. scoped_to presence-triggered completeness (2026-07-21 fix)
#
# Source: cross-repo/inbox/2026-07-21-claude-central-em-debash-directive-cites-
# guard-plus-scoped-to-q.md — routed schema question from example-retrieval-repo-em's
# proposal. scoped_to must NOT be required by `kind` (a directional /
# doctrine-establishing ask governs no versioned artifact); it must instead
# be optional overall, with the FULL triple (artifact + exactly one of
# version|sha + seam) required the moment ANY scoped_to sub-key is supplied.
# ===========================================================================

class TestScopedToPresenceTriggeredCompleteness:
    """(a) directional ask/no scoped_to passes; (b) complete triple passes;
    (c) partial triple fails loud — regardless of `kind`."""

    def test_directional_ask_no_scoped_to_passes(self):
        """kind: ask with NO scoped_to at all is a valid directional ask — passes."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "ask", "summary": "Directional ask.",
        })
        assert isinstance(result, SendParams), (
            f"a directional ask with no scoped_to must pass validation, got: {result}"
        )
        assert result.scoped_to is None

    def test_directional_proposal_no_scoped_to_passes(self):
        """kind: proposal with NO scoped_to at all also passes — not kind-gated."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "proposal", "summary": "Directional proposal.",
        })
        assert isinstance(result, SendParams), (
            f"a directional proposal with no scoped_to must pass validation, got: {result}"
        )

    def test_change_control_ask_complete_triple_with_version_passes(self):
        """kind: ask with the COMPLETE triple (artifact + version + seam) passes."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "ask", "summary": "Change-control ask.",
            "scoped_to": {"artifact": "coordinator_core", "version": "1.2.3", "seam": "memo_send"},
        })
        assert isinstance(result, SendParams), (
            f"a change-control ask with the complete triple must pass, got: {result}"
        )

    def test_change_control_ask_complete_triple_with_sha_passes(self):
        """kind: ask with the COMPLETE triple (artifact + sha + seam) also passes."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "ask", "summary": "Change-control ask.",
            "scoped_to": {"artifact": "coordinator_core", "sha": "deadbeef", "seam": "memo_send"},
        })
        assert isinstance(result, SendParams), (
            f"a change-control ask with the complete triple (sha variant) must pass, got: {result}"
        )

    def test_partial_scoped_to_missing_seam_fails_loud(self):
        """artifact + version but no seam is a PARTIAL triple — fails loud, not accepted."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "ask",
            "scoped_to": {"artifact": "coordinator_core", "version": "1.2.3"},
        })
        assert isinstance(result, dict), "a partial scoped_to triple must fail loud"
        assert result["exit_code"] == 1

    def test_partial_scoped_to_missing_version_and_sha_fails_loud(self):
        """artifact + seam but neither version nor sha is a PARTIAL triple — fails loud."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "ask",
            "scoped_to": {"artifact": "coordinator_core", "seam": "memo_send"},
        })
        assert isinstance(result, dict), "a partial scoped_to triple must fail loud"
        assert result["exit_code"] == 1

    def test_scoped_to_both_version_and_sha_fails_loud(self):
        """version AND sha both supplied violates 'exactly one of' — fails loud."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "ask",
            "scoped_to": {
                "artifact": "coordinator_core", "version": "1.2.3", "sha": "deadbeef",
                "seam": "memo_send",
            },
        })
        assert isinstance(result, dict), (
            "supplying both version and sha must fail loud (exactly one required)"
        )
        assert result["exit_code"] == 1

    def test_scoped_to_completeness_not_gated_on_kind(self):
        """A partial scoped_to on kind: fyi fails loud too — the gate is presence-
        triggered, not kind-gated (no special-case for non-ask/proposal kinds)."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T", "body": "",
            "kind": "fyi",
            "scoped_to": {"artifact": "coordinator_core"},
        })
        assert isinstance(result, dict), (
            "a partial scoped_to must fail loud regardless of kind"
        )
        assert result["exit_code"] == 1

    def test_canonical_nine_fields_unchanged_when_no_extras(self, tmp_path, monkeypatch):
        """Regression guard (DR-026 / schema lockstep + strang-03 round-trip fixture):
        with no extras passed, the composed frontmatter keeps byte-identical field
        order and shape for the nine canonical fields."""
        content = _compose_memo(
            from_id="sender-repo-alpha",
            to="example-retrieval-repo-em",
            topic="no-extras-regression",
            title="No Extras",
            body="Body text.",
            kind="fyi",
            summary=None,
            supersedes=None,
            today="2026-07-21",
        )
        expected_field_order = [
            "title", "from", "to", "created", "status",
            "delivery_mode", "summary", "kind",
        ]
        split = split_frontmatter(content)
        assert split is not None
        fm_lines = [
            line for line in split.fm_text.splitlines() if line.strip()
        ]
        actual_field_order = [line.split(":", 1)[0].strip() for line in fm_lines]
        assert actual_field_order == expected_field_order, (
            f"canonical field order must be unchanged when no extras are passed; "
            f"got: {actual_field_order}"
        )

    def test_known_param_keys_includes_scoped_to(self):
        """_KNOWN_PARAM_KEYS declares scoped_to (structural guard on the allowlist itself)."""
        assert "scoped_to" in _KNOWN_PARAM_KEYS

    def test_summary_derivation_skips_leading_heading(self):
        """footgun #4 (send path): a body opening with an ATX heading must not
        derive `summary:` as the literal heading line (including the `#`) —
        _compose_memo must use the same prose-first derive_prose_summary rule
        memo.compose uses, taking the first PROSE sentence instead."""
        content = _compose_memo(
            from_id="sender-repo-alpha",
            to="example-retrieval-repo-em",
            topic="heading-first-body",
            title="Heading First Body",
            body="# Some Heading\n\nActual prose sentence here.",
            kind="fyi",
            summary=None,
            supersedes=None,
            today="2026-07-21",
        )
        split = split_frontmatter(content)
        assert split is not None
        summary_line = next(
            line for line in split.fm_text.splitlines()
            if line.strip().startswith("summary:")
        )
        assert summary_line == 'summary: "Actual prose sentence here."', (
            f"expected derived prose-sentence summary, got: {summary_line!r}"
        )
        assert "# Some Heading" not in summary_line
        assert not summary_line.strip().startswith('summary: "#')

    def test_explicit_summary_over_cap_fails_loud_not_truncated(self, capsys):
        """2026-07-22 body-drop verdict memo (cross-repo/inbox/2026-07-22-
        claude-central-em-snippet-sync-adoption-and-body-drop-verdict.md):
        an EXPLICITLY authored summary over _SUMMARY_MAX_CHARS must fail
        loud (exit_code:1 setup-error) at _validate_send_params — BEFORE
        _compose_memo ever runs — never silently truncated mid-sentence."""
        long_summary = "S" * (_SUMMARY_MAX_CHARS + 1)
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T",
            "body": "Body.", "kind": "fyi", "summary": long_summary,
        })
        assert isinstance(result, dict), "over-cap explicit summary must fail loud"
        assert result["exit_code"] == 1
        stderr = capsys.readouterr().err
        assert str(_SUMMARY_MAX_CHARS) in stderr
        assert long_summary not in stderr, (
            "error must not echo the full over-cap summary back"
        )

    def test_explicit_summary_at_cap_passes(self):
        """A summary exactly AT the cap is valid — the gate is strictly-greater-than."""
        at_cap_summary = "S" * _SUMMARY_MAX_CHARS
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T",
            "body": "Body.", "kind": "fyi", "summary": at_cap_summary,
        })
        assert isinstance(result, SendParams)
        assert result.summary == at_cap_summary

    def test_omitted_summary_rejected(self, tmp_path):
        """DEC-1 (2026-07-24 memo-ownership-and-redesign plan): summary is now
        UNCONDITIONALLY required at send time — omit-and-derive via memo.send
        is retired, so an omitted summary param fails loud at
        _validate_send_params rather than being derived later."""
        long_body = "B" * (_SUMMARY_MAX_CHARS + 50) + "."
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T",
            "body": long_body, "kind": "fyi",
        })
        assert isinstance(result, dict), "an omitted summary must fail loud (DEC-1)"
        assert result["exit_code"] == 1

    def test_empty_summary_rejected(self, tmp_path):
        """summary='' (present key, empty value) is also rejected — DEC-1 requires
        present AND non-empty, not merely present."""
        result = _validate_send_params({
            "dry_run": True, "topic": "t", "to": "x-em", "title": "T",
            "body": "Body.", "kind": "fyi", "summary": "",
        })
        assert isinstance(result, dict), "an empty-string summary must fail loud (DEC-1)"
        assert result["exit_code"] == 1

    def test_compose_memo_raises_on_over_cap_explicit_summary(self):
        """Defense-in-depth: a direct _compose_memo caller that bypasses
        _validate_send_params (e.g. this test) gets a clear ValueError naming
        the cap — not the former silent `[:119] + "…"` clamp."""
        long_summary = "S" * (_SUMMARY_MAX_CHARS + 1)
        with pytest.raises(ValueError, match=str(_SUMMARY_MAX_CHARS)):
            _compose_memo(
                from_id="sender-repo-alpha",
                to="example-retrieval-repo-em",
                topic="over-cap-summary",
                title="Over Cap Summary",
                body="Body.",
                kind="fyi",
                summary=long_summary,
                supersedes=None,
                today="2026-07-21",
            )


# ===========================================================================
# 2. dry_run vs act envelope shape
# ===========================================================================

class TestEnvelopeShape:
    """Verify the dry_run:true and dry_run:false response envelope structures."""

    def test_dry_run_envelope_has_correct_shape(self, tmp_path, monkeypatch):
        """dry_run:true → exit_code:0, candidates[] non-empty, acted/failed empty."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=True)))

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        assert result["dry_run"] is True
        assert isinstance(result["candidates"], list)
        assert len(result["candidates"]) == 1
        assert result["acted"] == []
        assert result["failed"] == []

    def test_dry_run_candidate_fields(self, tmp_path, monkeypatch):
        """dry_run candidate has id, topic, receiver, target_path, collision fields."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=True, topic="my-memo")))

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert "id" in candidate
        assert "target_path" in candidate
        assert candidate["topic"] == "my-memo"
        assert candidate["receiver"] == "example-retrieval-repo-em"
        assert "collision" in candidate
        assert candidate["collision"] is False  # no prior write

    def test_act_success_envelope_has_correct_shape(self, tmp_path, monkeypatch):
        """dry_run:false success → exit_code:0, acted[] non-empty, candidates/failed empty."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False)))

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        assert result["dry_run"] is False
        assert result["candidates"] == []
        assert isinstance(result["acted"], list)
        assert len(result["acted"]) == 1
        assert result["failed"] == []

    def test_act_acted_item_fields(self, tmp_path, monkeypatch):
        """acted item has id, written:True, receiver, topic fields."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="my-memo")))

        assert result["exit_code"] == 0
        acted = result["acted"][0]
        assert acted.get("written") is True
        assert acted.get("receiver") == "example-retrieval-repo-em"
        assert acted.get("topic") == "my-memo"
        assert "id" in acted

    def test_act_success_top_level_envelope_keys_unchanged(self, tmp_path, monkeypatch):
        """AC3 — a successful send's TOP-LEVEL envelope keys/values are
        byte-identical to today: exactly exit_code/mode/dry_run/candidates/
        acted/failed. `delivery_commit` (C1) lands on the acted ENTRY, never
        as a new top-level key — this pins the boundary."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="envelope-shape-test")))

        assert set(result.keys()) == {
            "exit_code", "mode", "dry_run", "candidates", "acted", "skipped", "failed",
        }
        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        assert result["dry_run"] is False
        assert result["candidates"] == []
        assert result["failed"] == []
        assert "delivery_commit" in result["acted"][0], (
            "delivery_commit belongs on the acted ENTRY, not the top level"
        )

    def test_act_writes_file_into_inbox(self, tmp_path, monkeypatch):
        """act path writes the memo file into receiver's cross-repo/inbox/.

        DR-026: filename is now sender-namespaced (<date>-<sender-slug>-<topic>.md,
        not the pre-DR-026 <date>-<topic>.md). Expected filename is computed via the
        real _memo_filename/_sender_slug helpers (not hardcoded) so this test stays
        faithful to the contract rather than a frozen literal.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="delivery-test")))

        assert result["exit_code"] == 0
        acted_id = result["acted"][0]["id"]
        target = Path(acted_id)
        assert target.exists(), f"memo file should exist: {target}"
        # Must be structurally inside <receiver>/cross-repo/inbox/
        # Review: code-reviewer F5 — structural assertion (not substring) so a path like
        # /tmp/cross-repo-backup/inbox-staging/… does not produce a false positive.
        assert target.parent.name == "inbox"
        assert target.parent.parent.name == "cross-repo"
        assert "delivery-test" in target.name
        today = datetime.date.today().isoformat()
        expected_filename = _memo_filename(today, _ENGINE_ACTOR_ID, "delivery-test")
        assert target.name == expected_filename, (
            f"DR-026 sender-namespaced filename mismatch: expected {expected_filename!r}, "
            f"got {target.name!r}"
        )
        assert _sender_slug(_ENGINE_ACTOR_ID) in target.name

    def test_act_written_memo_has_schema_valid_frontmatter(self, tmp_path, monkeypatch):
        """Written memo has required frontmatter: to/from/status:open/delivery_mode."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False)))

        assert result["exit_code"] == 0
        memo_path = Path(result["acted"][0]["id"])
        content = memo_path.read_text(encoding="utf-8")

        # D2 criterion 6: schema-valid emission (to/from/status:open/delivery_mode:receiver-repo/kind:)
        assert "status: open" in content
        assert "delivery_mode: receiver-repo" in content
        assert '"example-retrieval-repo-em"' in content  # to: quoted
        assert f'"{_ENGINE_ACTOR_ID}"' in content  # from: engine actor id
        # Review: code-reviewer F1 — kind: is required per DR-214 D4 and D2-6 affirmation;
        # previously untested, allowing schema-invalid memos without kind: to be emitted.
        assert "kind:" in content

    def test_act_written_memo_has_body(self, tmp_path, monkeypatch):
        """Written memo includes the body text after the frontmatter."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        body_text = "This is the memo body for testing."
        params = {**_base_params(dry_run=False), "body": body_text}
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0
        content = Path(result["acted"][0]["id"]).read_text(encoding="utf-8")
        assert body_text in content

    def test_act_with_kind_field_emitted(self, tmp_path, monkeypatch):
        """kind field is emitted in frontmatter (required field per DR-214 D4 / D2-6)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = {**_base_params(dry_run=False), "kind": "consult"}
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0
        content = Path(result["acted"][0]["id"]).read_text(encoding="utf-8")
        assert '"consult"' in content

    def test_act_with_custom_from_id(self, tmp_path, monkeypatch):
        """Custom from_id overrides the engine actor default in frontmatter."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = {**_base_params(dry_run=False), "from_id": "claude-klabauter-em"}
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0
        content = Path(result["acted"][0]["id"]).read_text(encoding="utf-8")
        assert '"claude-klabauter-em"' in content


# ===========================================================================
# 2b. delivered-memo commit (retires DR-211 D2 criterion 3, PM directive 2026-07-21)
# ===========================================================================

class TestDeliveredMemoCommit:
    """send in act mode COMMITS the delivered memo into the receiver repo.

    Spec backlink: PM directive 2026-07-21 retiring DR-211 D2 criterion 3
    ("send is non-committing") for memo.send only — see _commit_delivered_memo.
    """

    def test_act_commits_delivered_memo(self, tmp_path, monkeypatch):
        """act-mode send lands a commit touching the memo path; tree is clean after."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="commit-test")))
        assert result["exit_code"] == 0
        memo_path = Path(result["acted"][0]["id"])
        rel_path = os.path.relpath(memo_path, receiver_repo)

        log = _git(receiver_repo, "log", "--oneline", "--", rel_path)
        assert log.stdout.strip(), (
            "expected a commit touching the delivered memo path, found none "
            f"(git log stdout={log.stdout!r} stderr={log.stderr!r})"
        )
        subject = log.stdout.decode(errors="replace")
        assert "deliver" in subject
        assert "Test Memo" in subject
        assert _ENGINE_ACTOR_ID in subject

        status = _git(receiver_repo, "status", "--porcelain", "--", rel_path)
        assert status.stdout.decode(errors="replace").strip() == "", (
            f"working tree for delivered memo path should be clean after commit, "
            f"got: {status.stdout!r}"
        )

    def test_all_hooks_off_honored_despite_failing_prepare_commit_msg_hook(
        self, tmp_path, monkeypatch
    ):
        """Regression guard (the motivating bug): a broken/arbitrary receiver-side
        `prepare-commit-msg` hook must NOT be able to defeat durable delivery.

        This is the exact hook `--no-verify` does NOT bypass (git does not skip
        `prepare-commit-msg` for `--no-verify` — verified empirically) — the
        incident that motivated this whole change. The all-hooks-off
        `-c core.hooksPath=<empty-tmpdir>` mechanism neutralizes it along with
        every other hook class, so this is now a regression guard that was
        IMPOSSIBLE to write honestly under the old `--no-verify` mechanism
        (the prior test dodged the gap by exercising `pre-commit` instead).
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        hooks_dir = receiver_repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "prepare-commit-msg"
        hook_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_path.chmod(0o755)

        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="hook-guard-test")))

        assert result["exit_code"] == 0
        memo_path = Path(result["acted"][0]["id"])
        rel_path = os.path.relpath(memo_path, receiver_repo)
        log = _git(receiver_repo, "log", "--oneline", "--", rel_path)
        assert log.stdout.strip(), (
            "delivery commit must still land even with a failing "
            "prepare-commit-msg hook in the receiver repo — all-hooks-off "
            "via core.hooksPath must skip it (this is the whole point of "
            "the change; --no-verify alone never fixed this)"
        )

    def test_post_commit_hook_suppressed_no_auto_push_driven(self, tmp_path, monkeypatch):
        """The all-hooks-off mechanism suppresses post-commit too — send does
        NOT drive the receiver's own post-commit automation (e.g. auto-push).

        Empirically confirms the one behavior the Staff Engineer flagged he had not
        confirmed: that core.hooksPath=<empty-tmpdir> actually suppresses
        post-commit in a real repo, not just pre-commit-class hooks.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        hooks_dir = receiver_repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        sentinel_path = receiver_repo / "post-commit-ran.sentinel"
        hook_path = hooks_dir / "post-commit"
        hook_path.write_text(
            f"#!/bin/sh\ntouch {sentinel_path}\n", encoding="utf-8"
        )
        hook_path.chmod(0o755)

        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="post-commit-guard-test")))

        assert result["exit_code"] == 0
        memo_path = Path(result["acted"][0]["id"])
        rel_path = os.path.relpath(memo_path, receiver_repo)
        log = _git(receiver_repo, "log", "--oneline", "--", rel_path)
        assert log.stdout.strip(), "delivery commit must still land"
        assert not sentinel_path.exists(), (
            "post-commit hook must NOT run on a delivery commit — the "
            "all-hooks-off mechanism suppresses post-commit too, so send "
            "never drives the receiver's own auto-push (committed-but-unpushed "
            "is the deliberate delivered state)"
        )

    def test_delivery_commit_message_has_no_injected_trailer(self, tmp_path, monkeypatch):
        """The delivery commit message is exactly the subject — no injected
        Session-Id or other trailer from a receiver message hook.

        A prepare-commit-msg (or commit-msg) hook that appends a trailer
        meaningful only to the receiver's OWN commits must never run on a
        foreign delivery commit; the all-hooks-off mechanism guarantees this.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        hooks_dir = receiver_repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "prepare-commit-msg"
        hook_path.write_text(
            '#!/bin/sh\necho "Session-Id: fake-foreign-session" >> "$1"\n',
            encoding="utf-8",
        )
        hook_path.chmod(0o755)

        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="no-trailer-test")))

        assert result["exit_code"] == 0
        memo_path = Path(result["acted"][0]["id"])
        rel_path = os.path.relpath(memo_path, receiver_repo)
        log = _git(receiver_repo, "log", "-1", "--format=%B", "--", rel_path)
        message = log.stdout.decode(errors="replace")
        assert "Session-Id" not in message, (
            f"delivery commit message must not carry a receiver-hook-injected "
            f"trailer (prepare-commit-msg must not have run): {message!r}"
        )
        assert "deliver" in message
        assert "Test Memo" in message
        assert _ENGINE_ACTOR_ID in message

    def test_commit_delivered_memo_idempotent_nothing_to_commit(self, tmp_path):
        """Committing an already-committed identical memo path is a no-op success."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        memo_path = receiver_repo / "cross-repo" / "inbox" / "already-committed.md"
        memo_path.write_text("dummy content\n", encoding="utf-8")
        _git(receiver_repo, "add", "--", "cross-repo/inbox/already-committed.md")
        _git(receiver_repo, "commit", "-m", "pre-existing commit of the memo")

        outcome = _run(
            _commit_delivered_memo(
                receiver_repo, "cross-repo/inbox/already-committed.md",
                "some-sender", "Some Title",
            )
        )
        assert outcome.committed is True
        assert outcome.branch  # non-empty — resolved to the repo's actual branch
        assert outcome.reason is None

    def test_commit_delivered_memo_never_raises_on_non_repo(self, tmp_path):
        """Pointing at a directory that is not a git repo must not raise."""
        non_repo = tmp_path / "not-a-git-repo"
        non_repo.mkdir()
        (non_repo / "cross-repo" / "inbox").mkdir(parents=True)
        memo_path = non_repo / "cross-repo" / "inbox" / "orphan.md"
        memo_path.write_text("dummy\n", encoding="utf-8")

        outcome = _run(
            _commit_delivered_memo(
                non_repo, "cross-repo/inbox/orphan.md", "sender", "Title",
            )
        )
        assert outcome.committed is False
        assert outcome.branch is None
        assert outcome.reason  # non-empty — the reason must not be discarded (AC1)
        # File must still be present — a commit failure never touches the write.
        assert memo_path.exists()

    def test_no_active_branch_receiver_left_uncommitted_no_branch_created(
        self, tmp_path
    ):
        """A receiver on no active branch (detached HEAD) is left uncommitted
        and dirty — branch-creation was REMOVED (2026-07-21, the Staff Engineer
        REQUIRES_CHANGES): a headless engine must never switch/create a
        branch in a foreign repo. Asserts:
          - _commit_delivered_memo returns None (never-raise contract)
          - the memo file is NOT committed (still shows in git status)
          - the receiver's HEAD/branch state is completely unchanged
            (still detached at the same commit — no new branch created)
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        # Detach HEAD at the current commit.
        head_sha = _git(receiver_repo, "rev-parse", "HEAD").stdout.decode().strip()
        _git(receiver_repo, "checkout", "--detach", head_sha)

        branches_before = _git(
            receiver_repo, "for-each-ref", "--format=%(refname)", "refs/heads/"
        ).stdout.decode()

        memo_path = receiver_repo / "cross-repo" / "inbox" / "detached-head-test.md"
        memo_path.write_text("dummy content\n", encoding="utf-8")

        outcome = _run(
            _commit_delivered_memo(
                receiver_repo, "cross-repo/inbox/detached-head-test.md",
                "some-sender", "Some Title",
            )
        )

        assert outcome.committed is False, (
            "no-active-branch receiver must report committed=False (never-raise; "
            "delivered memo left uncommitted for the receiver's session-init sweep)"
        )
        assert outcome.branch is None
        assert outcome.reason  # non-empty — never discarded (AC1)

        # File is still present but NOT committed (dirty working tree).
        assert memo_path.exists()
        status = _git(
            receiver_repo, "status", "--porcelain", "--",
            "cross-repo/inbox/detached-head-test.md",
        )
        assert status.stdout.decode().strip(), (
            "the memo file must show as uncommitted (dirty) in git status"
        )

        # No new branch was created — the set of refs/heads/ is unchanged.
        branches_after = _git(
            receiver_repo, "for-each-ref", "--format=%(refname)", "refs/heads/"
        ).stdout.decode()
        assert branches_after == branches_before, (
            f"no branch should be created in a headless receiver: "
            f"before={branches_before!r} after={branches_after!r}"
        )

        # HEAD is still detached at the same commit — untouched.
        current_head = _git(receiver_repo, "rev-parse", "HEAD").stdout.decode().strip()
        assert current_head == head_sha
        symbolic_check = _git(
            receiver_repo, "symbolic-ref", "-q", "HEAD", check=False
        )
        assert symbolic_check.returncode != 0, "HEAD must still be detached"

    def test_act_reports_success_even_when_commit_step_fails(
        self, tmp_path, monkeypatch
    ):
        """A failed commit step must not turn a successful delivery into a
        failed send — the file is already durably written by the time the
        commit step runs, and the handler must still report exit_code 0 with
        the written file in `acted` (AC4 — never-raise contract)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        async def _fake_commit_failure(*args, **kwargs):
            # Mirrors what the real function returns on any git failure
            # (logged WARNING, never raised) — a failed CommitOutcome, not
            # an exception.
            return memo_send_mod.CommitOutcome(
                committed=False, branch=None, reason="fake git failure: simulated",
            )

        monkeypatch.setattr(
            memo_send_mod, "_commit_delivered_memo", _fake_commit_failure
        )

        result = _run(_memo_send(_base_params(dry_run=False, topic="commit-fail-test")))

        assert result["exit_code"] == 0
        assert result["acted"]
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists(), (
            "the memo file must still be written even though the commit "
            "step reported failure — a commit failure never unwinds an "
            "already-durable write"
        )
        # AC1, AC2 — the git failure reason reaches the acted entry's
        # delivery_commit sub-object rather than dying in _LOG.warning.
        delivery_commit = result["acted"][0]["delivery_commit"]
        assert delivery_commit["committed"] is False
        assert delivery_commit["branch"] is None
        assert delivery_commit["reason"] == "fake git failure: simulated"
        assert delivery_commit["retried"] is False

    def test_index_lock_failure_succeeds_on_retry(self, tmp_path, monkeypatch):
        """AC5 — an index.lock failure on the first attempt that succeeds on
        the retry reports committed: true, retried: true, and the retry
        fires exactly once (call count, not just the outcome)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        call_count = 0

        async def _fake_commit(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return memo_send_mod.CommitOutcome(
                    committed=False, branch=None,
                    reason="Unable to create '.git/index.lock': File exists.",
                )
            return memo_send_mod.CommitOutcome(
                committed=True, branch="work/some-branch", reason=None,
            )

        monkeypatch.setattr(memo_send_mod, "_commit_delivered_memo", _fake_commit)

        result = _run(_memo_send(_base_params(dry_run=False, topic="index-lock-retry-test")))

        assert result["exit_code"] == 0
        delivery_commit = result["acted"][0]["delivery_commit"]
        assert delivery_commit["committed"] is True
        assert delivery_commit["retried"] is True
        assert call_count == 2, (
            f"retry must fire EXACTLY once (2 total calls), got {call_count}"
        )

    def test_index_lock_failure_persists_after_one_retry(self, tmp_path, monkeypatch):
        """AC5 — a second consecutive index.lock failure surfaces normally
        (retried: true, committed: false) rather than retrying again."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        call_count = 0

        async def _fake_commit(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return memo_send_mod.CommitOutcome(
                committed=False, branch=None,
                reason="Unable to create '.git/index.lock': File exists.",
            )

        monkeypatch.setattr(memo_send_mod, "_commit_delivered_memo", _fake_commit)

        result = _run(_memo_send(_base_params(dry_run=False, topic="index-lock-persist-test")))

        assert result["exit_code"] == 0  # AC4 — still never fails the send
        delivery_commit = result["acted"][0]["delivery_commit"]
        assert delivery_commit["committed"] is False
        assert delivery_commit["retried"] is True
        assert call_count == 2, (
            f"exactly one retry — no loop, no second retry, got {call_count} calls"
        )

    def test_non_index_lock_failure_is_not_retried(self, tmp_path, monkeypatch):
        """AC6 — a non-index.lock failure is NOT retried; the commit
        function is called exactly once."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        call_count = 0

        async def _fake_commit(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return memo_send_mod.CommitOutcome(
                committed=False, branch=None,
                reason="git commit failed: unrelated permission error",
            )

        monkeypatch.setattr(memo_send_mod, "_commit_delivered_memo", _fake_commit)

        result = _run(_memo_send(_base_params(dry_run=False, topic="non-lock-failure-test")))

        assert result["exit_code"] == 0
        delivery_commit = result["acted"][0]["delivery_commit"]
        assert delivery_commit["committed"] is False
        assert delivery_commit["retried"] is False
        assert call_count == 1, (
            f"a non-index.lock failure must not be retried, got {call_count} calls"
        )

    def test_dry_run_does_not_commit(self, tmp_path, monkeypatch):
        """dry_run writes nothing and commits nothing (act-only commit step)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        before_log = _git(receiver_repo, "log", "--oneline")
        result = _run(_memo_send(_base_params(dry_run=True, topic="dry-run-commit-test")))
        after_log = _git(receiver_repo, "log", "--oneline")

        assert result["exit_code"] == 0
        assert before_log.stdout == after_log.stdout, (
            "dry_run must not add any commit to the receiver repo"
        )
        status = _git(receiver_repo, "status", "--porcelain")
        assert status.stdout.decode(errors="replace").strip() == "", (
            "dry_run must leave the receiver repo's working tree untouched"
        )


# ===========================================================================
# 3. read-before-write ordering (AC7)
# ===========================================================================

class TestReadBeforeWrite:
    """Verify AC7: collision state is read before any write."""

    def test_dry_run_reads_collision_false_without_writing(self, tmp_path, monkeypatch):
        """dry_run reads collision state (shows False) but does NOT write any file."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=True, topic="no-write-test")))

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        # No prior write → collision is False (read state is correct)
        assert candidate["collision"] is False

        # Verify NO file was actually written (dry_run must not mutate)
        inbox = receiver_repo / "cross-repo" / "inbox"
        written = [f for f in inbox.iterdir() if f.name != ".gitkeep"]
        assert written == [], f"dry_run must not write; found: {written}"

    def test_act_then_dry_run_shows_collision_true(self, tmp_path, monkeypatch):
        """After act writes the memo, a subsequent dry_run reads collision:True (pre-write read)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # First act: writes the memo
        act_result = _run(_memo_send(_base_params(dry_run=False, topic="rw-order")))
        assert act_result["exit_code"] == 0

        # Subsequent dry_run: reads inbox state, sees the file already exists
        preview = _run(_memo_send(_base_params(dry_run=True, topic="rw-order")))
        assert preview["exit_code"] == 0
        candidate = preview["candidates"][0]
        # Read-before-write: the collision state was read from disk BEFORE any write would occur
        assert candidate["collision"] is True
        assert candidate.get("note") is not None
        assert "would refuse" in candidate["note"]

    def test_dry_run_does_not_mutate_inbox(self, tmp_path, monkeypatch):
        """Repeated dry_run calls do not accumulate files in the receiver inbox."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        inbox = receiver_repo / "cross-repo" / "inbox"
        initial_count = len(list(inbox.iterdir()))

        # Multiple dry_run calls
        for _ in range(3):
            result = _run(_memo_send(_base_params(dry_run=True)))
            assert result["exit_code"] == 0

        final_count = len(list(inbox.iterdir()))
        assert final_count == initial_count, "dry_run must not write files"

    def test_dry_run_runs_frontmatter_self_validation(self, tmp_path, monkeypatch, caplog):
        """dry_run rejects an invalid kind enum instead of green-lighting a doomed send.

        Regression for the 2026-07-21 dogfooding false-green: a dry_run with
        kind:"coordination" resolved the receiver, reported no collision and returned
        exit_code 0 — then the IDENTICAL payload failed loud on the real send with
        "kind 'coordination' is not a valid enum value". The dry_run branch returned
        before _compose_memo ran its frontmatter self-validation. --dry-run's contract
        is "if this passes, the real send would succeed", so a validation the act path
        enforces must also run on the preview path.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=True, topic="invalid-kind-dry-run", kind="coordination")
        with caplog.at_level("ERROR"):
            result = _run(_memo_send(params))

        assert result["exit_code"] == 1, (
            "dry_run must reject an invalid kind enum, not report success — "
            f"got exit_code {result['exit_code']}"
        )
        assert result["candidates"] == [], (
            "a rejected dry_run must not offer the memo as a deliverable candidate"
        )
        # Setup errors carry their reason in the log, not the envelope (see
        # build_setup_error_result docstring) — assert via caplog.
        assert "coordination" in caplog.text, (
            f"rejection must name the offending enum value; got: {caplog.text}"
        )

        # The rejection must still be a pure preview — nothing written.
        inbox = receiver_repo / "cross-repo" / "inbox"
        written = [f for f in inbox.iterdir() if f.name != ".gitkeep"]
        assert written == [], f"rejected dry_run must not write; found: {written}"

    def test_dry_run_and_act_agree_on_validity(self, tmp_path, monkeypatch):
        """The preview/act verdicts agree: whatever dry_run greens, act accepts.

        Pins the contract the test above protects, from the other direction — a valid
        payload must pass BOTH paths, so the shared-composition fix cannot be
        'fixed' by making dry_run reject everything.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        preview = _run(_memo_send(_base_params(dry_run=True, topic="agree-topic", kind="fyi")))
        assert preview["exit_code"] == 0, f"valid payload must pass dry_run: {preview}"

        acted = _run(_memo_send(_base_params(dry_run=False, topic="agree-topic", kind="fyi")))
        assert acted["exit_code"] == 0, (
            f"payload greened by dry_run must succeed on act: {acted}"
        )


# ===========================================================================
# 4. main_worktree_root derivation (lesson 2026-07-05-common-dir-keyed-ops-…)
# ===========================================================================

class TestWorktreeRootDerivation:
    """Verify main_worktree_root(common_dir) returns the real worktree, not .git/."""

    def test_main_worktree_root_returns_parent_of_dot_git(self, tmp_path):
        """main_worktree_root(common_dir) == common_dir.parent == real worktree root."""
        sender_repo = _make_sender_git_repo(tmp_path)
        common_dir = sender_repo / ".git"

        result = main_worktree_root(common_dir)

        assert result == common_dir.parent
        assert result == sender_repo
        # The worktree root must NOT end with .git — handler must not resolve paths against .git/
        assert not str(result).endswith(".git")
        assert not str(result).endswith(".git" + os.sep)

    def test_memo_send_with_common_dir_as_repo_root_succeeds(self, tmp_path, monkeypatch):
        """Handler with repo_root=<.git path> derives worktree correctly and delivers memo."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # repo_root is the .git common_dir as wired by _OP_KEY_SCOPE = "common_dir" in C3.
        # The handler must NOT use .git/ as the worktree root — it must call
        # main_worktree_root(common_dir) to get the real worktree (common_dir.parent).
        common_dir = str(sender_repo / ".git")

        result = _run(_memo_send(
            _base_params(dry_run=True),            repo_root=common_dir,
        ))

        # Handler succeeds — the .git common_dir was correctly resolved to the worktree
        # without trying to use .git/ as a path base for inbox lookups.
        assert result["exit_code"] == 0

    def test_memo_send_act_with_common_dir_as_repo_root_delivers_memo(self, tmp_path, monkeypatch):
        """act path with repo_root=<.git path> writes memo to receiver inbox.

        Companion to test_memo_send_with_common_dir_as_repo_root_succeeds (dry_run=True).
        Review: code-reviewer F6 — the worktree derivation lesson applies on both dry_run
        and act paths; a regression in _sender_worktree construction on the act path must
        be caught by this test.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = str(sender_repo / ".git")

        result = _run(_memo_send(
            _base_params(dry_run=False, topic="act-with-repo-root"),            repo_root=common_dir,
        ))

        assert result["exit_code"] == 0
        assert len(result["acted"]) == 1
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists(), f"memo must be written to receiver inbox: {memo_path}"
        assert memo_path.parent.name == "inbox"
        assert memo_path.parent.parent.name == "cross-repo"
        today = datetime.date.today().isoformat()
        expected_filename = _memo_filename(today, _ENGINE_ACTOR_ID, "act-with-repo-root")
        assert memo_path.name == expected_filename

    def test_memo_send_with_none_repo_root_succeeds(self, tmp_path, monkeypatch):
        """repo_root=None (before C3 wiring) is handled gracefully — delivery still works."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(
            _base_params(dry_run=True),            repo_root=None,
        ))

        assert result["exit_code"] == 0


# ===========================================================================
# 5. path-traversal rejection (absolute-override + ../)
# ===========================================================================

class TestPathTraversalRejection:
    """Absolute-override and ../ paths rejected before any filesystem mutation."""

    def test_absolute_path_as_to_is_rejected(self, tmp_path, monkeypatch):
        """An absolute filesystem path as 'to' is rejected (not registered → error)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Absolute path as receiver identity — cannot be a registered repos.* key
        evil_inbox = str(tmp_path / "evil-target" / "cross-repo" / "inbox")
        evil_to = evil_inbox

        result = _run(_memo_send(
            _base_params(to=evil_to, dry_run=False),        ))

        # Must be rejected: unregistered receiver → exit_code:1 (setup-error)
        assert result["exit_code"] == 1
        # The evil target directory must NOT have been created
        assert not (tmp_path / "evil-target").exists(), \
            "path-traversal must not create directories before rejection"

    def test_dotdot_traversal_in_to_is_rejected(self, tmp_path, monkeypatch):
        """'to' containing ../ traversal chars is rejected before any write."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # ../ traversal in receiver identity — maps to an unregistered repos.* key
        traversal_to = "example-retrieval-repo-em/../../etc"

        result = _run(_memo_send(
            _base_params(to=traversal_to, dry_run=False),        ))

        # Rejected before any write (not registered)
        assert result["exit_code"] == 1
        # No files written anywhere outside the registered inbox
        assert not (tmp_path / "etc").exists()

    def test_containment_check_rejects_inbox_not_in_allowed_set(self, tmp_path):
        """_containment_check rejects an inbox dir that is not in the registry-derived allowed-set."""
        registered_repo = tmp_path / "registered"
        registered_repo.mkdir()
        all_repos = {"repos.example_retrieval_repo": str(registered_repo)}

        # An inbox dir for a DIFFERENT, unregistered repo
        evil_inbox = tmp_path / "evil-repo" / "cross-repo" / "inbox"
        evil_inbox.mkdir(parents=True)
        evil_target = evil_inbox / "2026-07-05-test.md"

        error = _containment_check(evil_inbox, evil_target, all_repos)

        assert error is not None, "unregistered inbox must produce a containment error"
        assert "not a registry-enumerated" in error

    def test_containment_check_rejects_path_escaping_inbox(self, tmp_path):
        """_containment_check rejects a target path that escapes the inbox dir (../ inside inbox)."""
        registered_repo = tmp_path / "registered"
        registered_repo.mkdir()
        inbox_dir = registered_repo / "cross-repo" / "inbox"
        inbox_dir.mkdir(parents=True)
        all_repos = {"repos.example_retrieval_repo": str(registered_repo)}

        # A target path that tries to escape the inbox via ../
        # Note: Path resolution collapses ../ so test via the resolved form
        escape_target = inbox_dir.parent / "escaped-file.md"  # one level up from inbox

        error = _containment_check(inbox_dir, escape_target, all_repos)

        assert error is not None, "path escaping inbox must produce a containment error"

    def test_topic_slug_with_path_chars_rejected_in_validation(self):
        """Topic slugs with path chars (/, ..) are rejected by param validation, not containment."""
        # These are caught at param-validation level (before containment check)
        for bad_topic in ("../etc", "/absolute", "foo/bar"):
            result = _validate_send_params(
                {"dry_run": True, "topic": bad_topic, "to": "x-em", "title": "T", "body": ""}
            )
            assert isinstance(result, dict), f"topic={bad_topic!r} should be rejected"
            assert result["exit_code"] == 1


# ===========================================================================
# 6. registry-enumerated allowed-set
# ===========================================================================

class TestRegistryEnumeratedAllowedSet:
    """Write to an unregistered receiver inbox is rejected before any write."""

    def test_unregistered_receiver_dry_run_rejected(self, tmp_path, monkeypatch):
        """dry_run to unregistered receiver → exit_code:1, no filesystem mutation."""
        # Registry has only example-retrieval-repo registered
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(
            _base_params(to="unregistered-sibling-em", dry_run=True),        ))

        assert result["exit_code"] == 1  # setup-error: receiver not registered

    def test_unregistered_receiver_act_rejected(self, tmp_path, monkeypatch):
        """act to unregistered receiver → exit_code:1 setup-error, no file written."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        unregistered_to = "completely-unregistered-em"
        result = _run(_memo_send(
            _base_params(to=unregistered_to, dry_run=False),        ))

        assert result["exit_code"] == 1
        # No file written anywhere
        inbox = receiver_repo / "cross-repo" / "inbox"
        written = [f for f in inbox.iterdir() if f.name != ".gitkeep"]
        assert written == [], "unregistered receiver must not write to registered inbox"

    def test_registered_receiver_allowed(self, tmp_path, monkeypatch):
        """Registered receiver is allowed and act succeeds."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False)))
        assert result["exit_code"] == 0

    def test_empty_registry_all_receivers_rejected(self, tmp_path, monkeypatch):
        """With an empty registry (no repos.*), no receiver can be resolved."""
        claude_home = _make_claude_home(tmp_path, {})  # no repos registered
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=True)))
        assert result["exit_code"] == 1  # no repos registered

    def test_unresolved_to_suggests_nearest_registered_receiver(
        self, tmp_path, monkeypatch, caplog
    ):
        """C4 (footgun #2): an unresolved --to gets a 'did you mean?' suggestion.

        The plan's own worked example: 'claude-klabauter-em' -> suggests 'claude-klabauter-em'.
        Suggestion-only — the send still fails loud (exit_code:1, no write); the
        setup-error reason is logged daemon-side (build_setup_error_result docstring),
        so assert via caplog, not the returned dict.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"claude_klabauter": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        with caplog.at_level("ERROR"):
            result = _run(_memo_send(
                _base_params(to="claude-klabauter-em", dry_run=False),
            ))

        assert result["exit_code"] == 1
        assert "claude-klabauter-em" in caplog.text
        inbox = receiver_repo / "cross-repo" / "inbox"
        written = [f for f in inbox.iterdir() if f.name != ".gitkeep"]
        assert written == [], "a mere suggestion must never auto-select/write to a receiver"

    def test_containment_blocks_unregistered_inbox_even_if_resolved(self, tmp_path):
        """_containment_check rejects a resolved inbox dir that is not in the allowed-set.

        This tests the second containment layer: the allowed-set check is independent of
        the receiver-resolution step. Even if an inbox was somehow resolved to a path,
        it must be in the registry-derived allowed-set.
        """
        # Only repo_a is registered
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        all_repos = {"repos.repo_a": str(repo_a)}

        # repo_b is NOT registered — its inbox must be rejected
        repo_b_inbox = tmp_path / "repo-b" / "cross-repo" / "inbox"
        repo_b_inbox.mkdir(parents=True)
        repo_b_target = repo_b_inbox / "2026-07-05-evil.md"

        error = _containment_check(repo_b_inbox, repo_b_target, all_repos)

        assert error is not None, "unregistered inbox must be rejected by containment"
        assert "not a registry-enumerated" in error

    def test_registry_key_convention(self):
        """Receiver EM identity → repos.* key conversion follows the correct convention."""
        assert _receiver_em_to_repo_key("example-retrieval-repo-em") == "repos.example_retrieval_repo"
        assert _receiver_em_to_repo_key("example-doctrine-repo-em") == "repos.example_doctrine_repo"
        assert _receiver_em_to_repo_key("claude-klabauter-em") == "repos.claude_klabauter"
        # Without -em suffix (bare shortname is also accepted)
        assert _receiver_em_to_repo_key("example-retrieval-repo") == "repos.example_retrieval_repo"

    def test_multiple_registered_repos_all_inboxes_in_allowed_set(self, tmp_path, monkeypatch):
        """When multiple repos are registered, their inboxes are all in the allowed-set."""
        repo_a = _make_receiver_git_repo(tmp_path, "repo-a")
        repo_b = _make_receiver_git_repo(tmp_path, "repo-b")
        claude_home = _make_claude_home(tmp_path, {"repo_a": repo_a, "repo_b": repo_b})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Sending to repo_a should be allowed
        result_a = _run(_memo_send(
            _base_params(to="repo-a-em", dry_run=False, topic="send-to-a"),        ))
        assert result_a["exit_code"] == 0, f"sending to repo-a-em should succeed: {result_a}"

        # Sending to repo_b should also be allowed
        result_b = _run(_memo_send(
            _base_params(to="repo-b-em", dry_run=False, topic="send-to-b"),        ))
        assert result_b["exit_code"] == 0, f"sending to repo-b-em should succeed: {result_b}"

        # Each memo ends up in the correct receiver's inbox
        inbox_a = repo_a / "cross-repo" / "inbox"
        inbox_b = repo_b / "cross-repo" / "inbox"
        a_memos = [f for f in inbox_a.iterdir() if f.suffix == ".md"]
        b_memos = [f for f in inbox_b.iterdir() if f.suffix == ".md"]
        assert any("send-to-a" in f.name for f in a_memos)
        assert any("send-to-b" in f.name for f in b_memos)


# ===========================================================================
# 7. Unit tests for internal helpers
# ===========================================================================

class TestInternalHelpers:
    """Unit tests for the _write_memo_file helper and compose helpers."""

    def test_write_memo_file_creates_file(self, tmp_path):
        """_write_memo_file creates the target file with the given content."""
        target = tmp_path / "inbox" / "2026-07-05-test.md"
        content = "---\nstatus: open\n---\n\nBody.\n"

        _write_memo_file(target, content)

        assert target.exists()
        assert target.read_text(encoding="utf-8") == content

    def test_write_memo_file_creates_parent_dirs(self, tmp_path):
        """_write_memo_file creates parent directories if they do not exist."""
        target = tmp_path / "a" / "b" / "c" / "memo.md"
        _write_memo_file(target, "content")
        assert target.exists()

    def test_write_memo_file_raises_on_collision(self, tmp_path):
        """_write_memo_file raises FileExistsError when target already exists (O_EXCL)."""
        target = tmp_path / "memo.md"
        target.write_text("existing content", encoding="utf-8")

        with pytest.raises(FileExistsError):
            _write_memo_file(target, "new content")

        # Original content is preserved (no clobber)
        assert target.read_text(encoding="utf-8") == "existing content"

    def test_read_registry_repos_returns_empty_on_missing_home(self, tmp_path, monkeypatch):
        """_read_registry_repos returns {} when CLAUDE_HOME points to an empty dir."""
        missing_home = tmp_path / "nonexistent-claude-home"
        missing_home.mkdir()
        # machine-local subdir does not exist — no registry files
        monkeypatch.setenv("CLAUDE_HOME", str(missing_home))

        result = _read_registry_repos()
        assert result == {}

    def test_read_registry_repos_loads_local_overrides(self, tmp_path, monkeypatch):
        """_read_registry_repos merges baseline + local registry (local wins)."""
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": tmp_path / "rag-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _read_registry_repos()

        assert "repos.example_retrieval_repo" in result
        assert result["repos.example_retrieval_repo"] == str(tmp_path / "rag-repo")

    def test_git_check_ignore_unignored_path_returns_false(self, tmp_path):
        """_git_check_ignore returns False for a path not in .gitignore.

        Exercises the hardened-env subprocess path (env=_make_git_env()) added by
        Review: code-reviewer F2 — strips GIT_EXEC_PATH etc. to prevent a subverted
        check-ignore from bypassing the B3 delivery guard.
        """
        repo = _make_receiver_git_repo(tmp_path)
        result = _run(_git_check_ignore(repo, "cross-repo/inbox/2026-07-05-test.md"))
        assert result is False  # path not gitignored → safe to deliver

    def test_read_registry_repos_skips_empty_string_values(self, tmp_path, monkeypatch):
        """_read_registry_repos excludes declared-but-unset (empty string) keys."""
        machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text(
            'schema = 1\n"repos.declared_unset" = ""\n', encoding="utf-8"
        )
        (machine_local / "registry.local.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))

        result = _read_registry_repos()
        assert "repos.declared_unset" not in result


# ===========================================================================
# 8. Collision fail-loud (C4 — C1 D2 criterion 4 ratified semantics)
# ===========================================================================

class TestCollisionFailLoud:
    """C4: same-day/same-topic collision → fail-loud (refuse, no silent clobber).

    Collision semantics ratified in C1 (DR-214 D2 criterion 4, example-doctrine-repo-normative 2026-07-05):
    read-before-write → refuse on same-day/same-topic collision.
    Prior art: the Staff Engineer finding #2b; improvement-queue 2026-06-23-hot-shared-branch-a-cross-
    repo-memo-repl.yaml (canonical same-day/same-topic replace/clobber source); memo
    dup-key silent-drop incident 2026-06-17.

    Spec backlink:
        docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md § C4
        DR-214: docs/decisions/DR-214-send-class-cross-tree-write-boundary.md § D2 criterion 4

    Negative-spec: nonce/content-hash suffix is NOT a test variant — that would require a
    example-doctrine-repo-coordinated filename-contract change across all 5 lockstep sites (strang-03 § C4).
    """

    def test_same_topic_second_act_refused(self, tmp_path, monkeypatch):
        """Second act with same day/topic is refused; pre-existing file content is unchanged.

        First act writes the memo. Second act (same topic, same date) must be refused
        fail-loud (C1 D2 criterion 4):
          - exit_code:2 DETERMINATE-PARTIAL returned (error to caller, no silent clobber)
          - failed[] is non-empty; acted[] is empty
          - the pre-existing file content is byte-for-byte intact
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=False, topic="collision-topic")

        # First act: must succeed (no prior file).
        first_result = _run(_memo_send(params))
        assert first_result["exit_code"] == 0, f"first act should succeed: {first_result}"
        assert len(first_result["acted"]) == 1

        # Capture original file content — must survive the second act unchanged.
        memo_path = Path(first_result["acted"][0]["id"])
        assert memo_path.exists(), "first act must have written the memo file"
        original_content = memo_path.read_text(encoding="utf-8")

        # Second act: same topic (same date — tests run within one calendar day) → REFUSED.
        second_params = {**params, "body": "REPLACEMENT body — must NOT overwrite original"}
        second_result = _run(_memo_send(second_params))

        # Error returned to caller (exit_code:2 = DETERMINATE-PARTIAL with failed[] non-empty).
        # NOTE: exit_code:1 is reserved for setup errors; collision is exit_code:2 per
        # build_act_result contract (_common.py line 211: exit_code = 2 if failed else 0).
        assert second_result["exit_code"] == 2, (
            f"same-day/same-topic collision must be refused with exit_code:2 "
            f"(DETERMINATE-PARTIAL, failed[] non-empty): {second_result}"
        )
        assert second_result["acted"] == [], "collision must not produce acted items"
        assert len(second_result["failed"]) >= 1, "collision must produce at least one failed item"

        # Pre-existing file content is unchanged (no silent clobber — D2 criterion 4).
        assert memo_path.exists(), "collision must not delete pre-existing memo"
        current_content = memo_path.read_text(encoding="utf-8")
        assert current_content == original_content, (
            "collision must not clobber pre-existing memo content\n"
            f"expected (original):\n{original_content}\n"
            f"got (after refused second act):\n{current_content}"
        )

    def test_collision_failed_item_references_target_path(self, tmp_path, monkeypatch):
        """The failed item on collision has an 'id' field pointing to the contested target path."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "collision-failed-item-test"

        # First act: write the memo.
        first_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert first_result["exit_code"] == 0

        memo_path = Path(first_result["acted"][0]["id"])

        # Second act: collision → failed item must name the contested path.
        second_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert second_result["exit_code"] == 2

        failed_item = second_result["failed"][0]
        assert "id" in failed_item, "failed item must have an 'id' field"
        assert Path(failed_item["id"]) == memo_path, (
            f"failed item 'id' must point to the contested memo path {memo_path}; "
            f"got: {failed_item['id']}"
        )

    def test_collision_does_not_grow_inbox_file_count(self, tmp_path, monkeypatch):
        """On collision, no extra files are written to the inbox (file count unchanged)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "no-extra-files-on-collision"
        inbox = receiver_repo / "cross-repo" / "inbox"

        # First act: writes one memo.
        first_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert first_result["exit_code"] == 0

        count_after_first = len(list(inbox.iterdir()))

        # Second act: same topic → refused; inbox file count must not increase.
        second_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert second_result["exit_code"] == 2

        count_after_second = len(list(inbox.iterdir()))
        assert count_after_second == count_after_first, (
            f"collision must not write additional files; inbox had {count_after_first} "
            f"entries before the refused second act and {count_after_second} after"
        )

    def test_collision_cross_sender_both_survive(self, tmp_path, monkeypatch):
        """DR-026: two DIFFERENT senders, same topic + same day → both memos survive.

        Mirrors example-doctrine-repo's test_collision_cross_sender_both_survive (cross-repo-memo-
        roundtrip.test.py) — the whole point of the DR-026 sender-namespaced
        filename is that an N-repo broadcast reply with an identical topic slug
        on the same day does not collapse into a single-writer collision. Both
        act calls must succeed; both files must exist on disk with distinct
        sender-namespaced filenames; neither is clobbered.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "roundtrip-collision"
        params_alpha = {
            **_base_params(dry_run=False, topic=topic),
            "from_id": "sender-repo-alpha",
            "title": "From Alpha",
        }
        params_beta = {
            **_base_params(dry_run=False, topic=topic),
            "from_id": "sender-repo-beta",
            "title": "From Beta",
        }

        result_alpha = _run(_memo_send(params_alpha))
        result_beta = _run(_memo_send(params_beta))

        assert result_alpha["exit_code"] == 0, f"sender alpha act should succeed: {result_alpha}"
        assert result_beta["exit_code"] == 0, f"sender beta act should succeed: {result_beta}"

        path_alpha = Path(result_alpha["acted"][0]["id"])
        path_beta = Path(result_beta["acted"][0]["id"])

        assert path_alpha != path_beta, "cross-sender same-topic acts must not collide on filename"
        assert path_alpha.exists(), f"alpha memo must exist: {path_alpha}"
        assert path_beta.exists(), f"beta memo must exist: {path_beta}"

        today = datetime.date.today().isoformat()
        expected_alpha = _memo_filename(today, "sender-repo-alpha", topic)
        expected_beta = _memo_filename(today, "sender-repo-beta", topic)
        assert path_alpha.name == expected_alpha
        assert path_beta.name == expected_beta
        assert path_alpha.name != path_beta.name

        # Both contents survive distinctly — no clobber.
        content_alpha = path_alpha.read_text(encoding="utf-8")
        content_beta = path_beta.read_text(encoding="utf-8")
        assert '"From Alpha"' in content_alpha
        assert '"From Beta"' in content_beta


# ===========================================================================
# 8a. Act-path error precedence: compose error wins over collision/gitignore
#
# Review: code-reviewer Finding 1 (2026-07-21 codereview slicememo-send-
# dryrun-hoist) — the dry_run hoist moved `_compose_memo` (and its
# ValueError on an invalid `kind` enum) ahead of BOTH the collision check
# and the B3 gitignore guard on the act path. This precedence was reachable
# and asserted nowhere. EM decision: pin the CURRENT precedence (compose
# error wins) with a regression test rather than reorder the checks —
# composition is pure/cheap, an invalid payload can never be delivered
# regardless of collision state, and failing on the malformed payload first
# is the more fundamental error. See the finding sidecar for the full
# tradeoff; do not silently flip this ordering in a future refactor without
# updating these tests.
# ===========================================================================

class TestActPathErrorPrecedence:
    """Pins: an invalid+colliding (or invalid+gitignored) payload reports the
    compose error, not the collision/gitignore-guard error, on the act path."""

    def test_invalid_kind_and_collision_reports_compose_error_not_collision(
        self, tmp_path, monkeypatch
    ):
        """A payload that is simultaneously invalid (bad kind enum) AND colliding
        with an existing file must report the compose (setup-error, exit_code:1)
        error — NOT the collision (exit_code:2) error."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "precedence-collision-test"

        # First act: valid payload writes the memo, establishing the collision.
        first_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert first_result["exit_code"] == 0, f"setup write should succeed: {first_result}"

        # Second act: same topic (collision) AND an invalid kind enum.
        second_params = _base_params(dry_run=False, topic=topic, kind="coordination")
        second_result = _run(_memo_send(second_params))

        assert second_result["exit_code"] == 1, (
            "compose error must win over the collision error when both apply "
            f"— got exit_code {second_result['exit_code']}: {second_result}"
        )
        assert second_result["acted"] == []
        assert second_result["failed"] == [], (
            "a setup-error (exit_code:1) envelope carries no failed[] items — "
            "that shape is reserved for build_act_result's collision path"
        )

    def test_invalid_kind_and_gitignored_reports_compose_error_not_gitignore_guard(
        self, tmp_path, monkeypatch
    ):
        """A payload that is simultaneously invalid (bad kind enum) AND would be
        gitignore-blocked must report the compose (setup-error, exit_code:1)
        error — the B3 gitignore guard is never reached."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "precedence-gitignore-test"

        # gitignore every .md file under cross-repo/inbox/ in the receiver repo.
        gitignore_path = receiver_repo / "cross-repo" / "inbox" / ".gitignore"
        gitignore_path.write_text("*.md\n", encoding="utf-8")

        params = _base_params(dry_run=False, topic=topic, kind="coordination")
        result = _run(_memo_send(params))

        assert result["exit_code"] == 1, (
            "compose error must win over the gitignore-delivery-guard error "
            f"when both apply — got exit_code {result['exit_code']}: {result}"
        )
        assert result["acted"] == []
        assert result["failed"] == [], (
            "a setup-error (exit_code:1) envelope carries no failed[] items — "
            "that shape is reserved for build_act_result's gitignore-guard path"
        )
        inbox = receiver_repo / "cross-repo" / "inbox"
        written = [
            f for f in inbox.iterdir() if f.name not in (".gitkeep", ".gitignore")
        ]
        assert written == [], "compose-rejected payload must not write any file"


# ===========================================================================
# 8b. supersedes: sanctioned re-delivery (C6, footgun #5, A6)
# ===========================================================================

class TestSupersedesRedelivery:
    """C6 / A6: `supersedes:` is the sanctioned re-delivery path for footgun #5
    (same date+topic re-send), replacing the hand-edit-the-receiver workaround —
    a FRESH dated file, never an in-place `--force`/`--replace` clobber.

    Spec backlink:
        docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C6, AC A6
        DR-214: docs/decisions/DR-214-send-class-cross-tree-write-boundary.md § D2 criterion 4
    """

    def test_supersedes_redelivery_writes_new_file_and_preserves_prior(
        self, tmp_path, monkeypatch
    ):
        """Re-send with the SAME topic + `supersedes:` set writes a fresh file
        and does not touch the prior memo.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "supersedes-redelivery-topic"

        first_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert first_result["exit_code"] == 0, f"first act should succeed: {first_result}"
        prior_path = Path(first_result["acted"][0]["id"])
        prior_content = prior_path.read_text(encoding="utf-8")

        redelivery_params = {
            **_base_params(dry_run=False, topic=topic),
            "body": "Corrected body — sanctioned re-delivery, not a clobber.",
            "supersedes": topic,
        }
        second_result = _run(_memo_send(redelivery_params))
        assert second_result["exit_code"] == 0, (
            f"supersedes: re-delivery must succeed, not collide: {second_result}"
        )
        assert len(second_result["acted"]) == 1

        new_path = Path(second_result["acted"][0]["id"])
        assert new_path != prior_path, (
            "supersedes: re-delivery must land at a distinct path from the prior memo"
        )
        assert new_path.exists(), "re-delivery must have written a new file"

        # Prior memo is byte-for-byte untouched (no in-place clobber).
        assert prior_path.exists(), "supersedes: re-delivery must not delete the prior memo"
        assert prior_path.read_text(encoding="utf-8") == prior_content, (
            "supersedes: re-delivery must not mutate the prior memo's content"
        )

        # New memo carries the supersedes: frontmatter field and the new body.
        new_content = new_path.read_text(encoding="utf-8")
        assert f'supersedes: "{topic}"' in new_content
        assert "Corrected body" in new_content

    def test_without_supersedes_same_topic_still_refuses(self, tmp_path, monkeypatch):
        """Without `supersedes:`, same date+topic re-send is unchanged: still
        refuses fail-loud (C1 D2 criterion 4) — this mechanism is opt-in only.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "no-supersedes-still-refuses"

        first_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert first_result["exit_code"] == 0

        second_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert second_result["exit_code"] == 2, (
            f"same-day/same-topic collision without supersedes: must still refuse: {second_result}"
        )
        assert second_result["acted"] == []
        assert len(second_result["failed"]) >= 1

    def test_redelivery_filename_disambiguates_via_supersedes_slug(self):
        """_redelivery_filename derives a distinct, deterministic filename from
        the supersedes reference — same DR-026 <date>-<sender>-<topic> prefix.
        """
        today = "2026-07-21"
        base = _memo_filename(today, "example-retrieval-repo-em", "my-topic")
        redelivered = _redelivery_filename(today, "example-retrieval-repo-em", "my-topic", "my-topic")
        assert redelivered != base
        assert redelivered.startswith(base[: -len(".md")])
        assert redelivered.endswith(".md")
        assert "supersedes" in redelivered

    def test_second_same_day_redelivery_of_same_topic_fails_loud(self, tmp_path, monkeypatch):
        """Finding 3 (2026-07-21 codereview slicelaneA-memo-tool-rebuild): the
        redelivery-path check-then-write (`redelivery_path.exists()` followed by
        `_write_memo_file`'s O_EXCL open) is TOCTOU, which is fine mechanically
        (O_EXCL still wins any true race and fails loud) — but `_redelivery_filename`
        is deterministic on date+sender+topic+supersedes, NOT a nonce, so a SECOND
        `supersedes:` re-delivery for the identical topic on the same day wants the
        SAME redelivery filename as the first. `_redelivery_filename`'s own docstring
        calls this out: "Callers MUST check this path for a *further* collision
        themselves ... residual collision still refuses fail-loud rather than
        layering on a second disambiguator." This test exercises exactly that
        "two re-deliveries superseding the identical prior memo on the same day"
        case — previously asserted only in prose, not in a test — and asserts the
        second attempt is a fail-loud collision, not a silent second write or an
        unhandled exception.
        """
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "double-redelivery-same-day-same-topic"

        # Original send — establishes the base DR-026 filename.
        first_result = _run(_memo_send(_base_params(dry_run=False, topic=topic)))
        assert first_result["exit_code"] == 0, f"initial send should succeed: {first_result}"

        redelivery_params = {
            **_base_params(dry_run=False, topic=topic),
            "supersedes": topic,
        }

        # First redelivery — the sanctioned path, succeeds and writes the
        # redelivery filename (_redelivery_filename(today, from_id, topic, topic)).
        second_result = _run(_memo_send(redelivery_params))
        assert second_result["exit_code"] == 0, (
            f"first supersedes: redelivery must succeed: {second_result}"
        )
        redelivery_path = Path(second_result["acted"][0]["id"])
        assert redelivery_path.exists()
        redelivery_content = redelivery_path.read_text(encoding="utf-8")

        # Second redelivery — SAME topic + SAME supersedes reference on the SAME
        # day derives the IDENTICAL redelivery filename, which now already exists.
        # Per the docstring this residual collision is NOT layered with a second
        # disambiguator — it must refuse fail-loud, same as any other collision.
        third_result = _run(_memo_send(redelivery_params))
        assert third_result["exit_code"] == 2, (
            f"second same-day/same-topic/same-supersedes redelivery must fail "
            f"loud (residual collision), not silently double-write or raise an "
            f"unhandled exception: {third_result}"
        )
        assert third_result["acted"] == [], (
            f"residual collision must write nothing: {third_result}"
        )
        assert len(third_result["failed"]) == 1
        assert "collision" in third_result["failed"][0]["reason"]

        # Finding 2 (2026-07-21 codereview slicememo-send-deferred-review-
        # findings): the reported contested path must be the REDELIVERY file
        # that actually collided, not the original base DR-026 file the
        # caller's `supersedes:` was superseding. Pins both the `id` field
        # and the filename named in `reason` against the real redelivery
        # path computed above (not the base filename, which would be the
        # pre-fix misdirected report).
        assert third_result["failed"][0]["id"] == str(redelivery_path), (
            f"residual collision must report the REDELIVERY path that "
            f"actually blocked the second attempt, not the original base "
            f"file: expected id={str(redelivery_path)!r}, "
            f"got {third_result['failed'][0]['id']!r}"
        )
        assert redelivery_path.name in third_result["failed"][0]["reason"], (
            f"residual collision reason must name the redelivery filename "
            f"{redelivery_path.name!r}, not the base filename: "
            f"{third_result['failed'][0]['reason']!r}"
        )

        # No silent second write — the redelivery file from the first (successful)
        # redelivery is byte-for-byte untouched.
        assert redelivery_path.exists()
        assert redelivery_path.read_text(encoding="utf-8") == redelivery_content


class TestMemoSendThreeSeamRegistration:
    """Finding 2 (2026-07-21 codereview slicelaneA-memo-tool-rebuild): identity-
    assertion coverage for the op-registration wire seam. An op must be wired
    at THREE distinct seams to dispatch correctly:

      1. authz/classification.py — OP_CLASSIFICATION entry (privilege gate;
         classify() fail-closed KeyErrors if the op is absent).
      2. op_scopes.py — _OP_KEY_SCOPE entry (repo-key derivation scope; an op
         absent from this table silently degrades to scope "none" — repo_root
         is never injected — per lesson
         2026-07-06-compute-only-op-registration-needs-an-op).
      3. ops/__init__.py — _EAGER_OP_MODULES entry (the import that triggers
         the op module's register_op() side-effect and populates
         ipc._REGISTRY under a bare `import coordinator_core.ops` — the path
         pytest collection AND the real production package-init default both
         take when COORDINATOR_CORE_LAZY_OPS is unset).

    A DIRECT `import coordinator_core.ops.fleet.memo_send` (bypassing the
    `coordinator_core.ops` package's own __init__) makes a bare
    `get_op_handler("memo.send")` (or `"memo.send" in ipc._REGISTRY`) check
    FALSE-POSITIVE: it proves the submodule imports and self-registers in
    isolation, not that ops/__init__.py's _EAGER_OP_MODULES table actually
    lists it. A prior incident shipped an op registered at only two of the
    three seams and this exact false-positive shape masked the gap (see
    op_scopes.py's records.query backfill comment for the sibling incident:
    "registered without an _OP_KEY_SCOPE entry by a concurrent session,
    leaving the key-scope coverage gate RED on HEAD"). This test imports
    `coordinator_core.ops` (the PACKAGE, not the submodule) to reproduce the
    real production/test-collection import path, then asserts memo.send is
    present and consistent across all three seams.

    Spec backlink: state/review-trail/findings/2026-07-21-codereview-
    slicelaneA-memo-tool-rebuild-coordinator-core-ops-fleet-coordinator-c.md
    Finding 2
    """

    def test_memo_send_registered_at_all_three_seams(self):
        import coordinator_core.ipc as ipc
        import coordinator_core.ops as ops_pkg
        from coordinator_core.authz.classification import OpClass, classify

        # Seam 3 — assert on the DECLARATION (ops/__init__.py's
        # _EAGER_OP_MODULES table), not the runtime-populated registry.
        # This is a stronger fix than subprocess isolation: a clean
        # subprocess that imports ONLY `coordinator_core.ops` is STILL
        # vacuous here, because `_EAGER_OP_MODULES` also lists
        # `memo_draft`, `memo_list`, and `memo_compose` — each of which
        # imports directly `from coordinator_core.ops.fleet.memo_send
        # import (...)` at module scope for shared helpers
        # (`_TOPIC_SLUG_RE`, `_yaml_quote`, `_memo_filename`, etc). So
        # `_eager_import_all()` self-registers "memo.send" via ANY ONE of
        # those three sibling entries, transitively, regardless of
        # whether memo_send's OWN entry is present in the table —
        # empirically confirmed by removing ONLY the
        # `("coordinator_core.ops.fleet.memo_send", ...)` tuple from
        # `_EAGER_OP_MODULES` and re-running a subprocess-isolated probe
        # (`import coordinator_core.ops; "memo.send" in ipc._REGISTRY`)
        # in a fresh interpreter — it still evaluated True (Finding 1,
        # 2026-07-21 codereview slicememo-send-deferred-review-findings;
        # this transitive-import wrinkle was not itself named in the
        # finding — no combination of "import the package, check the
        # registry" can catch the regression the finding describes, in
        # any process, given the current sibling-module import graph).
        # A direct list-membership assertion on the declaration itself
        # sidesteps the whole import-order/transitive-import question and
        # is the only check that reliably fails when the entry is
        # dropped.
        eager_module_paths = {path for path, _note in ops_pkg._EAGER_OP_MODULES}
        assert "coordinator_core.ops.fleet.memo_send" in eager_module_paths, (
            "'coordinator_core.ops.fleet.memo_send' is missing from "
            "ops/__init__.py's _EAGER_OP_MODULES table — a bare `import "
            "coordinator_core.ops` (the path pytest collection AND the "
            "real production package-init default both take when "
            "COORDINATOR_CORE_LAZY_OPS is unset) would no longer "
            "guarantee 'memo.send' registers."
        )

        # Now safe to import the package in-process for the registry
        # sanity check and seams 1/2 below.
        import coordinator_core.ops  # noqa: F401
        assert "memo.send" in ipc._REGISTRY
        assert callable(ipc._REGISTRY["memo.send"])

        # Seam 1 — authz/classification.py's OP_CLASSIFICATION table.
        assert classify("memo.send") is OpClass.MUTATING, (
            "'memo.send' must be classified MUTATING in "
            "authz/classification.py's OP_CLASSIFICATION registry (DR-208 "
            "five-question affirmation)."
        )

        # Seam 2 — op_scopes.py's _OP_KEY_SCOPE table, read via ipc.OP_KEY_SCOPE.
        assert "memo.send" in ipc.OP_KEY_SCOPE, (
            "'memo.send' is missing from op_scopes._OP_KEY_SCOPE — an op "
            "absent from this table silently degrades to scope 'none' "
            "(repo_root never injected) instead of failing loud "
            "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
        )
        assert ipc.OP_KEY_SCOPE["memo.send"] == "common_dir", (
            "memo.send's handler derives the sender's own worktree via "
            "main_worktree_root(common_dir) to run the own-inbox-refusal "
            f"check — expected scope 'common_dir', got "
            f"{ipc.OP_KEY_SCOPE['memo.send']!r}."
        )

    def test_ipc_op_key_scope_is_op_scopes_table_not_a_duplicate(self):
        """Finding 2's literal suggested fix: pin `ipc.OP_KEY_SCOPE is
        op_scopes.OP_KEY_SCOPE` (same object, not a copy) so a future edit
        that reintroduces a second, divergent `_OP_KEY_SCOPE`-shaped table in
        ipc.py fails this test loud instead of silently diverging from
        op_scopes.py's canonical table (op_scopes.py is the live source;
        ipc.py only re-exports it — confirmed on disk at review time)."""
        import coordinator_core.ipc as ipc
        import coordinator_core.op_scopes as op_scopes

        assert ipc.OP_KEY_SCOPE is op_scopes.OP_KEY_SCOPE
        assert ipc._OP_KEY_SCOPE is op_scopes._OP_KEY_SCOPE


# ===========================================================================
# 9. Q-d store-less-ness architecture test (C6 — AC8)
# ===========================================================================

class TestNoMemoIndex:
    """C6 / AC8: no module-level dict/store accumulates memo state across handler calls.

    The handler is stateless per-request; ipc.py's existing no-persisted-handler-state
    negative-spec satisfies this structurally. This test locks the structural invariant
    against future refactors — an accidental addition of a module-level index/store/cache
    would cause this test to fail before it silently corrupts send semantics (Q-d invariant).

    Two layers:
      1. Structural (test_no_memo_index): inspect module globals at import time — no
         mutable collection (dict/list/set) is present at module scope.
      2. Runtime (test_handler_calls_do_not_mutate_module_state): run the handler several
         times and verify no new mutable names appear at module scope afterward.

    Spec backlink:
        docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md § C6
        AC8: no module-level or persisted fleet-wide memo index (store-less-ness invariant).

    Negative-spec: tests only module-level bindings — the surface where a "store" would
    land if accidentally introduced as a global variable. Runtime state inside a single
    handler invocation (local dicts) is out of scope; those do not persist across calls.
    """

    def test_no_memo_index(self):
        """No module-level mutable collection (dict/list/set) in memo_send acts as a memo store.

        Structurally inspects coordinator_core.ops.fleet.memo_send module globals for any
        mutable collection type that could serve as a fleet-wide memo index or accumulator.
        Permitted module-level names: immutable constants (str, int, re.Pattern), loggers,
        and imported names (modules, functions, classes). Mutable collections (dict/list/set)
        are NOT permitted — their presence at module scope would violate Q-d.

        This is an architecture-level assertion (AC8): it catches a future violation at
        the point of introduction, before any runtime behaviour test could notice growth.
        """
        import types
        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        # Enumerate all module-level bindings, excluding dunder attrs (module machinery).
        module_globals = {
            name: val
            for name, val in vars(memo_send_mod).items()
            if not name.startswith("__")
        }

        # Mutable collections (dict/list/set) at module scope would constitute a memo
        # index/store and violate the Q-d store-less-ness invariant.
        # Imported modules and callable objects (functions, classes) are not stores.
        mutable_collections = {
            name: val
            for name, val in module_globals.items()
            if isinstance(val, (dict, list, set))
            and not isinstance(val, types.ModuleType)
        }

        assert mutable_collections == {}, (
            f"memo_send module MUST NOT contain module-level mutable collections "
            f"(dict/list/set) — any such binding would violate the Q-d store-less-ness "
            f"invariant (AC8: no fleet-wide memo index). "
            f"Found violating names: {sorted(mutable_collections.keys())}"
        )

    def test_handler_calls_do_not_mutate_module_state(self, tmp_path, monkeypatch):
        """Multiple handler calls leave no accumulated mutable state in memo_send module globals.

        Runs the handler with several distinct call shapes (dry_run, act, unregistered
        receiver) and verifies the set of module-level mutable names is identical before
        and after — no new dict/list/set appears at module scope as a side-effect.

        This runtime layer complements the structural test: it catches a scenario where
        a mutable name is created lazily (e.g. on first call via a module-level assignment
        inside a function) rather than at import time.
        """
        import coordinator_core.ops.fleet.memo_send as memo_send_mod

        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        def _mutable_module_names():
            """Return frozenset of module-scope names currently bound to mutable collections."""
            return frozenset(
                name
                for name, val in vars(memo_send_mod).items()
                if isinstance(val, (dict, list, set))
                and not name.startswith("__")
            )

        # Snapshot before any handler call.
        names_before = _mutable_module_names()

        # dry_run call (read-only path).
        _run(_memo_send(_base_params(dry_run=True, topic="state-check-dry")))

        # act call (write path — actually delivers a memo).
        _run(_memo_send(_base_params(dry_run=False, topic="state-check-act")))

        # Error path: unregistered receiver (setup-error envelope returned).
        _run(_memo_send(_base_params(to="unregistered-em", dry_run=True)))

        # Snapshot after all handler calls.
        names_after = _mutable_module_names()

        assert names_after == names_before, (
            f"memo_send module must not accumulate new module-level mutable state "
            f"across handler calls (Q-d store-less-ness invariant, AC8). "
            f"Names that appeared after handler calls: {sorted(names_after - names_before)}"
        )


# ===========================================================================
# 10. Receiver alias resolution via example-doctrine-repo manifest (example-doctrine-repo consult 2026-07-05 strang-03 Q2)
# ===========================================================================

class TestReceiverAliasResolution:
    """Manifest-driven alias resolution for _receiver_em_to_repo_key.

    Spec backlink: example-doctrine-repo consult 2026-07-05 strang-03 follow-up, Q2.
    Aliases (identity.repoAliases) and central IDs (identity.centralReceiverIds)
    are read from coordinator-registry.manifest.json via the .doe-root sentinel.
    """

    def test_receiver_alias_resolves_via_manifest(self, tmp_path, monkeypatch):
        """example-game-repo-em → repos.example_game_workbench_repo via manifest alias lookup."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [
            {
                "registryKey": "example_game_workbench_repo",
                "dirBasename": "example-game-workbench-repo",
                "shortname": "example-game-repo",
            }
        ])

        # example-game-repo-em strips to shortname "example-game-repo"; aliases["example-game-repo"] = "example_game_workbench_repo"
        result = _receiver_em_to_repo_key("example-game-repo-em")
        assert result == "repos.example_game_workbench_repo", (
            f"example-game-repo-em must resolve via manifest alias to repos.example_game_workbench_repo; "
            f"got: {result}"
        )

    def test_receiver_convention_when_no_alias(self, tmp_path, monkeypatch):
        """example-retrieval-repo-em → repos.example_retrieval_repo via convention when no alias entry in manifest."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # Manifest present but only has example-game-repo alias — example-retrieval-repo is not aliased
        _make_doe_manifest(tmp_path, claude_home, [
            {
                "registryKey": "example_game_workbench_repo",
                "dirBasename": "example-game-workbench-repo",
                "shortname": "example-game-repo",
            }
        ])

        result = _receiver_em_to_repo_key("example-retrieval-repo-em")
        assert result == "repos.example_retrieval_repo", (
            f"example-retrieval-repo-em must fall back to convention (no alias); got: {result}"
        )

    def test_alias_graceful_degradation_when_manifest_absent(self, tmp_path, monkeypatch):
        """No .doe-root/manifest → convention still works, no exception raised."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # Deliberately do NOT write .doe-root or any manifest

        # Convention path must still function
        assert _receiver_em_to_repo_key("example-retrieval-repo-em") == "repos.example_retrieval_repo"
        assert _receiver_em_to_repo_key("example-game-repo-em") == "repos.example-game-repo"

        # _read_receiver_aliases itself must return {} without raising
        aliases = _read_receiver_aliases()
        assert aliases == {}, f"absent sentinel must yield empty alias map; got: {aliases}"


# ===========================================================================
# 11. Own-inbox refusal (invariant c)
# ===========================================================================

class TestOwnInboxRefusal:
    """A repo must not write into its own inbox (memo_send.py invariant c).

    Fires only when the sender worktree is derivable (repo_root supplied, i.e.
    _OP_KEY_SCOPE = "common_dir" is wired) AND the resolved sender worktree ==
    the resolved receiver repo path. Constructed here by registering the SAME
    git repo as both the receiver (via the machine-local registry) and the
    sender (via repo_root pointing at that repo's .git common dir) — this is
    the only way to make _sender_worktree.resolve() == receiver_repo_path.resolve()
    inside the existing test harness (_sender_worktree is derived from
    main_worktree_root(Path(repo_root)); receiver_repo_path is registry-derived).
    """

    def test_own_inbox_refusal(self, tmp_path, monkeypatch, caplog):
        """sender worktree == receiver repo path → fail-loud setup-error, no write.

        The frozen wire envelope does not carry a reason/error field (setup-error
        message is logged daemon-side, not echoed on the wire — see
        build_setup_error_result docstring) — assert the refusal reason via caplog,
        not via the returned dict.
        """
        same_repo = _make_receiver_git_repo(tmp_path, "self-repo")
        claude_home = _make_claude_home(tmp_path, {"self_repo": same_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = str(same_repo / ".git")

        with caplog.at_level("ERROR"):
            result = _run(_memo_send(
                _base_params(to="self-repo-em", dry_run=False, topic="own-inbox-test"),
                repo_root=common_dir,
            ))

        assert result["exit_code"] == 1, f"own-inbox send must be a setup-error: {result}"
        assert "own-inbox" in caplog.text.lower(), (
            f"logged setup-error must mention own-inbox refusal: {caplog.text}"
        )

        # No file written to the (own) inbox.
        inbox = same_repo / "cross-repo" / "inbox"
        written = [f for f in inbox.iterdir() if f.name != ".gitkeep"]
        assert written == [], f"own-inbox refusal must not write any file; found: {written}"

    def test_own_inbox_refusal_dry_run_also_refused(self, tmp_path, monkeypatch):
        """dry_run preview against your own inbox is also refused (not act-only)."""
        same_repo = _make_receiver_git_repo(tmp_path, "self-repo-dry")
        claude_home = _make_claude_home(tmp_path, {"self_repo_dry": same_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = str(same_repo / ".git")

        result = _run(_memo_send(
            _base_params(to="self-repo-dry-em", dry_run=True, topic="own-inbox-dry-test"),
            repo_root=common_dir,
        ))

        assert result["exit_code"] == 1, f"own-inbox dry_run must also be a setup-error: {result}"


# ===========================================================================
# 12. Frontmatter self-validation (invariant b)
# ===========================================================================

class TestFrontmatterSelfValidation:
    """_compose_memo self-validates required frontmatter fields before composing.

    Defense-in-depth: the engine bypasses the session-side PreToolUse Write hook
    that would otherwise validate outgoing memo frontmatter, so memo.send must
    self-enforce the required-field shape (title/from/to/created/delivery_mode/
    summary non-empty; status literally 'open') before every write.
    """

    def test_compose_missing_required_field_fails_loud(self):
        """Missing/empty required field (title) → ValueError, not a silently invalid memo."""
        with pytest.raises(ValueError, match="self-validation"):
            _compose_memo(
                from_id=_ENGINE_ACTOR_ID,
                to="example-retrieval-repo-em",
                topic="missing-title-test",
                title="",  # required field, empty → must fail loud
                body="Body text.",
                kind="fyi",
                summary=None,
                supersedes=None,
                today="2026-07-17",
            )

    def test_compose_missing_to_fails_loud(self):
        """Empty 'to' also trips the self-validation guard (not just title)."""
        with pytest.raises(ValueError, match="self-validation"):
            _compose_memo(
                from_id=_ENGINE_ACTOR_ID,
                to="",
                topic="missing-to-test",
                title="Some Title",
                body="Body text.",
                kind="fyi",
                summary=None,
                supersedes=None,
                today="2026-07-17",
            )

    def test_compose_missing_summary_key_fails_loud(self):
        """summary=None (the field itself absent, distinct from empty-string) also fails loud.

        _self_validate_frontmatter_fields requires the summary KEY be present (may be
        empty-string) but not None — _compose_memo derives a non-None summary from the
        body before self-validation runs whenever body is non-empty, so this exercises
        the boundary directly via the private validator rather than through _compose_memo
        (which cannot reach a None summary once body-derivation has run).
        """
        from coordinator_core.ops.fleet.memo_send import _self_validate_frontmatter_fields

        errors = _self_validate_frontmatter_fields(
            title="Some Title",
            from_id=_ENGINE_ACTOR_ID,
            to="example-retrieval-repo-em",
            created="2026-07-17",
            status="open",
            delivery_mode="receiver-repo",
            summary=None,
            kind="fyi",
        )
        assert any("summary" in e for e in errors), (
            f"missing summary key must be flagged; got errors: {errors}"
        )

    def test_new_send_required_gate_does_not_retroactively_invalidate_existing_corpus(
        self, tmp_path, monkeypatch,
    ):
        """AC1 (c) / DEC-1: an existing on-disk memo lacking kind/summary is
        UNAFFECTED by the new send-time required gate — DEC-1 is a SEND-TIME
        gate only, not a schema-`required` change, so pre-existing memos in
        the receiver's inbox that predate this gate still sit there
        untouched and still get read correctly by collision detection (the
        one place memo.send itself re-reads prior on-disk memos)."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Hand-write a pre-existing memo lacking kind:/summary: entirely —
        # simulates the historical corpus that predates DEC-1's gate.
        inbox = receiver_repo / "cross-repo" / "inbox"
        legacy_memo = inbox / "2026-07-01-legacy-sender-legacy-topic.md"
        legacy_memo.write_text(
            "---\n"
            'title: "Legacy Memo"\n'
            'from: "legacy-sender"\n'
            'to: "example-retrieval-repo-em"\n'
            "created: 2026-07-01\n"
            "status: open\n"
            'delivery_mode: "receiver-repo"\n'
            "---\n"
            "\nLegacy body with no kind/summary fields.\n",
            encoding="utf-8",
        )

        # A NEW send (post-DEC-1) to a DIFFERENT topic must still succeed —
        # the legacy file's presence (and its missing fields) does not
        # trip up memo.send's own inbox read/collision-check machinery.
        result = _run(_memo_send(
            _base_params(dry_run=False, topic="post-gate-topic")
        ))
        assert result["exit_code"] == 0, (
            f"a legacy memo lacking kind/summary must not break a new send: {result}"
        )
        assert legacy_memo.exists(), "the legacy memo must be left untouched"
        legacy_split = split_frontmatter(legacy_memo.read_text(encoding="utf-8"))
        assert legacy_split is not None
        assert "kind" not in legacy_split.fm_text, (
            "the legacy memo's frontmatter must not be retroactively rewritten "
            "with kind/summary"
        )
        assert "summary" not in legacy_split.fm_text


# ===========================================================================
# 13. End-to-end YAML/schema validation of the REAL composed output (finding 2)
# ===========================================================================

class TestComposedMemoYamlSchemaValid:
    """Parse claude-klabauter's REAL _compose_memo output as YAML and assert schema shape.

    Closes the residual conformance gap the example-doctrine-repo round-trip fixture cannot catch:
    the fixture SIMULATES the claude-klabauter write shape at most sites rather than driving
    claude-klabauter's own op, and test_act_written_memo_has_schema_valid_frontmatter (above)
    only asserts via substrings ('status: open' in content) — neither actually
    parses claude-klabauter's real emission with a YAML parser. These two tests take
    _compose_memo's real output (once directly, once via a live `act` call that
    writes to disk) and yaml.safe_load() the split frontmatter block — the only
    place in the suite that structurally certifies claude-klabauter's actual emission shape.

    Review: the Staff Engineer — finding 2 (the most important note): nothing currently runs
    claude-klabauter's real composed emission through a structural schema check.
    """

    _REQUIRED_KEYS = {
        "title", "from", "to", "created", "status", "delivery_mode", "summary", "kind",
    }

    def _assert_schema_valid(self, frontmatter: dict) -> None:
        """Structural assertions mirroring the D2 criterion 6 / D2-6 schema contract."""
        assert isinstance(frontmatter, dict), (
            f"frontmatter must parse to a YAML mapping, got: {type(frontmatter)}"
        )
        missing = self._REQUIRED_KEYS - frontmatter.keys()
        assert not missing, f"frontmatter missing required keys: {missing}"
        assert frontmatter["status"] == "open", (
            f"status must be 'open', got: {frontmatter['status']!r}"
        )
        assert frontmatter["delivery_mode"] == "receiver-repo", (
            f"delivery_mode must be 'receiver-repo', got: {frontmatter['delivery_mode']!r}"
        )
        for field in ("to", "from", "title", "created"):
            assert frontmatter[field], f"{field!r} must be non-empty, got: {frontmatter[field]!r}"
        assert frontmatter["kind"] in _VALID_KINDS, (
            f"kind {frontmatter['kind']!r} not in valid enum {_VALID_KINDS}"
        )

    def test_compose_memo_output_parses_as_valid_yaml_frontmatter(self):
        """_compose_memo's real output round-trips through yaml.safe_load with correct shape."""
        content = _compose_memo(
            from_id="sender-repo-alpha",
            to="example-retrieval-repo-em",
            topic="yaml-schema-test",
            title="YAML Schema Test",
            body="Body text for YAML schema round-trip test.",
            kind="consult",
            summary=None,
            supersedes=None,
            today="2026-07-17",
        )

        split = split_frontmatter(content)
        assert split is not None, "composed memo must have a parseable frontmatter block"

        frontmatter = yaml.safe_load(split.fm_text)
        self._assert_schema_valid(frontmatter)
        assert frontmatter["to"] == "example-retrieval-repo-em"
        assert frontmatter["from"] == "sender-repo-alpha"
        assert frontmatter["kind"] == "consult"

        body = split.body_with_leading_newline.lstrip("\n")
        assert "Body text for YAML schema round-trip test." in body

    def test_act_written_memo_parses_as_valid_yaml_frontmatter(self, tmp_path, monkeypatch):
        """A live act call's on-disk memo — claude-klabauter's REAL emission, not a simulated fixture —
        parses via yaml.safe_load and structurally validates against the schema contract
        (required keys, status:open, delivery_mode:receiver-repo, kind enum, non-empty
        to/from/title/created, body follows the closing '---')."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(
            _base_params(dry_run=False, topic="yaml-e2e-test", kind="proposal")
        ))
        assert result["exit_code"] == 0

        memo_path = Path(result["acted"][0]["id"])
        content = memo_path.read_text(encoding="utf-8")

        split = split_frontmatter(content)
        assert split is not None, "written memo must have a parseable frontmatter block"

        frontmatter = yaml.safe_load(split.fm_text)
        self._assert_schema_valid(frontmatter)
        assert frontmatter["kind"] == "proposal"

        body = split.body_with_leading_newline.lstrip("\n")
        assert body.strip(), "body must follow the closing '---'"
        assert "This is a test memo body." in body


# ===========================================================================
# 14. Central receiver id resolution (fan-in to a single registered key)
# ===========================================================================

class TestCentralReceiverResolution:
    """All identity.centralReceiverIds aliases fan in to the ONE registered central key.

    Bug: the plain convention fallback in _receiver_em_to_repo_key mapped each
    central id independently ('claude-central-em' -> 'repos.claude_central',
    'central-em' -> 'repos.central', 'central' -> 'repos.central',
    'example-doctrine-repo-em' -> 'repos.example_doctrine_repo') — only 'example-doctrine-repo-em' happened to
    match the machine-local registry's actual central entry (repos.example_doctrine_repo),
    so sending to the canonical alias 'claude-central-em' silently refused
    (exit_code:1) even though central IS registered under a different id.

    Fix: _resolve_receiver_inbox resolves ANY central id to the single
    registered central key by scanning centralReceiverIds against the
    registered repos (no hardcoded 'repos.example_doctrine_repo' literal).
    """

    def test_claude_central_em_resolves_to_example_doctrine_repo_path(self, tmp_path, monkeypatch):
        """claude-central-em (the canonical claude-klabauter-side central alias) resolves — was broken."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="claude-central-em", dry_run=False, topic="central-fanin-test")
        ))

        assert result["exit_code"] == 0, (
            f"claude-central-em must resolve to the registered central repo: {result}"
        )
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists()
        assert memo_path.parent.parent == example_doctrine_repo_repo / "cross-repo"

    def test_central_em_resolves_to_example_doctrine_repo_path(self, tmp_path, monkeypatch):
        """central-em fans in to the same registered central key."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="central-em", dry_run=False, topic="central-em-fanin-test")
        ))

        assert result["exit_code"] == 0, f"central-em must resolve to registered central: {result}"
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists()
        assert memo_path.parent.parent == example_doctrine_repo_repo / "cross-repo"

    def test_bare_central_resolves_to_example_doctrine_repo_path(self, tmp_path, monkeypatch):
        """'central' (bare, no -em suffix) fans in to the same registered central key."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="central", dry_run=False, topic="central-bare-fanin-test")
        ))

        assert result["exit_code"] == 0, f"'central' must resolve to registered central: {result}"
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists()
        assert memo_path.parent.parent == example_doctrine_repo_repo / "cross-repo"

    def test_example_doctrine_repo_em_still_resolves_regression_guard(self, tmp_path, monkeypatch):
        """example-doctrine-repo-em (the pre-fix working case) keeps working after the fan-in fix."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="example-doctrine-repo-em", dry_run=False, topic="example-doctrine-repo-em-regression")
        ))

        assert result["exit_code"] == 0, f"example-doctrine-repo-em must still resolve: {result}"
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists()
        assert memo_path.parent.parent == example_doctrine_repo_repo / "cross-repo"

    def test_non_central_receiver_unchanged(self, tmp_path, monkeypatch):
        """A non-central receiver (example-retrieval-repo-em) resolves to its own repo, unaffected."""
        rag_repo = _make_receiver_git_repo(tmp_path, "rag-repo")
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(
            tmp_path, {"example_retrieval_repo": rag_repo, "example_doctrine_repo": example_doctrine_repo_repo}
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="example-retrieval-repo-em", dry_run=False, topic="non-central-unchanged")
        ))

        assert result["exit_code"] == 0
        memo_path = Path(result["acted"][0]["id"])
        assert memo_path.exists()
        assert memo_path.parent.parent == rag_repo / "cross-repo"

    def test_central_id_present_but_no_central_key_registered_graceful_setup_error(
        self, tmp_path, monkeypatch
    ):
        """Central id in manifest, but NO central repo registered anywhere → graceful exit_code:1.

        Must not crash/traceback — _resolve_receiver_inbox falls through to the
        (None, None, all_repos) graceful return when no central id resolves to a
        registered key, and _memo_send returns the central-specific setup-error envelope.
        """
        # Only an unrelated repo is registered — no repos.example_doctrine_repo / repos.central / etc.
        other_repo = _make_receiver_git_repo(tmp_path, "unrelated-repo")
        claude_home = _make_claude_home(tmp_path, {"unrelated": other_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="claude-central-em", dry_run=False, topic="central-unregistered")
        ))

        assert result["exit_code"] == 1, (
            f"unregistered central receiver must be a graceful setup-error, not a crash: {result}"
        )
        assert result["mode"] == _MODE

    def test_resolve_receiver_inbox_central_fanin_direct_unit(self, tmp_path, monkeypatch):
        """Direct unit test of _resolve_receiver_inbox's central fan-in resolution."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        for central_id in ("claude-central-em", "central-em", "central", "example-doctrine-repo-em"):
            inbox_dir, receiver_repo_path, all_repos = _resolve_receiver_inbox(central_id)
            assert inbox_dir is not None, f"{central_id!r} must resolve an inbox"
            assert receiver_repo_path == example_doctrine_repo_repo, (
                f"{central_id!r} must resolve to the registered example_doctrine_repo repo path; "
                f"got: {receiver_repo_path}"
            )

    def test_read_central_receiver_ids_reads_manifest(self, tmp_path, monkeypatch):
        """_read_central_receiver_ids reads identity.centralReceiverIds and lowercases them."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [], central_ids=["Claude-Central-EM", "central"])

        result = _read_central_receiver_ids()
        assert result == {"claude-central-em", "central"}


# ===========================================================================
# 15. Canonical receiver-id stamping — the `to:` addressee gate
#
# Problem this closes: `_compose_memo` stamped `to:` verbatim from whatever
# central/redirect alias the caller typed (`claude-central-em`, `central-em`,
# `central`, `example-doctrine-repo-em`, `.claude-em`, `claude-home`, `coordinator-claude`,
# `coordinator-claude-em` all address the SAME example-doctrine-repo seat) — a reader could not
# verify by inspection that two differently-addressed memos landed at the
# same receiver. Fix: _memo_resolver.canonical_receiver_id() derives the ONE
# repo-matching central id, and memo_send.py stamps THAT into `to:` instead
# of the caller's literal string.
# ===========================================================================

class TestCanonicalReceiverIdStamping:
    def _read_to_field(self, memo_path: Path) -> str:
        content = memo_path.read_text(encoding="utf-8")
        match = re.search(r'^to:\s*"((?:[^"\\]|\\.)*)"', content, re.MULTILINE)
        assert match, f"no to: frontmatter field found in {memo_path}"
        return match.group(1)

    @pytest.mark.parametrize(
        "alias",
        ["claude-central-em", "central-em", "central", "example-doctrine-repo-em"],
    )
    def test_every_central_alias_stamps_same_canonical_to(
        self, tmp_path, monkeypatch, alias
    ):
        """Every central alias resolves to the SAME registered repo AND stamps
        the SAME canonical `to:` value — one seat, one name in the corpus."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to=alias, dry_run=False, topic=f"canon-{alias}")
        ))

        assert result["exit_code"] == 0, result
        memo_path = Path(result["acted"][0]["id"])
        stamped_to = self._read_to_field(memo_path)
        assert stamped_to == "example-doctrine-repo-em", (
            f"alias {alias!r} must stamp the repo-matching canonical id "
            f"'example-doctrine-repo-em', got {stamped_to!r}"
        )
        assert result["acted"][0]["receiver"] == "example-doctrine-repo-em"

    @pytest.mark.parametrize(
        "redirect_alias",
        [".claude-em", "claude-home", "coordinator-claude", "coordinator-claude-em"],
    )
    def test_redirect_alias_canonicalizes_to_same_central_id(
        self, tmp_path, monkeypatch, redirect_alias
    ):
        """canonical_receiver_id() alone: a redirect alias (identity.redirectAliases)
        canonicalizes to the SAME central id `claude-central-em` does — even
        though resolve_receiver_inbox's send-resolution path does not itself
        route redirect aliases to a registered repo (a separate, pre-existing
        concern this fix does not touch — memo.check_addressee owns that
        redirect-MATCH path per _memo_resolver's module docstring)."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(
            tmp_path, claude_home, [],
            redirect_aliases=[".claude-em", "claude-home", "coordinator-claude", "coordinator-claude-em"],
        )

        assert _canonical_receiver_id(redirect_alias) == "example-doctrine-repo-em"
        assert _canonical_receiver_id("claude-central-em") == "example-doctrine-repo-em"

    def test_non_central_receiver_stamped_unchanged(self, tmp_path, monkeypatch):
        """A non-central receiver (example-retrieval-repo-em) is stamped as-is — no rewrite."""
        receiver_repo = _make_receiver_git_repo(tmp_path, "rag-repo")
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        result = _run(_memo_send(
            _base_params(to="example-retrieval-repo-em", dry_run=False, topic="non-central-stamp")
        ))

        assert result["exit_code"] == 0, result
        memo_path = Path(result["acted"][0]["id"])
        assert self._read_to_field(memo_path) == "example-retrieval-repo-em"

    def test_manifest_absent_canonicalization_is_passthrough(self, tmp_path, monkeypatch):
        """No .doe-root/manifest at all → canonicalization is a no-op passthrough;
        send still succeeds and stamps the caller's literal (lowercased) `to`."""
        receiver_repo = _make_receiver_git_repo(tmp_path, "rag-repo")
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # Deliberately no _make_doe_manifest call — no .doe-root sentinel at all.

        result = _run(_memo_send(
            _base_params(to="example-retrieval-repo-em", dry_run=False, topic="no-manifest-passthrough")
        ))

        assert result["exit_code"] == 0, result
        memo_path = Path(result["acted"][0]["id"])
        assert self._read_to_field(memo_path) == "example-retrieval-repo-em"

    def test_dry_run_preview_and_actual_write_agree_on_canonical_to(
        self, tmp_path, monkeypatch
    ):
        """A dry-run preview's `receiver` field must equal the `to:` the act
        path actually stamps — same canonicalization, same value, both paths."""
        example_doctrine_repo_repo = _make_receiver_git_repo(tmp_path, "example-doctrine-repo-repo")
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": example_doctrine_repo_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _make_doe_manifest(tmp_path, claude_home, [])

        preview = _run(_memo_send(
            _base_params(to="claude-central-em", dry_run=True, topic="preview-agree-test")
        ))
        assert preview["exit_code"] == 0, preview
        previewed_receiver = preview["candidates"][0]["receiver"]

        result = _run(_memo_send(
            _base_params(to="claude-central-em", dry_run=False, topic="preview-agree-test")
        ))
        assert result["exit_code"] == 0, result
        memo_path = Path(result["acted"][0]["id"])
        stamped_to = self._read_to_field(memo_path)

        assert previewed_receiver == stamped_to == "example-doctrine-repo-em"

    def test_read_central_receiver_ids_graceful_degradation(self, tmp_path, monkeypatch):
        """No .doe-root/manifest → _read_central_receiver_ids returns empty set, no raise."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # Deliberately do NOT write .doe-root or any manifest.

        result = _read_central_receiver_ids()
        assert result == set()


class TestResolveSenderIdDegradation:
    """DEC-4: on RegistryReadError/AmbiguousReceiverError, resolve_sender_id
    logs a visible warning naming both the raw id and the underlying error,
    then returns the raw id — it must never raise (fail-open is deliberate;
    sender-slug canonicalization is filename namespacing, not the addressee
    gate)."""

    @pytest.mark.parametrize(
        "exc",
        [
            RegistryReadError("manifest unreadable"),
            AmbiguousReceiverError("central-em", ["repos.a", "repos.b"]),
        ],
    )
    def test_degradation_warns_and_returns_raw(self, monkeypatch, caplog, exc):
        def _raise(_raw):
            raise exc

        monkeypatch.setattr(
            "coordinator_core.ops.fleet.memo_send._canonical_receiver_id", _raise
        )

        with caplog.at_level("WARNING"):
            result = resolve_sender_id("some-raw-sender-id")

        assert result == "some-raw-sender-id"
        assert "some-raw-sender-id" in caplog.text, (
            f"warning must name the raw id it fell back to: {caplog.text}"
        )
        assert str(exc) in caplog.text, (
            f"warning must name the underlying error: {caplog.text}"
        )


# ===========================================================================
# 16. DEC-3/C7 — 1->N fan-out (`to` as a list): shared campaign_id, N
#     independent per-receiver writes, best-effort fail-loud-per-receiver.
# ===========================================================================

class TestFanOutCampaign:
    """A list-shaped `to` routes through `_memo_send_fan_out`, which iterates
    the SAME single-receiver `_memo_send` path once per receiver. Covers:
    dry_run preview across N receivers, an all-succeed act, a
    partial-failure act (one bad receiver does not block the rest), an
    explicit campaign_id, and the four setup-error shapes (empty list,
    duplicate receivers, non-string entry, bad dry_run type) — plus a
    negative-spec check that the plain single-receiver envelope is
    untouched by this addition (no manifest/campaign_id key leak)."""

    def test_dry_run_previews_all_receivers_with_shared_campaign_id(self, tmp_path, monkeypatch):
        receiver_a = _make_receiver_git_repo(tmp_path, "receiver-a")
        receiver_b = _make_receiver_git_repo(tmp_path, "receiver-b")
        claude_home = _make_claude_home(
            tmp_path, {"receiver_a": receiver_a, "receiver_b": receiver_b}
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=True, topic="campaign-dry-run")
        params["to"] = ["receiver-a-em", "receiver-b-em"]
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0, result
        assert result["dry_run"] is True
        assert result["campaign_id"]
        assert len(result["candidates"]) == 2
        manifest = result["manifest"]
        assert {m["receiver"] for m in manifest} == {"receiver-a-em", "receiver-b-em"}
        assert all(m["outcome"] == "previewed" for m in manifest)
        assert all(m["error"] is None for m in manifest)
        assert all(m["campaign_id"] == result["campaign_id"] for m in manifest)
        # dry-run must not write anything.
        assert list((receiver_a / "cross-repo" / "inbox").glob("*.md")) == []

    def test_act_writes_all_receivers_with_persisted_campaign_id(self, tmp_path, monkeypatch):
        receiver_a = _make_receiver_git_repo(tmp_path, "receiver-a")
        receiver_b = _make_receiver_git_repo(tmp_path, "receiver-b")
        claude_home = _make_claude_home(
            tmp_path, {"receiver_a": receiver_a, "receiver_b": receiver_b}
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=False, topic="campaign-act")
        params["to"] = ["receiver-a-em", "receiver-b-em"]
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0, result
        campaign_id = result["campaign_id"]
        assert campaign_id
        assert len(result["acted"]) == 2
        manifest = result["manifest"]
        assert len(manifest) == 2
        assert all(m["outcome"] == "delivered" for m in manifest)
        assert all(m["error"] is None for m in manifest)

        # campaign_id MUST be persisted to disk on every successful write —
        # DEC-3: the rag compliance query's soundness depends on this being
        # a queryable on-disk field, not transient send-time output only.
        for entry in result["acted"]:
            memo_path = Path(entry["id"])
            split = split_frontmatter(memo_path.read_text(encoding="utf-8"))
            assert split is not None
            fm = yaml.safe_load(split.fm_text)
            assert fm.get("campaign_id") == campaign_id, (
                f"campaign_id must be persisted on-disk for every successful "
                f"receiver write: {fm}"
            )

    def test_partial_failure_does_not_abort_remaining_receivers(self, tmp_path, monkeypatch):
        receiver_a = _make_receiver_git_repo(tmp_path, "receiver-a")
        claude_home = _make_claude_home(tmp_path, {"receiver_a": receiver_a})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=False, topic="campaign-partial-fail")
        params["to"] = ["receiver-a-em", "unregistered-em"]
        result = _run(_memo_send(params))

        # DETERMINATE-PARTIAL — at least one receiver failed, at least one
        # other succeeded; the fan-out attempted BOTH (K's failure did not
        # abort K+1).
        assert result["exit_code"] == 2, result
        assert len(result["acted"]) == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "unregistered-em"
        assert result["failed"][0]["reason"]

        manifest_by_receiver = {m["receiver"]: m for m in result["manifest"]}
        assert manifest_by_receiver["receiver-a-em"]["outcome"] == "delivered"
        assert manifest_by_receiver["receiver-a-em"]["error"] is None
        assert manifest_by_receiver["unregistered-em"]["outcome"] == "error"
        assert manifest_by_receiver["unregistered-em"]["error"], (
            "a per-receiver setup-error refusal must still surface a non-empty "
            "manifest error (harvested from the single-receiver path's stderr "
            "diagnostic — the frozen setup-error envelope itself carries no "
            "top-level 'reason' field)"
        )

        campaign_id = result["campaign_id"]
        assert manifest_by_receiver["receiver-a-em"]["campaign_id"] == campaign_id
        assert manifest_by_receiver["unregistered-em"]["campaign_id"] == campaign_id

        memo_path = Path(result["acted"][0]["id"])
        split = split_frontmatter(memo_path.read_text(encoding="utf-8"))
        fm = yaml.safe_load(split.fm_text)
        assert fm.get("campaign_id") == campaign_id

    def test_explicit_campaign_id_is_honored_and_persisted(self, tmp_path, monkeypatch):
        receiver_a = _make_receiver_git_repo(tmp_path, "receiver-a")
        claude_home = _make_claude_home(tmp_path, {"receiver_a": receiver_a})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=False, topic="campaign-explicit-id")
        params["to"] = ["receiver-a-em"]
        params["campaign_id"] = "my-explicit-campaign-id"
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0, result
        assert result["campaign_id"] == "my-explicit-campaign-id"
        memo_path = Path(result["acted"][0]["id"])
        split = split_frontmatter(memo_path.read_text(encoding="utf-8"))
        fm = yaml.safe_load(split.fm_text)
        assert fm.get("campaign_id") == "my-explicit-campaign-id"

    def test_empty_to_list_is_setup_error(self):
        params = _base_params(dry_run=True)
        params["to"] = []
        result = _run(_memo_send(params))
        assert result["exit_code"] == 1

    def test_duplicate_receivers_is_setup_error(self):
        params = _base_params(dry_run=True)
        params["to"] = ["same-em", "same-em"]
        result = _run(_memo_send(params))
        assert result["exit_code"] == 1

    def test_non_string_entry_is_setup_error(self):
        params = _base_params(dry_run=True)
        params["to"] = ["good-em", 123]
        result = _run(_memo_send(params))
        assert result["exit_code"] == 1

    def test_bad_dry_run_type_is_setup_error_on_fan_out_path_too(self):
        params = _base_params(dry_run=True)
        params["to"] = ["a-em", "b-em"]
        params["dry_run"] = "not-a-bool"
        result = _run(_memo_send(params))
        assert result["exit_code"] == 1

    def test_single_receiver_string_path_envelope_unaffected(self, tmp_path, monkeypatch):
        """A plain string `to` (the pre-existing shape) never touches the
        fan-out envelope — no campaign_id/manifest keys leak into the
        ordinary single-receiver envelope."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="single-unaffected")))
        assert result["exit_code"] == 0
        assert "manifest" not in result
        assert "campaign_id" not in result

    def test_fan_out_shares_one_today_across_receivers_despite_midnight_straddle(
        self, tmp_path, monkeypatch
    ):
        """Review: code-reviewer (Finding 3) — `_memo_send_fan_out` must
        capture ONE `datetime.date.today()` and thread it into every
        per-receiver `_memo_send` call, so a fan-out loop that straddles
        local midnight does not let receivers land on different
        filename/`created:` dates under the same campaign_id. Fakes
        `datetime.date.today()` to return a NEW date on every call — if the
        fix regressed (each receiver computing its own today()), the two
        receivers would disagree; sharing collapses both onto the single
        value captured once by the fan-out loop."""
        receiver_a = _make_receiver_git_repo(tmp_path, "receiver-a")
        receiver_b = _make_receiver_git_repo(tmp_path, "receiver-b")
        claude_home = _make_claude_home(
            tmp_path, {"receiver_a": receiver_a, "receiver_b": receiver_b}
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        real_today = datetime.date.today()
        advancing_dates = iter(
            real_today + datetime.timedelta(days=offset) for offset in range(10)
        )

        class _AdvancingDate(datetime.date):
            @classmethod
            def today(cls):
                return next(advancing_dates)

        monkeypatch.setattr(memo_send_module.datetime, "date", _AdvancingDate)

        params = _base_params(dry_run=False, topic="campaign-midnight")
        params["to"] = ["receiver-a-em", "receiver-b-em"]
        result = _run(_memo_send(params))

        assert result["exit_code"] == 0, result
        assert len(result["acted"]) == 2
        # Every call to the faked today() advances by one day — including
        # the ONE call `_generate_campaign_id` makes before the fan-out's own
        # `today` capture — so this asserts on SHARED-ness (both receivers'
        # filenames carry the identical date prefix), not on any specific
        # calendar date: a regression (each receiver computing its own
        # today()) would consume a fresh date per receiver and the two
        # filenames would disagree.
        filenames = [Path(entry["id"]).name for entry in result["acted"]]
        date_prefixes = {name[:10] for name in filenames}
        assert len(date_prefixes) == 1, (
            f"both receivers must share the ONE today() the fan-out loop "
            f"captured — got {filenames}"
        )


# ===========================================================================
# in_reply_to — write-side support (2026-07-25)
# ===========================================================================

class TestInReplyToNormalization:
    """_normalize_in_reply_to / _validate_send_params — basename normalization."""

    def test_bare_basename_passes_through_unchanged(self):
        assert _normalize_in_reply_to("2026-07-25-foo.md") == "2026-07-25-foo.md"

    def test_path_normalizes_to_basename(self):
        assert _normalize_in_reply_to("cross-repo/inbox/2026-07-25-foo.md") == "2026-07-25-foo.md"

    def test_absolute_path_normalizes_to_basename(self):
        assert _normalize_in_reply_to("/tmp/x/cross-repo/archive/2026-07-25-foo.md") == "2026-07-25-foo.md"

    def test_validate_send_params_normalizes_in_reply_to(self):
        params = _base_params(dry_run=True)
        params["in_reply_to"] = "cross-repo/inbox/2026-07-25-foo.md"
        result = _validate_send_params(params)
        assert isinstance(result, SendParams)
        assert result.in_reply_to == "2026-07-25-foo.md"

    def test_omitted_in_reply_to_is_none(self):
        result = _validate_send_params(_base_params(dry_run=True))
        assert isinstance(result, SendParams)
        assert result.in_reply_to is None

    def test_empty_string_in_reply_to_rejected(self):
        params = _base_params(dry_run=True)
        params["in_reply_to"] = ""
        result = _validate_send_params(params)
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_known_param_keys_includes_in_reply_to(self):
        assert "in_reply_to" in _KNOWN_PARAM_KEYS


class TestInReplyToExistenceGate:
    """_validate_in_reply_to_exists / end-to-end act path — the send-time
    fail-loud gate: in_reply_to must name a memo THIS repo's own
    cross-repo/inbox/ or cross-repo/archive/ (recursive) actually holds."""

    def test_unit_missing_sender_worktree_fails_loud(self):
        result = _validate_in_reply_to_exists(True, None, "2026-07-25-foo.md")
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_unit_found_in_inbox_passes(self, tmp_path):
        sender = tmp_path / "sender"
        (sender / "cross-repo" / "inbox").mkdir(parents=True)
        (sender / "cross-repo" / "inbox" / "2026-07-25-foo.md").write_text("x", encoding="utf-8")
        result = _validate_in_reply_to_exists(True, sender, "2026-07-25-foo.md")
        assert result is None

    def test_unit_found_in_nested_archive_passes(self, tmp_path):
        sender = tmp_path / "sender"
        (sender / "cross-repo" / "archive" / "2026-07").mkdir(parents=True)
        (sender / "cross-repo" / "archive" / "2026-07" / "2026-07-25-foo.md").write_text(
            "x", encoding="utf-8",
        )
        result = _validate_in_reply_to_exists(True, sender, "2026-07-25-foo.md")
        assert result is None

    def test_unit_not_found_fails_loud(self, tmp_path):
        sender = tmp_path / "sender"
        sender.mkdir()
        result = _validate_in_reply_to_exists(True, sender, "2026-07-25-does-not-exist.md")
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_act_omitted_emits_no_in_reply_to_key(self, tmp_path, monkeypatch):
        """No in_reply_to supplied — the delivered memo carries no such key at all."""
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(_base_params(dry_run=False, topic="no-in-reply-to")))
        assert result["exit_code"] == 0
        content = Path(result["acted"][0]["id"]).read_text(encoding="utf-8")
        assert "in_reply_to" not in content

    def test_act_unresolvable_in_reply_to_fails_loud_writes_nothing(self, tmp_path, monkeypatch):
        """An in_reply_to that matches nothing in the sender's own tree refuses
        BEFORE anything is written to the receiver's inbox."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        inbox_before = sorted((receiver_repo / "cross-repo" / "inbox").iterdir())

        params = _base_params(dry_run=False, topic="typo-in-reply-to")
        params["in_reply_to"] = "2026-07-25-never-existed.md"
        result = _run(_memo_send(params, repo_root=str(sender_repo / ".git")))

        assert result["exit_code"] == 1
        inbox_after = sorted((receiver_repo / "cross-repo" / "inbox").iterdir())
        assert inbox_after == inbox_before, "nothing should be written to the receiver tree"

    def test_act_valid_in_reply_to_emits_normalized_basename(self, tmp_path, monkeypatch):
        """A caller-supplied PATH (not a bare basename) still emits as a bare
        basename in the delivered frontmatter."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        inbound_dir = sender_repo / "cross-repo" / "inbox"
        inbound_dir.mkdir(parents=True, exist_ok=True)
        (inbound_dir / "2026-07-25-inbound.md").write_text("x", encoding="utf-8")

        params = _base_params(dry_run=False, topic="in-reply-to-path-form")
        params["in_reply_to"] = "cross-repo/inbox/2026-07-25-inbound.md"
        result = _run(_memo_send(params, repo_root=str(sender_repo / ".git")))

        assert result["exit_code"] == 0, result
        content = Path(result["acted"][0]["id"]).read_text(encoding="utf-8")
        assert 'in_reply_to: "2026-07-25-inbound.md"' in content


class TestInReplyToEndToEndReplyClosure:
    """The actual point of the feature: a memo sent with in_reply_to is
    subsequently classified `evidenced` (not `open`) by
    coordinator_core.pickup_assemble.compute_reply_closure for the inbound
    memo it names."""

    def test_end_to_end_in_reply_to_makes_reply_closure_evidenced(self, tmp_path, monkeypatch):
        # "their" repo — the ORIGINAL sender of the inbound memo we're
        # replying to, and the RECEIVER of our reply send.
        their_repo = _make_receiver_git_repo(tmp_path, "their-repo")
        claude_home = _make_claude_home(tmp_path, {"their_repo": their_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # "our" repo — where the reply is sent FROM and where
        # compute_reply_closure is run from afterwards. Does not need to be a
        # real git repo — main_worktree_root is pure path arithmetic
        # (common_dir.parent), and compute_reply_closure only reads its
        # `.name` (for the self_em_id convention) plus disk contents.
        our_repo = tmp_path / "our-repo"
        our_repo.mkdir()
        inbound_basename = "2026-07-20-their-repo-em-topic-consult.md"
        inbound_dir = our_repo / "cross-repo" / "inbox"
        inbound_dir.mkdir(parents=True)
        (inbound_dir / inbound_basename).write_text(
            '---\nfrom: "their-repo-em"\nto: "our-repo-em"\ncreated: 2026-07-20\n'
            'status: open\nkind: consult\nsummary: "An ask."\n---\n\nPlease advise.\n',
            encoding="utf-8",
        )

        result = _run(_memo_send(
            {
                "dry_run": False,
                "topic": "reply-to-their-ask",
                "to": "their-repo-em",
                "title": "Our reply",
                "body": "Here is our reply to your ask.",
                "kind": "consult",
                "summary": "Our reply summary.",
                "from_id": "our-repo-em",
                "in_reply_to": inbound_basename,
            },
            repo_root=str(our_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result

        closure = pickup_assemble.compute_reply_closure(
            {"kind": "consult", "from": "their-repo-em", "created": "2026-07-20"},
            inbound_basename,
            our_repo,
        )
        assert closure["verdict"] == "evidenced", closure
        assert closure["candidates"], closure


# ===========================================================================
# space: / supersedes: — inbox-blitz write-side support (2026-07-28)
# Spec: cross-repo/inbox/2026-07-28-example-retrieval-repo-em-inbox-blitz-proven-pattern.md
# ===========================================================================

class TestSpaceParam:
    def test_known_param_keys_includes_space(self):
        assert "space" in _KNOWN_PARAM_KEYS

    def test_omitted_space_is_none(self):
        result = _validate_send_params(_base_params(dry_run=True))
        assert isinstance(result, SendParams)
        assert result.space is None

    def test_space_is_stripped(self):
        params = _base_params(dry_run=True)
        params["space"] = "  gate-migration  "
        result = _validate_send_params(params)
        assert isinstance(result, SendParams)
        assert result.space == "gate-migration"

    def test_blank_space_rejected(self):
        params = _base_params(dry_run=True)
        params["space"] = "   "
        result = _validate_send_params(params)
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_space_renders_into_frontmatter(self):
        content = _compose_memo(
            from_id="claude-klabauter-engine", to="example-retrieval-repo-em", topic="t", title="T",
            body="Body.", kind="fyi", summary="S.", supersedes=None,
            today="2026-07-28", space="gate-migration",
        )
        assert 'space: "gate-migration"' in content

    def test_space_absent_emits_no_key(self):
        content = _compose_memo(
            from_id="claude-klabauter-engine", to="example-retrieval-repo-em", topic="t", title="T",
            body="Body.", kind="fyi", summary="S.", supersedes=None,
            today="2026-07-28",
        )
        assert "space:" not in content


class TestSupersedesListForm:
    def test_single_element_list_collapses_to_string(self):
        # Collapsing keeps every downstream consumer (filename disambiguation,
        # frontmatter rendering) on the pre-existing shape for the common case.
        params = _base_params(dry_run=True)
        params["supersedes"] = ["2026-07-20-a-em-old.md"]
        result = _validate_send_params(params)
        assert isinstance(result, SendParams)
        assert result.supersedes == "2026-07-20-a-em-old.md"

    def test_multi_element_list_preserved(self):
        params = _base_params(dry_run=True)
        params["supersedes"] = ["2026-07-20-a.md", "  2026-07-21-b.md  "]
        result = _validate_send_params(params)
        assert isinstance(result, SendParams)
        assert result.supersedes == ["2026-07-20-a.md", "2026-07-21-b.md"]

    def test_blank_list_entry_fails_loud_rather_than_pruning(self):
        # A silently-shortened supersession list leaves a live ask looking
        # retired — the whole reason this fails instead of dropping.
        params = _base_params(dry_run=True)
        params["supersedes"] = ["2026-07-20-a.md", ""]
        result = _validate_send_params(params)
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_non_string_supersedes_rejected(self):
        params = _base_params(dry_run=True)
        params["supersedes"] = 17
        result = _validate_send_params(params)
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_list_renders_as_a_yaml_sequence(self):
        content = _compose_memo(
            from_id="claude-klabauter-engine", to="example-retrieval-repo-em", topic="t", title="T",
            body="Body.", kind="fyi", summary="S.",
            supersedes=["2026-07-20-a.md", "2026-07-21-b.md"], today="2026-07-28",
        )
        fm = content.split("---")[1]
        from coordinator_core.frontmatter.schema_validate import parse_yaml
        parsed = parse_yaml(fm)
        # Must round-trip as N references, never one _yaml_quote'd scalar.
        assert parsed["supersedes"] == ["2026-07-20-a.md", "2026-07-21-b.md"]

    def test_string_form_renders_unchanged(self):
        content = _compose_memo(
            from_id="claude-klabauter-engine", to="example-retrieval-repo-em", topic="t", title="T",
            body="Body.", kind="fyi", summary="S.",
            supersedes="2026-07-20-a.md", today="2026-07-28",
        )
        assert 'supersedes: "2026-07-20-a.md"' in content

    def test_redelivery_filename_slugs_first_reference_of_a_list(self):
        listed = _redelivery_filename(
            "2026-07-28", "example-doctrine-repo-em", "topic",
            ["2026-07-20-a.md", "2026-07-21-b.md"],
        )
        scalar = _redelivery_filename(
            "2026-07-28", "example-doctrine-repo-em", "topic", "2026-07-20-a.md",
        )
        assert listed == scalar


# ===========================================================================
# Sender-outbox sent-stamp (write-back onto the SENDER's own draft copy)
#
# Root cause: verified cross-repo/inbox finding, example-doctrine-repo-em — send
# dispatched to the receiver and removed the sender's local draft, but
# nothing ever wrote delivery evidence back onto the sender's own outbox
# copy. See memo_send._stamp_sender_outbox_sent's docstring for the
# ordering/never-raise rationale these tests pin.
# ===========================================================================

def _write_outbox_draft(sender_repo: Path, topic: str, *, to: str, title: str) -> Path:
    """Write a minimal status:draft outbox file at
    <sender_repo>/state/memo-outbox/<topic>.md, mirroring
    memo_draft.compose_draft_frontmatter's shape."""
    outbox_dir = sender_repo / "state" / "memo-outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    outbox_path = outbox_dir / f"{topic}.md"
    outbox_path.write_text(
        "---\n"
        f'title: "{title}"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        "created: 2026-01-01\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "Draft summary."\n'
        'kind: "fyi"\n'
        "---\n\nDraft body.\n",
        encoding="utf-8",
    )
    return outbox_path


class TestPortableDeliveredToForm:
    """_portable_delivered_to_form: receiver-repo-relative when possible,
    falling back to ~/-home-relative, and only then to the absolute string —
    never an absolute machine path when a relative form is available."""

    def test_under_receiver_root_is_repo_relative(self, tmp_path):
        receiver_repo = tmp_path / "receiver-repo"
        delivered = receiver_repo / "cross-repo" / "inbox" / "memo.md"
        delivered.parent.mkdir(parents=True)
        delivered.write_text("x", encoding="utf-8")
        assert _portable_delivered_to_form(receiver_repo, delivered) == (
            "cross-repo/inbox/memo.md"
        )

    def test_outside_receiver_root_falls_back_to_home_relative(
        self, tmp_path, monkeypatch,
    ):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        receiver_repo = tmp_path / "unrelated-receiver-repo"
        receiver_repo.mkdir()
        delivered = fake_home / "elsewhere" / "memo.md"
        delivered.parent.mkdir(parents=True)
        delivered.write_text("x", encoding="utf-8")
        assert _portable_delivered_to_form(receiver_repo, delivered) == (
            "~/elsewhere/memo.md"
        )

    def test_outside_both_falls_back_to_absolute_with_forward_slashes(
        self, tmp_path, monkeypatch,
    ):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        receiver_repo = tmp_path / "unrelated-receiver-repo"
        receiver_repo.mkdir()
        delivered = tmp_path / "nowhere-near-either" / "memo.md"
        delivered.parent.mkdir(parents=True)
        delivered.write_text("x", encoding="utf-8")
        result = _portable_delivered_to_form(receiver_repo, delivered)
        assert "\\" not in result
        assert result == str(delivered.resolve()).replace("\\", "/")


class TestSenderOutboxSentStamp:
    """Engine-choke-point write-back: a successful memo.send stamps the
    sender's own state/memo-outbox/<topic>.md `status: sent` plus delivery
    evidence; a failed dispatch leaves it untouched; the draft file itself is
    never removed by this module (removal, if any, is the CLI's concern —
    out of scope here)."""

    def test_successful_send_stamps_outbox_copy_sent_with_evidence(
        self, tmp_path, monkeypatch,
    ):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "outbox-stamp-success"
        outbox_path = _write_outbox_draft(
            sender_repo, topic, to="example-retrieval-repo-em", title="Stamp test",
        )

        result = _run(_memo_send(
            _base_params(dry_run=False, topic=topic, to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result
        delivered_id = result["acted"][0]["id"]
        # Portable form: receiver-repo-relative, not the absolute machine
        # path — see _portable_delivered_to_form. Absolute home paths tracked
        # into a sent memo redden example-doctrine-repo's test_no_posix_home_path_citations gate.
        expected_relpath = Path(delivered_id).resolve().relative_to(
            receiver_repo.resolve()
        ).as_posix()

        stamped_text = outbox_path.read_text(encoding="utf-8")
        split = split_frontmatter(stamped_text)
        assert split is not None
        assert "status: sent" in split.fm_text
        assert "sent_at:" in split.fm_text
        assert f"delivered_to: {expected_relpath}" in split.fm_text
        assert str(Path.home()) not in split.fm_text
        # The draft copy is stamped, not deleted — removal (if any) is a
        # CLI-side concern, out of scope for this engine module.
        assert outbox_path.exists()

    def test_failed_dispatch_does_not_stamp_outbox_copy(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "outbox-stamp-collision"
        outbox_path = _write_outbox_draft(
            sender_repo, topic, to="example-retrieval-repo-em", title="Collision test",
        )

        # Pre-create the receiver-side target so the act path refuses on
        # collision (C1 D2 criterion 4) — the write never happens, so the
        # stamp must never happen either.
        today = datetime.date.today().isoformat()
        colliding_filename = _memo_filename(today, _ENGINE_ACTOR_ID, topic)
        (receiver_repo / "cross-repo" / "inbox" / colliding_filename).write_text(
            "existing", encoding="utf-8",
        )

        result = _run(_memo_send(
            _base_params(dry_run=False, topic=topic, to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 2, result
        assert result["failed"], result

        unstamped_text = outbox_path.read_text(encoding="utf-8")
        split = split_frontmatter(unstamped_text)
        assert split is not None
        assert "status: draft" in split.fm_text
        assert "sent_at:" not in split.fm_text
        assert "delivered_to:" not in split.fm_text

    def test_missing_outbox_draft_is_silently_skipped(self, tmp_path, monkeypatch):
        """A flag-only/campaign send with no prior memo.draft has nothing at
        the outbox path — the stamp step must be a silent no-op, not a
        failure."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(
            _base_params(dry_run=False, topic="no-outbox-draft-here", to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result
        assert not (sender_repo / "state" / "memo-outbox" / "no-outbox-draft-here.md").exists()

    def test_already_sent_outbox_copy_is_not_reclobbered(self, tmp_path, monkeypatch):
        """A second send of a topic whose outbox copy already reads
        status: sent (e.g. a stale re-run) must not overwrite the FIRST
        send's delivery evidence with a second stamp."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "outbox-already-sent"
        outbox_path = _write_outbox_draft(
            sender_repo, topic, to="example-retrieval-repo-em", title="Already sent",
        )
        outbox_path.write_text(
            outbox_path.read_text(encoding="utf-8").replace(
                "status: draft", "status: sent\nsent_at: 2020-01-01T00:00:00Z\n"
                "delivered_to: /original/delivery/path.md",
            ),
            encoding="utf-8",
        )

        # Same topic as the already-"sent"-stamped outbox file above — this
        # is the exact shape a fan-out receiver K+1 would hit if it shared
        # topic with receiver K (or a stale re-run against an already-sent
        # topic), and the ONLY thing that must protect the first stamp is
        # the status:draft guard inside _stamp_sender_outbox_sent.
        result = _run(_memo_send(
            _base_params(dry_run=False, topic=topic, to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result

        unchanged_text = outbox_path.read_text(encoding="utf-8")
        assert "delivered_to: /original/delivery/path.md" in unchanged_text
        assert "sent_at: 2020-01-01T00:00:00Z" in unchanged_text


# ===========================================================================
# Sender-side sent-memo ledger (append-only, UNCONDITIONAL on a draft
# existing — closes the one-shot-form chunk-closure gap)
#
# Root cause: cross-repo memo, example-doctrine-repo-em, 2026-08-04 — a plan chunk in a
# sending repo whose deliverable is a memo has no local evidence the memo
# shipped when sent via the legacy one-shot flag form (no memo.draft, so
# _stamp_sender_outbox_sent's conditional stamp never fires). See
# memo_send._append_sent_ledger's docstring for the full rationale these
# tests pin.
# ===========================================================================

def _read_ledger_lines(sender_repo: Path) -> list[dict]:
    ledger_path = _sender_sent_ledger_path(sender_repo)
    if not ledger_path.is_file():
        return []
    return [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestSentMemoLedger:
    """_append_sent_ledger: one JSONL line per delivered receiver in
    state/memo-outbox/sent-ledger.jsonl, unconditional on an outbox draft
    ever having existed."""

    def test_one_shot_form_with_no_outbox_draft_still_produces_a_ledger_line(
        self, tmp_path, monkeypatch,
    ):
        """The regression this ledger exists to prevent: a flag-only send
        (no prior memo.draft, so _stamp_sender_outbox_sent no-ops) must still
        leave local evidence of the send."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "one-shot-ledger"
        result = _run(_memo_send(
            _base_params(dry_run=False, topic=topic, to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result
        # No outbox draft ever existed for this topic — confirming this is
        # genuinely the one-shot path, not a lifecycle send.
        assert not (sender_repo / "state" / "memo-outbox" / f"{topic}.md").exists()

        lines = _read_ledger_lines(sender_repo)
        assert len(lines) == 1
        line = lines[0]
        assert line["topic"] == topic
        assert line["to"] == "example-retrieval-repo-em"
        assert line["kind"] == "fyi"
        assert line["in_reply_to"] is None
        assert line["sent_at"]
        delivered_id = result["acted"][0]["id"]
        expected_relpath = Path(delivered_id).resolve().relative_to(
            receiver_repo.resolve()
        ).as_posix()
        assert line["delivered_to"] == expected_relpath

    def test_lifecycle_form_produces_both_sent_stamp_and_ledger_line(
        self, tmp_path, monkeypatch,
    ):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        topic = "lifecycle-ledger"
        outbox_path = _write_outbox_draft(
            sender_repo, topic, to="example-retrieval-repo-em", title="Lifecycle ledger test",
        )

        result = _run(_memo_send(
            _base_params(dry_run=False, topic=topic, to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result

        # Existing stamp behavior is untouched.
        stamped_text = outbox_path.read_text(encoding="utf-8")
        assert "status: sent" in stamped_text

        lines = _read_ledger_lines(sender_repo)
        assert len(lines) == 1
        assert lines[0]["topic"] == topic

    def test_fan_out_produces_one_ledger_line_per_receiver(self, tmp_path, monkeypatch):
        receiver_a = _make_receiver_git_repo(tmp_path, "receiver-a")
        receiver_b = _make_receiver_git_repo(tmp_path, "receiver-b")
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(
            tmp_path, {"receiver_a": receiver_a, "receiver_b": receiver_b}
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        params = _base_params(dry_run=False, topic="fan-out-ledger")
        params["to"] = ["receiver-a-em", "receiver-b-em"]
        result = _run(_memo_send(params, repo_root=str(sender_repo / ".git")))
        assert result["exit_code"] == 0, result

        lines = _read_ledger_lines(sender_repo)
        assert len(lines) == 2
        assert {line["to"] for line in lines} == {"receiver-a-em", "receiver-b-em"}

    def test_second_send_appends_rather_than_truncates(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result_1 = _run(_memo_send(
            _base_params(dry_run=False, topic="append-first", to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result_1["exit_code"] == 0, result_1
        result_2 = _run(_memo_send(
            _base_params(dry_run=False, topic="append-second", to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result_2["exit_code"] == 0, result_2

        lines = _read_ledger_lines(sender_repo)
        assert len(lines) == 2
        assert [line["topic"] for line in lines] == ["append-first", "append-second"]

    def test_delivered_to_is_receiver_relative_never_absolute_home_path(
        self, tmp_path, monkeypatch,
    ):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_send(
            _base_params(dry_run=False, topic="home-path-check", to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result

        lines = _read_ledger_lines(sender_repo)
        assert len(lines) == 1
        assert str(Path.home()) not in lines[0]["delivered_to"]
        assert not Path(lines[0]["delivered_to"]).is_absolute()

    def test_ledger_write_failure_does_not_fail_the_send(self, tmp_path, monkeypatch):
        """An unwritable ledger path (best-effort, never-raise contract) must
        not turn a successful send into a reported failure."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # Pre-create the ledger's PARENT as a plain file (not a directory) so
        # `ledger_path.parent.mkdir(...)` raises OSError/NotADirectoryError.
        outbox_dir = sender_repo / "state" / "memo-outbox"
        outbox_dir.parent.mkdir(parents=True, exist_ok=True)
        outbox_dir.write_text("blocking file, not a directory", encoding="utf-8")

        result = _run(_memo_send(
            _base_params(dry_run=False, topic="ledger-write-failure", to="example-retrieval-repo-em"),
            repo_root=str(sender_repo / ".git"),
        ))
        assert result["exit_code"] == 0, result
        assert result["acted"], result
