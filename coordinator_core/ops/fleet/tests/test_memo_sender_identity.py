"""
Tests for coordinator_core.ops.fleet._memo_compose's sender-identity resolution —
the DoE `e267d18336` fix (doe-claude-bc withdrew the Ask-1 concurrence that had
excused a fixed `_ENGINE_ACTOR_ID = "claude-klabauter-engine"` literal in `from:`).

Covers:
  - a falsy `from_id` resolves to a RESOLVED RECEIVER (the sending repo's own
    registered identity), never the old fixed literal.
  - a sending repo registered as a publish-target mirror resolves to the
    mirror's declared OWNER, not the mirror's own unaddressable alias — the
    exact `claude-klabauter-em` -> `claude-klabauter-em` case the defect report
    cites.
  - `resolve_and_assert_sender_id` warns once at compose time, and still
    composes, when a defaulted sender identity does not resolve to anything
    `cross-repo-memo --list-receivers` would accept. It raised at first; the
    raise was stricter than this repo's own rule for the symmetric
    receiver-side case ("where no peer EM is reachable `memo.send` warns once
    and then sends"), and refused every memo from an unregistered repo.

Harness: asyncio-free — these are plain function calls, no UDS op envelope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet._memo_compose import (
    _resolve_engine_sender_id,
    resolve_and_assert_sender_id,
    resolve_sender_id,
)


def _make_sender_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_claude_home(
    tmp_path: Path,
    receiver_repos: dict[str, Path] | None = None,
    mirror_tables: dict[str, dict] | None = None,
) -> Path:
    receiver_repos = receiver_repos or {}
    mirror_tables = mirror_tables or {}
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)

    baseline_lines = ["schema = 1"]
    for mirror_key, entry in mirror_tables.items():
        baseline_lines.append(f"\n[publish.mirrors.{mirror_key}]")
        owner = entry.get("owner")
        if owner is not None:
            baseline_lines.append(f'owner = "{owner}"')
        path = entry.get("path")
        if path is not None:
            toml_path = str(path).replace("\\", "\\\\").replace('"', '\\"')
            baseline_lines.append(f'path = "{toml_path}"')
    (machine_local / "registry.toml").write_text(
        "\n".join(baseline_lines) + "\n", encoding="utf-8"
    )

    local_lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        local_lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(local_lines) + "\n", encoding="utf-8"
    )
    return claude_home


class TestEngineDefaultedSenderResolvesToRegisteredReceiver:
    def test_falsy_from_id_resolves_to_sending_repo_own_identity(
        self, tmp_path, monkeypatch
    ):
        sender = _make_sender_root(tmp_path / "claude-klabauter")
        claude_home = _make_claude_home(tmp_path, {"claude_klabauter": sender})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        resolved = resolve_sender_id(None, root=str(sender))

        assert resolved == "claude-klabauter-em"
        assert resolved != "claude-klabauter-engine"

    def test_explicit_from_id_still_passes_through_unchanged(self, tmp_path, monkeypatch):
        assert resolve_sender_id("some-caller-em", root=str(tmp_path)) == "some-caller-em"


class TestPublishedMirrorResolvesToOwner:
    def test_mirror_root_resolves_to_declared_owner_not_the_mirror_alias(
        self, tmp_path, monkeypatch
    ):
        mirror_root = _make_sender_root(tmp_path / "claude-klabauter")
        claude_home = _make_claude_home(
            tmp_path,
            mirror_tables={
                "claude_klabauter": {"owner": "claude-klabauter-em", "path": str(mirror_root)},
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        resolved = _resolve_engine_sender_id(root=str(mirror_root))

        assert resolved == "claude-klabauter-em"
        assert "claude-klabauter" not in resolved


class TestComposeTimeAssertionWarnsOnUnacceptedSender:
    def test_defaulted_sender_not_a_registered_receiver_warns_and_composes(
        self, tmp_path, monkeypatch, caplog
    ):
        sender = _make_sender_root(tmp_path / "totally-unregistered-repo")
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        with caplog.at_level("WARNING"):
            resolved = resolve_and_assert_sender_id(None, root=str(sender))

        assert resolved is not None
        assert any(
            "not a name" in record.message for record in caplog.records
        )

    def test_registered_defaulted_sender_passes(self, tmp_path, monkeypatch):
        sender = _make_sender_root(tmp_path / "project-rag")
        claude_home = _make_claude_home(tmp_path, {"project_rag": sender})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        resolved = resolve_and_assert_sender_id(None, root=str(sender))

        assert resolved == "example-retrieval-repo-em"

    def test_explicit_from_id_bypasses_the_assertion(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        assert (
            resolve_and_assert_sender_id("some-unregistered-em", root=str(tmp_path))
            == "some-unregistered-em"
        )
