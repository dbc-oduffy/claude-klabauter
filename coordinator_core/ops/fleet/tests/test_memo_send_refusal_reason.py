"""
Purpose: every memo.send refusal returns its reason in the `reason` envelope field,
which cc_invoke.route_mutation reads, so the reason survives the warm transport
(stderr / the warm diagnostic channel can be dropped).
"""

from __future__ import annotations

from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS
from coordinator_core.ops.fleet.memo_send import _MODE, _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_sender_git_repo,
    _write_draft,
)


def _send(tmp_path, monkeypatch, *, summary: str, dry_run: bool = False) -> dict:
    sender_repo = _make_sender_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {})))
    _write_draft(sender_repo, "over-cap", summary=summary)
    return _memo_send({"dry_run": dry_run, "topic": "over-cap"}, repo_root=sender_repo)


def test_over_cap_summary_refusal_names_the_reason_in_the_envelope(tmp_path, monkeypatch):
    result = _send(tmp_path, monkeypatch, summary="x" * (_SUMMARY_MAX_CHARS + 17))
    assert result["exit_code"] == 1
    assert result["mode"] == _MODE
    assert f"summary is {_SUMMARY_MAX_CHARS + 17} chars, cap is {_SUMMARY_MAX_CHARS}" in result["reason"]
    assert result["reason"].count("memo.send:") == 1


def test_summary_at_the_cap_is_not_refused_for_length(tmp_path, monkeypatch):
    result = _send(tmp_path, monkeypatch, summary="x" * _SUMMARY_MAX_CHARS, dry_run=True)
    assert "cap is" not in result.get("reason", "")


def test_param_and_setup_refusals_carry_reason(tmp_path):
    result = _memo_send({"dry_run": True, "topic": "x"}, repo_root=None)
    assert result["exit_code"] == 1
    assert "no repo_root supplied" in result["reason"]

    bad = _memo_send({"dry_run": "yes", "topic": "x"}, repo_root=None)
    assert "dry_run must be bool" in bad["reason"]


def test_refusal_keeps_the_frozen_envelope_fields(tmp_path, monkeypatch):
    result = _send(tmp_path, monkeypatch, summary="x" * (_SUMMARY_MAX_CHARS + 1))
    assert result["candidates"] == [] and result["acted"] == []
    assert result["skipped"] == [] and result["failed"] == []
    assert result["dry_run"] is False
