"""
coordinator_core.ops.test_self_persist_findings

Tests for the findings.self_persist_fallback op.

Import guard: coordinator_core.ops.self_persist_findings MUST be imported at module
load time so @register_op("findings.self_persist_fallback") fires and populates
_REGISTRY.

Coverage:
  (a) registry assertion         — op name present in _REGISTRY after import
  (b) fresh write                — creates a new file with exact content; written=True
  (c) overwrite                  — different content on a second call overwrites; written=True
  (d) idempotent double-invoke   — identical (target_path, content) on a second call is a
                                    safe no-op: written=False, exit_code 0, file unchanged
  (e) missing target_path param  — exit_code 1
  (f) missing content param      — exit_code 1
  (g) relative target_path       — rejected, exit_code 1
  (h) target_path outside any git repo — exit_code 1
  (i) content with embedded quotes/backticks — written verbatim, byte-for-byte
  (j) LockTimeout                — exit_code 1 with lock-timeout message

Spec backlink: coordinator_core/ops/self_persist_findings.py
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.self_persist_findings  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.self_persist_findings import _handler

_OP_NAME = "findings.self_persist_fallback"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.self_persist_findings @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "self-persist-test@claude-klabauter.test")
    _git("config", "user.name", "Self Persist Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state").mkdir(parents=True, exist_ok=True)
    (repo / "state" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) Fresh write
# ---------------------------------------------------------------------------


def test_fresh_write_creates_file(tmp_path):
    repo = _make_git_repo(tmp_path)
    target = repo / "state" / "findings.md"

    result = _run(_handler({
        "target_path": str(target),
        "content": "# Findings\n\nsome content\n",
    }))

    assert result["exit_code"] == 0
    assert result["written"] is True
    assert result["bytes_written"] == len("# Findings\n\nsome content\n".encode("utf-8"))
    assert target.read_text(encoding="utf-8") == "# Findings\n\nsome content\n"


# ---------------------------------------------------------------------------
# (c) Overwrite with different content
# ---------------------------------------------------------------------------


def test_overwrite_with_different_content(tmp_path):
    repo = _make_git_repo(tmp_path)
    target = repo / "state" / "findings.md"
    target.write_text("old content\n", encoding="utf-8")

    result = _run(_handler({
        "target_path": str(target),
        "content": "new content\n",
    }))

    assert result["exit_code"] == 0
    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == "new content\n"


# ---------------------------------------------------------------------------
# (d) Idempotent double-invocation
# ---------------------------------------------------------------------------


def test_idempotent_double_invocation(tmp_path):
    repo = _make_git_repo(tmp_path)
    target = repo / "state" / "findings.md"
    content = "# Findings\n\nidempotent content\n"

    first = _run(_handler({"target_path": str(target), "content": content}))
    assert first["exit_code"] == 0
    assert first["written"] is True

    mtime_after_first = target.stat().st_mtime_ns

    second = _run(_handler({"target_path": str(target), "content": content}))
    assert second["exit_code"] == 0
    assert second["written"] is False
    assert second["bytes_written"] == first["bytes_written"]

    # No physical write on the identical rerun — content and mtime unchanged.
    assert target.read_text(encoding="utf-8") == content
    assert target.stat().st_mtime_ns == mtime_after_first


# ---------------------------------------------------------------------------
# (e) / (f) Missing required params
# ---------------------------------------------------------------------------


def test_missing_target_path_param(tmp_path):
    result = _run(_handler({"content": "x"}))
    assert result["exit_code"] == 1
    assert "target_path" in result["error"]


def test_missing_content_param(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = _run(_handler({"target_path": str(repo / "state" / "f.md")}))
    assert result["exit_code"] == 1
    assert "content" in result["error"]


# ---------------------------------------------------------------------------
# (g) Relative target_path rejected
# ---------------------------------------------------------------------------


def test_relative_target_path_rejected():
    result = _run(_handler({"target_path": "state/findings.md", "content": "x"}))
    assert result["exit_code"] == 1
    assert "absolute" in result["error"]


# ---------------------------------------------------------------------------
# (h) target_path outside any git repo
# ---------------------------------------------------------------------------


def test_target_path_outside_git_repo_rejected(tmp_path):
    outside = tmp_path / "no-repo-here" / "findings.md"
    result = _run(_handler({"target_path": str(outside), "content": "x"}))
    assert result["exit_code"] == 1
    assert "git repository" in result["error"]
    assert not outside.exists()


# ---------------------------------------------------------------------------
# (i) Embedded quotes/backticks written verbatim
# ---------------------------------------------------------------------------


def test_content_with_embedded_quotes_and_backticks(tmp_path):
    repo = _make_git_repo(tmp_path)
    target = repo / "state" / "findings.md"
    tricky_content = (
        "Finding: uses `subprocess.run(\"echo hi\")` and a lone ' apostrophe, "
        "plus $VAR and \\backslash\\ sequences.\n"
    )

    result = _run(_handler({"target_path": str(target), "content": tricky_content}))

    assert result["exit_code"] == 0
    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == tricky_content


# ---------------------------------------------------------------------------
# (k) Missing intermediate directory — locked_rmw creates it, not this module
# ---------------------------------------------------------------------------
# Review: code-reviewer (F6) — self_persist_findings.py has no explicit
# mkdir(parents=True) call (contrast write_identity_file.py); this asserts
# locked_rmw's own write-step mkdir handles a target_path whose immediate
# parent (and grandparent) don't yet exist but whose ancestor chain still
# resolves to the git-repo root found by _find_repo_root.


def test_missing_intermediate_directory_is_created(tmp_path):
    repo = _make_git_repo(tmp_path)
    target = repo / "state" / "review-trail" / "findings" / "x.md"
    assert not target.parent.exists()

    result = _run(_handler({
        "target_path": str(target),
        "content": "# Findings\n\nnested content\n",
    }))

    assert result["exit_code"] == 0
    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == "# Findings\n\nnested content\n"


# ---------------------------------------------------------------------------
# (j) LockTimeout surfaces as exit_code 1
# ---------------------------------------------------------------------------


def test_lock_timeout_surfaces_as_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    target = repo / "state" / "findings.md"

    with patch(
        "coordinator_core.ops.self_persist_findings.locked_rmw",
        side_effect=LockTimeout("simulated timeout"),
    ):
        result = _run(_handler({"target_path": str(target), "content": "x"}))

    assert result["exit_code"] == 1
    assert "lock timeout" in result["error"]
