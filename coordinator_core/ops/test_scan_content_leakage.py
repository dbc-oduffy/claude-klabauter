"""
coordinator_core.ops.test_scan_content_leakage

Characterization tests for the "percolate.scan_content_leakage_tiers" op
(coordinator_core.ops.scan_content_leakage) — the three-tier Python-`re`
content-leakage sweep ratified by Wave-3 settlement B7.

Coverage:
  (a) registered under exactly "percolate.scan_content_leakage_tiers" on import
  (b) missing target_root raises a descriptive ValueError; a nonexistent
      target_root is a premise failure (fail-loud), not a falsely green scan
  (c) clean tree → all tiers empty, blocked False, skipped empty
  (d) HIGH-tier hit (credential shape) → finding recorded, blocked True
  (e) MEDIUM-tier hit (internal path / email shapes) → recorded, blocked False
  (f) LOW-tier hit (40-hex SHA) → recorded, blocked False
  (g) binary file → skipped with a per-file note, sweep completes (no crash)
  (h) .git tree contents excluded from the sweep
  (i) two-detector split preserved: operator-identity-shaped tokens produce
      NO finding here (they are example-doctrine-repo publish.py's PERSONAL_REVIEW_PATTERNS
      surface, deliberately NOT folded in — settlement B7 binding)
  (j) idempotency (AC7/CC-4): double invocation over identical content
      returns an identical result (read-only, inherent)

Secret-shaped fixtures are constructed by concatenation so no literal
credential-looking token sits in this file's source.

Spec backlink: docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B7
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.scan_content_leakage  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.scan_content_leakage import _scan_content_leakage_tiers

_OP_NAME = "percolate.scan_content_leakage_tiers"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.scan_content_leakage @register_op did not fire"
)


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _scan(root):
    return _scan_content_leakage_tiers({"target_root": str(root)})


def test_op_registered():
    assert _OP_NAME in _REGISTRY


def test_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        _scan_content_leakage_tiers({})


def test_nonexistent_target_root_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        _scan_content_leakage_tiers(
            {"target_root": str(tmp_path / "no-such-tree")}
        )


def test_clean_tree_all_empty_not_blocked(tmp_path):
    root = tmp_path / "pub"
    _write(root, "docs/readme.md", "Nothing to see here.\nJust prose.\n")
    result = _scan(root)
    assert result == {
        "high": [],
        "medium": [],
        "low": [],
        "blocked": False,
        "skipped": [],
    }


def test_high_tier_credential_shape_blocks(tmp_path):
    root = tmp_path / "pub"
    # Constructed, not literal — a GitHub-PAT-shaped token.
    token = "ghp_" + "A1b2C3d4" * 3  # 24 chars after the prefix
    _write(root, "snippets/example.md", f"token = {token}\n")
    result = _scan(root)
    assert result["blocked"] is True
    assert result["high"] == [
        {"path": "snippets/example.md", "line": 1, "match": token}
    ]
    assert result["medium"] == []
    assert result["low"] == []


def test_medium_tier_internal_path_and_email_do_not_block(tmp_path):
    root = tmp_path / "pub"
    _write(
        root,
        "wiki/notes.md",
        "See ~/.claude/tasks/foo for details.\n"
        "Contact someone@example.com about it.\n",
    )
    result = _scan(root)
    assert result["blocked"] is False
    assert result["high"] == []
    paths_lines = [(f["path"], f["line"]) for f in result["medium"]]
    assert ("wiki/notes.md", 1) in paths_lines
    assert ("wiki/notes.md", 2) in paths_lines


def test_low_tier_commit_sha_informational_only(tmp_path):
    root = tmp_path / "pub"
    sha = "0123456789abcdef" * 2 + "01234567"  # 40 hex chars
    _write(root, "docs/changelog.md", f"Fixed in {sha} last week.\n")
    result = _scan(root)
    assert result["blocked"] is False
    assert result["low"] == [
        {"path": "docs/changelog.md", "line": 1, "match": sha}
    ]
    assert result["high"] == []
    assert result["medium"] == []


def test_binary_file_skipped_with_note_sweep_completes(tmp_path):
    root = tmp_path / "pub"
    binary = root / "assets" / "logo.png"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x89PNG\xff\xfe\x00\x01invalid-utf8\xff")
    sha = "abcdef0123456789" * 2 + "abcdef01"  # 40 hex chars
    _write(root, "docs/notes.md", f"deadline commit {sha}\n")
    result = _scan(root)
    assert result["skipped"] == ["assets/logo.png"]
    # The sweep completed past the binary file and still scanned the text file.
    assert len(result["low"]) == 1


def test_git_tree_excluded(tmp_path):
    root = tmp_path / "pub"
    token = "ghp_" + "Zz9Yy8Xx7" * 3
    _write(root, ".git/config-blob.txt", f"leaked = {token}\n")
    _write(root, "docs/clean.md", "All clear.\n")
    result = _scan(root)
    assert result == {
        "high": [],
        "medium": [],
        "low": [],
        "blocked": False,
        "skipped": [],
    }


def test_operator_identity_tokens_not_scanned_here(tmp_path):
    """Settlement B7 binding: identity-token scanning stays in example-doctrine-repo publish.py
    (PERSONAL_REVIEW_PATTERNS) — this op must NOT flag operator-identity-shaped
    content that matches no generic tier shape."""
    root = tmp_path / "pub"
    _write(
        root,
        "wiki/machines.md",
        "The build ran on machine codename machine-b under the usual account.\n",
    )
    result = _scan(root)
    assert result == {
        "high": [],
        "medium": [],
        "low": [],
        "blocked": False,
        "skipped": [],
    }


def test_double_invocation_identical_result(tmp_path):
    """AC7/CC-4 idempotency proof: read-only sweep — the second call with
    identical inputs returns an identical result (inherent, per the module's
    DEC-7 note)."""
    root = tmp_path / "pub"
    token = "xoxb-" + "1234567890-abc"
    _write(root, "docs/a.md", f"slack {token}\n")
    _write(root, "docs/b.md", "path ~/.claude/plans/x.md\n")

    first = _scan(root)
    second = _scan(root)

    assert first == second
    assert first["blocked"] is True
