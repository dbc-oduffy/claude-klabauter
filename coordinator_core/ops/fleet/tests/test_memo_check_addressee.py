"""
Tests for coordinator_core.ops.fleet.memo_check_addressee — memo.check_addressee
COMPUTE_ONLY UDS op.

Ratifying spinoff: state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md

Cover:
  - MATCH — self_root resolves to the same repo as `to`.
  - MISMATCH — self is repo A, `to` resolves to registered repo B.
  - UNRESOLVED + suggestion — `to` is a near-miss of a registered receiver
    (defect 2, GREEN).
  - setup errors — missing `to`; `dry_run: false`; `repo_root=None`.
  - defect-1 redirect-MATCH — `to` is a DoE redirect-alias literal that
    resolves (via the manifest's declared central receiver id) to the same
    repo as self. DoE promoted `identity.redirectAliases` into the manifest
    2026-07-21; this is now a real passing assertion, not a gated xfail.

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency
(mirrors test_memo_list.py).

Spec backlink: state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coordinator_core.ops.fleet.memo_check_addressee import (
    _MODE,
    _memo_check_addressee,
    _validate_check_addressee_params,
)

import pytest
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _run(result):
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _make_claude_home(tmp_path: Path, receiver_repos: dict) -> Path:
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _registered_doe_claude(machine_local: Path) -> Path | None:
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
        value = data.get("repos.doe_claude") or (data.get("repos") or {}).get("doe_claude")
        if value:
            return Path(str(value))
    return None


def _write_doe_manifest(
    claude_home: Path, tmp_path: Path, manifest: dict, doe_root: Path | None = None
) -> None:
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    doe_root = (
        doe_root or _registered_doe_claude(machine_local) or (tmp_path / "doe-root")
    )
    schemas_dir = doe_root / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    machine_local.mkdir(parents=True, exist_ok=True)
    (machine_local / ".doe-root").write_text(str(doe_root), encoding="utf-8")
    (schemas_dir / "coordinator-registry.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _git_common_dir(repo_root: Path) -> Path:
    import subprocess

    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(repo_root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    return (repo_root / ".git").resolve()


class TestMatch:
    def test_self_resolves_to_same_repo_as_to(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "claude-klabauter"
        self_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"claude_klabauter": str(self_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = _git_common_dir(self_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "claude-klabauter-em"}, repo_root=common_dir
            )
        )

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        candidate = result["candidates"][0]
        assert candidate["verdict"] == "MATCH"
        assert candidate["resolved"] is True
        assert candidate["self_repo"] == str(self_repo)
        assert candidate["to_repo"] == str(self_repo)


# 2. MISMATCH

class TestMismatch:
    def test_to_resolves_to_a_different_registered_repo(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "claude-klabauter"
        other_repo = tmp_path / "project-rag"
        self_repo.mkdir()
        other_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"claude_klabauter": str(self_repo), "project_rag": str(other_repo)},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = _git_common_dir(self_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "example-retrieval-repo-em"}, repo_root=common_dir
            )
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["verdict"] == "MISMATCH"
        assert candidate["resolved"] is True
        assert candidate["self_repo"] == str(self_repo)
        assert candidate["to_repo"] == str(other_repo)


# 3. UNRESOLVED + suggestion (defect 2, GREEN)

class TestUnresolvedWithSuggestion:
    def test_near_miss_receiver_gets_did_you_mean_suggestion(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "some-self-repo"
        self_repo.mkdir()
        claude_klabauter_repo = tmp_path / "claude-klabauter"
        claude_klabauter_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"some_self_repo": str(self_repo), "claude_klabauter": str(claude_klabauter_repo)},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = _git_common_dir(self_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "claude-klabauter-em"}, repo_root=common_dir
            )
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["verdict"] == "UNRESOLVED"
        assert candidate["resolved"] is False
        assert candidate["to_repo"] is None
        assert "Did you mean" in candidate["note"]
        assert "claude-klabauter-em" in candidate["note"]


class TestSetupErrors:
    def test_missing_to_rejected(self):
        result = _validate_check_addressee_params({"dry_run": True})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_empty_to_rejected(self):
        result = _validate_check_addressee_params({"dry_run": True, "to": "   "})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_dry_run_false_rejected(self):
        result = _validate_check_addressee_params({"dry_run": False, "to": "some-em"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_handler_dry_run_false_returns_setup_error(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_check_addressee({"dry_run": False, "to": "some-em"}, repo_root=tmp_path)
        )
        assert result["exit_code"] == 1
        assert result["dry_run"] is False

    def test_repo_root_none_returns_setup_error(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_check_addressee({"dry_run": True, "to": "some-em"}, repo_root=None)
        )
        assert result["exit_code"] == 1


class TestResolverExceptionMapping:

    def test_corrupt_registry_returns_setup_error(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        claude_home = tmp_path / "claude-home"
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text(
            'schema = 1\n"repos.broken" = "unterminated\n', encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_check_addressee({"dry_run": True, "to": "some-em"}, repo_root=tmp_path)
        )
        assert result["exit_code"] == 1

    def test_ambiguous_central_receiver_returns_setup_error(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {
                "central": str(tmp_path / "central-repo"),
                "doe_claude": str(tmp_path / "doe-claude-repo"),
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        _write_doe_manifest(
            claude_home,
            tmp_path,
            {
                "identity": {
                    "centralReceiverIds": ["central-em", "doe-claude-em"],
                    "repoAliases": [],
                }
            },
        )

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "central-em"}, repo_root=tmp_path
            )
        )
        assert result["exit_code"] == 1


class TestRedirectMatchDefect1:
    def test_redirect_alias_matches_central_self(self, tmp_path, monkeypatch):
        central_repo = tmp_path / "doe-claude-repo"
        central_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"doe_claude": str(central_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        _write_doe_manifest(
            claude_home,
            tmp_path,
            {
                "identity": {
                    "centralReceiverIds": ["doe-claude-em"],
                    "repoAliases": [],
                    "redirectAliases": ["coordinator-claude"],
                }
            },
        )

        common_dir = _git_common_dir(central_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "coordinator-claude"}, repo_root=common_dir
            )
        )

        candidate = result["candidates"][0]
        assert candidate["verdict"] == "MATCH"
        assert candidate["resolved"] is True
        assert candidate["self_repo"] == str(central_repo)
        assert candidate["to_repo"] == str(central_repo)
