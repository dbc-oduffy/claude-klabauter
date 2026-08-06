"""Tests for coordinator_core.ops.measure_token_envelope.

Spec backlink: docs/plans/2026-07-27-doctrine-envelope-allocation.md § C1(a)(c)
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.measure_token_envelope import (
    estimate_tokens,
    main,
    measure_surface,
    measure_surfaces,
)


def test_estimate_tokens_empty_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_rounds_up():
    # 5 chars / 4 chars-per-token = 1.25 -> ceil to 2
    assert estimate_tokens("abcde") == 2


def test_estimate_tokens_exact_boundary():
    # exactly 4 chars per token -> no rounding needed
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


def test_measure_surface_missing_file_degrades_gracefully(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    result = measure_surface(missing)
    assert result == {"path": str(missing), "bytes": 0, "tokens": 0, "exists": False}


def test_measure_surface_existing_file(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("abcdefgh", encoding="utf-8")  # 8 bytes -> 2 tokens
    result = measure_surface(f)
    assert result["exists"] is True
    assert result["bytes"] == 8
    assert result["tokens"] == 2
    assert result["path"] == str(f)


def test_measure_surfaces_totals_and_order(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("abcd", encoding="utf-8")  # 4 bytes, 1 token
    b.write_text("abcdefgh", encoding="utf-8")  # 8 bytes, 2 tokens
    missing = tmp_path / "missing.md"

    result = measure_surfaces([a, missing, b])

    assert [r["path"] for r in result["surfaces"]] == [str(a), str(missing), str(b)]
    assert result["total_bytes"] == 12
    assert result["total_tokens"] == 3


def test_measure_surfaces_missing_contributes_zero_not_skewed(tmp_path):
    missing = tmp_path / "missing.md"
    result = measure_surfaces([missing])
    assert result["total_bytes"] == 0
    assert result["total_tokens"] == 0
    assert result["surfaces"][0]["exists"] is False


def test_main_no_args_is_usage_error(capsys):
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage:" in err


def test_main_prints_json_report(tmp_path, capsys):
    f = tmp_path / "CLAUDE.md"
    f.write_text("abcd", encoding="utf-8")

    rc = main([str(f)])
    out = capsys.readouterr().out
    assert rc == 0

    payload = json.loads(out)
    assert payload["total_bytes"] == 4
    assert payload["total_tokens"] == 1
    assert payload["surfaces"][0]["path"] == str(f)
