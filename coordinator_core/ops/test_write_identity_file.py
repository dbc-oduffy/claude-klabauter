"""
coordinator_core.ops.test_write_identity_file

Tests for the install.write_identity_file op.

Import guard: coordinator_core.ops.write_identity_file MUST be imported at
module load time so @register_op("install.write_identity_file") fires and
populates _REGISTRY.

Coverage:
  (a) registry assertion  — op name present in _REGISTRY after import
  (b) fresh write         — no prior identity file: writes version + fields,
                            written=True
  (c) double-invocation   — identical (claude_home, fields) twice: second call
                            written=False, file bytes unchanged (AC7)
  (d) merge, not replace  — a second call with a DIFFERENT field preserves the
                            first call's field untouched (Step 3 / Step 3b-4
                            fold-into-one-write oracle)
  (e) overwrite existing key — a second call with the SAME key, different
                            value updates just that key
  (f) missing claude_home param  → exit_code 1
  (g) missing fields param       → exit_code 1
  (h) non-string field value     → exit_code 1
  (i) not inside a git repo      → exit_code 1 (no .git ancestor found)
  (j) LockTimeout                → exit_code 1 with lock-timeout message

Spec backlink: coordinator_core/ops/write_identity_file.py
Port source:   commands/install.md (coordinator-claude) Phase 2, Steps 3 / 3b-4
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------

from coordinator_core.ipc import _REGISTRY  # noqa: E402
from coordinator_core.locked_write import LockTimeout  # noqa: E402
import coordinator_core.ops.write_identity_file as wif  # noqa: E402
from coordinator_core.ops.write_identity_file import _handler  # noqa: E402

_OP_NAME = "install.write_identity_file"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.write_identity_file @register_op did not fire"
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(root: Path) -> Path:
    """Create a minimal committed git repo at *root* so locked_rmw's
    git_common_dir lookup resolves. Mirrors coordinator_core/tests/
    test_locked_write.py's own fixture (same requirement: at least one commit
    for git_common_dir to work correctly)."""
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "write-identity-file-test@claude-klabauter.test")
    _git("config", "user.name", "Write Identity File Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


def _call(params: dict) -> dict:
    return asyncio.run(_handler(params))


# ---------------------------------------------------------------------------
# (a) registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) fresh write
# ---------------------------------------------------------------------------


def test_fresh_write_creates_identity_file(tmp_path):
    claude_home = _make_git_repo(tmp_path / "home")
    result = _call({
        "claude_home": str(claude_home),
        "fields": {"operator_name": "Dax"},
    })
    assert result["exit_code"] == 0
    assert result["written"] is True

    identity_path = claude_home / ".claude" / "coordinator-identity.yaml"
    assert result["path"] == str(identity_path)
    text = identity_path.read_text(encoding="utf-8")
    assert text.startswith("# ~/.claude/coordinator-identity.yaml")
    doc = yaml.safe_load(text)
    assert doc == {"version": 1, "operator_name": "Dax"}


# ---------------------------------------------------------------------------
# (c) double-invocation idempotency (AC7)
# ---------------------------------------------------------------------------


def test_double_invocation_is_idempotent_noop(tmp_path):
    claude_home = _make_git_repo(tmp_path / "home")
    params = {"claude_home": str(claude_home), "fields": {"operator_name": "Sisko"}}

    first = _call(params)
    assert first["written"] is True

    identity_path = claude_home / ".claude" / "coordinator-identity.yaml"
    before = identity_path.read_text(encoding="utf-8")
    before_mtime = identity_path.stat().st_mtime_ns

    second = _call(params)
    assert second["exit_code"] == 0
    assert second["written"] is False

    after = identity_path.read_text(encoding="utf-8")
    after_mtime = identity_path.stat().st_mtime_ns
    assert after == before
    assert after_mtime == before_mtime


# ---------------------------------------------------------------------------
# (d) merge, not replace — two distinct-key calls fold into one document
# ---------------------------------------------------------------------------


def test_second_call_merges_new_field_without_clobbering_first(tmp_path):
    claude_home = _make_git_repo(tmp_path / "home")
    _call({"claude_home": str(claude_home), "fields": {"operator_name": "Kira"}})
    result = _call({
        "claude_home": str(claude_home),
        "fields": {"engagement_posture": "default"},
    })
    assert result["written"] is True

    identity_path = claude_home / ".claude" / "coordinator-identity.yaml"
    doc = yaml.safe_load(identity_path.read_text(encoding="utf-8"))
    assert doc == {
        "version": 1,
        "operator_name": "Kira",
        "engagement_posture": "default",
    }


# ---------------------------------------------------------------------------
# (e) overwrite an existing key with a new value
# ---------------------------------------------------------------------------


def test_overwrite_existing_key(tmp_path):
    claude_home = _make_git_repo(tmp_path / "home")
    _call({"claude_home": str(claude_home), "fields": {"operator_name": "Kira"}})
    result = _call({"claude_home": str(claude_home), "fields": {"operator_name": "Odo"}})
    assert result["written"] is True

    identity_path = claude_home / ".claude" / "coordinator-identity.yaml"
    doc = yaml.safe_load(identity_path.read_text(encoding="utf-8"))
    assert doc == {"version": 1, "operator_name": "Odo"}


# ---------------------------------------------------------------------------
# (f)-(h) param validation
# ---------------------------------------------------------------------------


def test_missing_claude_home_param():
    result = _call({"fields": {"operator_name": "Dax"}})
    assert result["exit_code"] == 1
    assert "claude_home" in result["error"]


def test_missing_fields_param(tmp_path):
    claude_home = _make_git_repo(tmp_path / "home")
    result = _call({"claude_home": str(claude_home)})
    assert result["exit_code"] == 1
    assert "fields" in result["error"]


def test_non_string_field_value_rejected(tmp_path):
    claude_home = _make_git_repo(tmp_path / "home")
    result = _call({"claude_home": str(claude_home), "fields": {"operator_name": 7}})
    assert result["exit_code"] == 1
    assert "dict[str, str]" in result["error"]


# ---------------------------------------------------------------------------
# (i) not inside a git repo
# ---------------------------------------------------------------------------


def test_not_inside_git_repo_fails_loud(tmp_path):
    claude_home = tmp_path / "no-git-home"
    claude_home.mkdir()
    result = _call({"claude_home": str(claude_home), "fields": {"operator_name": "Dax"}})
    assert result["exit_code"] == 1
    assert "not inside a git repository" in result["error"]


# ---------------------------------------------------------------------------
# (j) LockTimeout surfaces as exit_code 1
# ---------------------------------------------------------------------------


def test_lock_timeout_surfaces_as_error(tmp_path, monkeypatch):
    claude_home = _make_git_repo(tmp_path / "home")

    def _raise_timeout(*_args, **_kwargs):
        raise LockTimeout("simulated timeout")

    monkeypatch.setattr(wif, "locked_rmw", _raise_timeout)
    result = _call({"claude_home": str(claude_home), "fields": {"operator_name": "Dax"}})
    assert result["exit_code"] == 1
    assert "lock timeout" in result["error"]
