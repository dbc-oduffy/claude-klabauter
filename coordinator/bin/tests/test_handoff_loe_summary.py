"""test_handoff_loe_summary.py — unit tests for handoff-loe-summary.py, the
naked-Python port of the /handoff SKILL.md "Get LoE metrics" bash block
(2026-07 debash campaign, chunk HO-2).

Loaded by file path (`importlib.util.spec_from_file_location`) since the
module lives at `coordinator/bin/handoff-loe-summary.py` — hyphens in its
own filename preclude a dotted `import handoff_loe_summary`, so this suite
uses the identical file-path load idiom the module itself uses to pull in
its sibling `coordinator-session-loe.py`.

Covers:
  - `_loe_metrics` success path (fake coordinator-session-loe.py module)
  - `_loe_metrics` fallback-to-XS path on any exception (no repo / broken
    sibling module) — the "never fail a handoff over LoE" contract
  - `_recent_commits` git-failure fallback to ""
  - `main()` argv parsing end-to-end (JSON emitted on stdout, with LoE/
    commits/session-id/created all wired through)

Converted from a hand-rolled unittest runner to collectable pytest functions.

Spec backlink: archive/specs/2026-05/2026-05-19-completion-log-phase2-loe-and-handoff-ledger.md
  § Chunk 4.

Run:
    python3 -m pytest coordinator/bin/tests/test_handoff_loe_summary.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    path = _BIN_DIR / "handoff-loe-summary.py"
    spec = importlib.util.spec_from_file_location("handoff_loe_summary_test", str(path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def _fake_session_loe_module(*, git_root, ad, od, em_tokens, tshirt):
    fake = types.ModuleType("fake_coordinator_session_loe")
    fake._resolve_git_root = lambda: git_root
    fake._count_session = lambda sessions_base, sid: (ad, od)
    fake._sum_children = lambda sessions_base, sid, a, o: (a, o)
    fake._resolve_em_tokens = lambda: em_tokens
    fake._compute_tshirt = lambda a, o, t: tshirt
    return fake


# ---------------------------------------------------------------------------
# _loe_metrics — success path
# ---------------------------------------------------------------------------


def test_returns_computed_fields(tmp_path):
    """Real values flow through unmodified when the sibling module loads and
    computes cleanly."""
    fake = _fake_session_loe_module(
        git_root=str(tmp_path), ad=26, od=4, em_tokens=482000, tshirt="L"
    )
    with mock.patch.object(_mod, "_load_session_loe_module", return_value=fake):
        result = _mod._loe_metrics("some-session", include_children=False)
    assert result == {
        "agent_dispatches": 26, "opus_dispatches": 4, "em_tokens": 482000, "tshirt": "L"
    }


def test_none_dispatch_counts_normalize_to_zero(tmp_path):
    """_count_session returns (None, None) when dispatched-agents.txt is
    absent (null-honesty in the read helper) — the summary CLI still
    needs a concrete int for the ledger, matching the bash block's
    implicit `${AD:-0}`-shaped consumption."""
    fake = _fake_session_loe_module(
        git_root=str(tmp_path), ad=None, od=None, em_tokens=None, tshirt="XS"
    )
    with mock.patch.object(_mod, "_load_session_loe_module", return_value=fake):
        result = _mod._loe_metrics("some-session", include_children=False)
    assert result["agent_dispatches"] == 0
    assert result["opus_dispatches"] == 0


# ---------------------------------------------------------------------------
# _loe_metrics — fallback path
#
# Any failure computing real metrics degrades to the all-zero/XS fallback —
# mirrors the bash block's `... 2>/dev/null || echo '{...}'` error-handling
# ladder; a handoff must never fail to write over this.
# ---------------------------------------------------------------------------


def test_no_git_root_falls_back():
    fake = _fake_session_loe_module(
        git_root=None, ad=1, od=1, em_tokens=1, tshirt="S"
    )
    with mock.patch.object(_mod, "_load_session_loe_module", return_value=fake):
        result = _mod._loe_metrics("some-session", include_children=False)
    assert result == _mod._FALLBACK_LOE


def test_sibling_module_load_failure_falls_back():
    with mock.patch.object(
        _mod, "_load_session_loe_module", side_effect=ImportError("boom")
    ):
        result = _mod._loe_metrics("some-session", include_children=False)
    assert result == _mod._FALLBACK_LOE


# ---------------------------------------------------------------------------
# _recent_commits
# ---------------------------------------------------------------------------


def test_git_failure_returns_empty_string():
    with mock.patch.object(
        _mod.subprocess,
        "run",
        side_effect=OSError("git not found"),
    ):
        assert _mod._recent_commits(20) == ""


def test_nonzero_returncode_returns_empty_string():
    fake_result = subprocess.CompletedProcess(args=[], returncode=128, stdout="")
    with mock.patch.object(_mod.subprocess, "run", return_value=fake_result):
        assert _mod._recent_commits(20) == ""


def test_joins_sha_lines_space_separated():
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="abc1234\ndef5678\n"
    )
    with mock.patch.object(_mod.subprocess, "run", return_value=fake_result):
        assert _mod._recent_commits(20) == "abc1234 def5678"


# ---------------------------------------------------------------------------
# main() — end-to-end argv -> JSON stdout, with the session-id resolution
# ladder and _loe_metrics/_recent_commits/_utc_now_iso stubbed out so this
# suite never needs a real claude-klabauter checkout or git repo.
# ---------------------------------------------------------------------------


def test_explicit_session_id_skips_engine_session_id_resolution():
    """`--session-id` still bypasses `_resolve_session_id_via_engine` (the
    session-id ladder), but `_resolve_claude_klabauter_root` IS still called once —
    unconditionally now — to support the `oneline_row` formatter import."""
    with mock.patch.object(
        _mod, "_loe_metrics", return_value=_mod._FALLBACK_LOE
    ) as mock_loe, mock.patch.object(
        _mod, "_recent_commits", return_value="abc1234 def5678"
    ), mock.patch.object(
        _mod, "_utc_now_iso", return_value="2026-07-23T00:00:00Z"
    ), mock.patch.object(
        _mod, "_resolve_session_id_via_engine"
    ) as mock_session_engine, mock.patch.object(
        _mod, "_format_oneline_row", return_value="2026-07-23 | e-sid | XS | 0d / 0o | <one-line summary — fill in>"
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _mod.main(
                ["handoff-loe-summary.py", "--session-id", "explicit-sid", "--commit-limit", "5"]
            )
    assert rc == 0
    mock_session_engine.assert_not_called()
    mock_loe.assert_called_once_with("explicit-sid", False)
    payload = json.loads(buf.getvalue())
    assert payload == {
        "session_id": "explicit-sid",
        "agent_dispatches": 0,
        "opus_dispatches": 0,
        "em_tokens": None,
        "tshirt": "XS",
        "commits": "abc1234 def5678",
        "created": "2026-07-23T00:00:00Z",
        "oneline_row": "2026-07-23 | e-sid | XS | 0d / 0o | <one-line summary — fill in>",
    }


def test_no_session_id_falls_back_to_unknown_on_resolution_failure():
    with mock.patch.object(
        _mod, "_loe_metrics", return_value=_mod._FALLBACK_LOE
    ), mock.patch.object(
        _mod, "_recent_commits", return_value=""
    ), mock.patch.object(
        _mod, "_utc_now_iso", return_value="2026-07-23T00:00:00Z"
    ), mock.patch.object(
        _mod, "_resolve_claude_klabauter_root", side_effect=RuntimeError("no CLAUDE_KLABAUTER_ROOT")
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _mod.main(["handoff-loe-summary.py"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["session_id"] == "unknown"


def test_include_children_flag_forwarded(tmp_path):
    with mock.patch.object(
        _mod, "_loe_metrics", return_value=_mod._FALLBACK_LOE
    ) as mock_loe, mock.patch.object(
        _mod, "_recent_commits", return_value=""
    ), mock.patch.object(
        _mod, "_utc_now_iso", return_value="2026-07-23T00:00:00Z"
    ), mock.patch.object(
        _mod, "_resolve_claude_klabauter_root", return_value=str(tmp_path / "fake-claude-klabauter-live-root")
    ), mock.patch.object(
        _mod, "_format_oneline_row", return_value=None
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _mod.main(
                ["handoff-loe-summary.py", "--session-id", "sid", "--include-children"]
            )
    assert rc == 0
    mock_loe.assert_called_once_with("sid", True)


def test_missing_flag_value_is_usage_error():
    with redirect_stdout(io.StringIO()):
        rc = _mod.main(["handoff-loe-summary.py", "--session-id"])
    assert rc == 1


def test_invalid_commit_limit_is_usage_error():
    with redirect_stdout(io.StringIO()):
        rc = _mod.main(["handoff-loe-summary.py", "--commit-limit", "not-a-number"])
    assert rc == 1


def test_help_flag_exits_zero():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _mod.main(["handoff-loe-summary.py", "--help"])
    assert rc == 0
    assert "Usage: handoff-loe-summary.py" in buf.getvalue()


def test_unknown_argument_is_usage_error():
    with redirect_stdout(io.StringIO()):
        rc = _mod.main(["handoff-loe-summary.py", "--bogus"])
    assert rc == 1


# ---------------------------------------------------------------------------
# _format_oneline_row — best-effort import + delegation to
# aggregate_chain_loe.format_oneline_row (the single authoritative
# formatter; this CLI must not hand-format a second copy of the grammar).
# ---------------------------------------------------------------------------


def test_format_oneline_row_none_claude_klabauter_root_returns_none():
    assert _mod._format_oneline_row(None, "abc123def", "L", 5, 1, "2026-07-25T00:00:00Z") is None


def test_format_oneline_row_import_failure_returns_none(tmp_path):
    with mock.patch.object(_mod.sys, "path", list(_mod.sys.path)):
        with mock.patch(
            "builtins.__import__",
            side_effect=ImportError("no coordinator_core here"),
        ):
            result = _mod._format_oneline_row(
                str(tmp_path / "nonexistent" / "claude-klabauter" / "root"),
                "abc123def", "L", 5, 1, "2026-07-25T00:00:00Z"
            )
    assert result is None


def test_format_oneline_row_delegates_to_aggregate_chain_loe():
    """Real (unmocked) call against the actual claude-klabauter checkout — this repo
    IS claude-klabauter, so `coordinator_core` is genuinely importable here."""
    root = _mod._resolve_claude_klabauter_root()
    row = _mod._format_oneline_row(root, "abc123def456", "L", 26, 4, "2026-07-25T10:00:00Z")
    assert row == "2026-07-25 | def456 | L | 26d / 4o | <one-line summary — fill in>"


# ---------------------------------------------------------------------------
# Round-trip proof: a row this CLI emits must parse back via
# aggregate_chain_loe.parse_session_ledgers exactly as the field values
# that produced it — not eyeballed, asserted against the parsed record.
# ---------------------------------------------------------------------------


def test_oneline_row_round_trips_through_parse_session_ledgers():
    import importlib

    aggregate_chain_loe = importlib.import_module(
        "coordinator_core.session_ledger.aggregate_chain_loe"
    )

    root = _mod._resolve_claude_klabauter_root()
    row = _mod._format_oneline_row(root, "deadbeef01", "M", 12, 3, "2026-07-25T10:00:00Z")
    assert row is not None

    handoff_body = f"## Session Ledger\n\n{row}\n"
    records = aggregate_chain_loe.parse_session_ledgers(handoff_body)
    assert records == [
        {
            "session_id": "beef01",
            "agent_dispatches": "12",
            "opus_dispatches": "3",
            "em_tokens": "null",
            "commits": "",
            "created": "2026-07-25",
        }
    ]


# ---------------------------------------------------------------------------
# main() --row-only — prints ONLY the ready-to-paste row, no JSON wrapper.
# ---------------------------------------------------------------------------


def test_row_only_prints_bare_row():
    with mock.patch.object(
        _mod, "_loe_metrics", return_value=_mod._FALLBACK_LOE
    ), mock.patch.object(
        _mod, "_recent_commits", return_value="abc1234"
    ), mock.patch.object(
        _mod, "_utc_now_iso", return_value="2026-07-25T00:00:00Z"
    ), mock.patch.object(
        _mod,
        "_format_oneline_row",
        return_value="2026-07-25 | e-sid | XS | 0d / 0o | <one-line summary — fill in>",
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _mod.main(
                ["handoff-loe-summary.py", "--session-id", "explicit-sid", "--row-only"]
            )
    assert rc == 0
    assert buf.getvalue() == "2026-07-25 | e-sid | XS | 0d / 0o | <one-line summary — fill in>\n"


def test_row_only_exits_nonzero_when_row_unavailable():
    with mock.patch.object(
        _mod, "_loe_metrics", return_value=_mod._FALLBACK_LOE
    ), mock.patch.object(
        _mod, "_recent_commits", return_value=""
    ), mock.patch.object(
        _mod, "_utc_now_iso", return_value="2026-07-25T00:00:00Z"
    ), mock.patch.object(
        _mod, "_format_oneline_row", return_value=None
    ):
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf):
            with mock.patch.object(_mod.sys, "stderr", err):
                rc = _mod.main(
                    ["handoff-loe-summary.py", "--session-id", "sid", "--row-only"]
                )
    assert rc == 1
    assert buf.getvalue() == ""
    assert "format_oneline_row" in err.getvalue()
