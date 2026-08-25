"""Regression tests — cross_repo_memos full `body` content emission (2026-07-24, C8).

Plan: docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md § C8. C8 widens the
C6 `decision_note`-only substance feed to the memo's FULL body content, so the fleet can
content-search memo prose. ``records.query`` never returns body text (only
``{path, frontmatter}``), so the section re-reads the source file directly
(``_read_memo_body``) and bounds it (``_cap_body``) before emission — never unbounded, never
silently dropped on an oversized body.

These tests exercise the two new helpers directly, then ``_collect_bucket`` end-to-end
against real on-disk memo files (unlike the query-failure-signal tests, which stub out
``_collect_bucket``/``_query_records`` and never touch body content at all).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coordinator_core.ops.emit.sections.cross_repo_memos import (
    _BODY_MAX_CHARS,
    _cap_body,
    _collect_bucket,
    _read_memo_body,
)


def _make_ctx(repo_root: Path, repo_name: str = "test-org/test-repo") -> MagicMock:
    ctx = MagicMock()
    ctx.repo_name = repo_name
    ctx.repo_root = str(repo_root)
    ctx.subprocess_root = None

    def provenance(source_kind: str, path: str | None = None, derivation: str = "parsed") -> dict:
        return {
            "source_kind": source_kind,
            "repo": repo_name,
            "ref": None,
            "path": path or "",
            "observed_at": "2026-07-24T00:00:00Z",
            "derivation": derivation,
        }

    ctx.provenance.side_effect = provenance
    return ctx


def _write_memo(root: Path, rel_path: str, body_text: str, **frontmatter) -> None:
    fm_lines = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    content = f"---\n{fm_lines}\n---\n{body_text}"
    full_path = root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# _cap_body — bounded, never dropped
# ---------------------------------------------------------------------------


def test_cap_body_passes_through_under_cap_unchanged():
    text = "Some memo body content."
    assert _cap_body(text) == text


def test_cap_body_truncates_oversized_with_ellipsis():
    long_body = "x" * (_BODY_MAX_CHARS + 500)

    capped = _cap_body(long_body)

    assert capped is not None
    assert len(capped) == _BODY_MAX_CHARS
    assert capped.endswith("…")


def test_cap_body_none_on_blank_or_non_string():
    assert _cap_body("") is None
    assert _cap_body("   ") is None
    assert _cap_body(None) is None
    assert _cap_body(123) is None


# ---------------------------------------------------------------------------
# _read_memo_body — re-reads the source file, strips frontmatter
# ---------------------------------------------------------------------------


def test_read_memo_body_strips_frontmatter_and_returns_body(tmp_path):
    _write_memo(
        tmp_path,
        "cross-repo/inbox/2026-07-24-x.md",
        "This is the memo body.\n\nMore prose here.",
        title="An ask",
        **{"from": "team-a", "to": "team-b"},
    )
    ctx = _make_ctx(tmp_path)

    body = _read_memo_body(ctx, "cross-repo/inbox/2026-07-24-x.md")

    assert body is not None
    assert "This is the memo body." in body
    assert "title:" not in body


def test_read_memo_body_none_on_missing_file(tmp_path):
    ctx = _make_ctx(tmp_path)

    body = _read_memo_body(ctx, "cross-repo/inbox/does-not-exist.md")

    assert body is None


def test_read_memo_body_none_on_non_string_path(tmp_path):
    ctx = _make_ctx(tmp_path)

    assert _read_memo_body(ctx, None) is None
    assert _read_memo_body(ctx, "") is None


# Review: code-reviewer (F1) — UnicodeDecodeError (a ValueError subclass) was not caught
# by the original `except OSError` guard, so a non-UTF-8 memo body crashed the whole
# section's collect() rather than fail-opening to None. Regression test for the widened
# `except (OSError, UnicodeDecodeError)` guard.
def test_read_memo_body_none_on_non_utf8_file(tmp_path):
    rel_path = "cross-repo/inbox/2026-07-24-binary.md"
    full_path = tmp_path / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(b"---\ntitle: t\n---\n\xff\xfe\x00\x01not valid utf-8")
    ctx = _make_ctx(tmp_path)

    body = _read_memo_body(ctx, rel_path)

    assert body is None


def test_read_memo_body_uses_subprocess_root_when_set(tmp_path):
    real_root = tmp_path / "real"
    other_root = tmp_path / "other"
    real_root.mkdir()
    other_root.mkdir()
    _write_memo(real_root, "cross-repo/inbox/x.md", "Body under subprocess_root.", title="t")

    ctx = _make_ctx(other_root)
    ctx.subprocess_root = str(real_root)

    body = _read_memo_body(ctx, "cross-repo/inbox/x.md")

    assert body is not None
    assert "Body under subprocess_root." in body


# ---------------------------------------------------------------------------
# _collect_bucket — end-to-end: body lands on the emitted record, bounded, never dropped
# ---------------------------------------------------------------------------


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_stamps_full_body_on_record(mock_qr, tmp_path):
    _write_memo(
        tmp_path,
        "cross-repo/inbox/2026-07-24-x.md",
        "Full body content that should be content-searchable.",
        title="Some ask",
        **{"from": "team-a", "to": "team-b"},
    )
    mock_qr.return_value = (
        [
            {
                "path": "cross-repo/inbox/2026-07-24-x.md",
                "frontmatter": {
                    "title": "Some ask",
                    "from": "team-a",
                    "to": "team-b",
                    "status": "open",
                    "created": "2026-07-24T00:00:00Z",
                },
            }
        ],
        None,
    )
    ctx = _make_ctx(tmp_path)

    records, malformed = _collect_bucket(ctx, "cross-repo-memo", archived=False)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["body"] == "Full body content that should be content-searchable."


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_oversized_body_capped_not_dropped(mock_qr, tmp_path):
    long_body = "y" * (_BODY_MAX_CHARS + 1000)
    _write_memo(
        tmp_path,
        "cross-repo/archive/2026-07-24-big.md",
        long_body,
        title="A big closed ask",
        **{"from": "team-a", "to": "team-b", "status": "actioned"},
    )
    mock_qr.return_value = (
        [
            {
                "path": "cross-repo/archive/2026-07-24-big.md",
                "frontmatter": {
                    "title": "A big closed ask",
                    "from": "team-a",
                    "to": "team-b",
                    "status": "actioned",
                    "created": "2026-07-24T00:00:00Z",
                },
            }
        ],
        None,
    )
    ctx = _make_ctx(tmp_path)

    records, malformed = _collect_bucket(ctx, "archived-memo", archived=True)

    assert malformed == []
    assert len(records) == 1
    body = records[0]["body"]
    assert body is not None
    assert len(body) == _BODY_MAX_CHARS
    assert body.endswith("…")


@patch("coordinator_core.ops.emit.sections.cross_repo_memos._query_records")
def test_collect_bucket_body_key_absent_when_source_file_unreadable(mock_qr, tmp_path):
    """The `path` records.query returned no longer resolves to a readable file — the
    record still emits (metadata fields intact); only `body` is absent (fail-open, not a
    quarantine — distinct from the malformed-shape path)."""
    mock_qr.return_value = (
        [
            {
                "path": "cross-repo/inbox/vanished.md",
                "frontmatter": {
                    "title": "Some ask",
                    "from": "team-a",
                    "to": "team-b",
                    "status": "open",
                    "created": "2026-07-24T00:00:00Z",
                },
            }
        ],
        None,
    )
    ctx = _make_ctx(tmp_path)

    records, malformed = _collect_bucket(ctx, "cross-repo-memo", archived=False)

    assert malformed == []
    assert len(records) == 1
    assert "body" not in records[0]
