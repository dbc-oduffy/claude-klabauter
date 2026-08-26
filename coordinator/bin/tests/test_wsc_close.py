"""test_wsc_close.py — unit tests for coordinator/bin/wsc-close.py.

Coverage:
  _build_tail_args (pure function, no I/O):
    - empty/absent inputs -> [] (no flags emitted)
    - --deleted-paths only, --kept-entries only, both
    - all five --review-* fields supplied -> emitted verbatim in order
    - partial --review-* supply -> ValueError naming the missing fields
      (mirrors wsc-tail.py's own _missing_review_trail_fields all-or-nothing
      contract rather than silently forwarding a set it would itself reject)
  archive-session subcommand argv wiring:
    - missing --sid -> exit 1, no call
    - archive() raising -> non-fatal, exit 0 (docstring contract)
    - archive() returning False -> non-fatal, exit 0
    - archive() returning True -> exit 0, called with the right sid

Module import: wsc-close.py is a hyphenated filename, loaded by file path
(same idiom as test_check_install_divergence.py / test-archive-stamp-cli-
ship-handoff.py in this same tests/ dir).

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 (WSC-3 chunk).

Run:
    python -m pytest coordinator/bin/tests/test_wsc_close.py -q
"""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_wsc_close_module():
    spec = importlib.util.spec_from_file_location(
        "wsc_close_test_module", _BIN_DIR / "wsc-close.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_wsc_close = _load_wsc_close_module()


# ---------------------------------------------------------------------------
# _build_tail_args
# ---------------------------------------------------------------------------


def test_all_absent_yields_no_flags():
    assert (
        _wsc_close._build_tail_args(
            deleted_paths=None,
            kept_entries=None,
            review_sha_range=None,
            review_reviewer=None,
            review_scope=None,
            review_verdict=None,
            review_diff_loc=None,
        )
        == []
    )


def test_empty_lists_yield_no_flags():
    assert (
        _wsc_close._build_tail_args(
            deleted_paths=[],
            kept_entries=[],
            review_sha_range=None,
            review_reviewer=None,
            review_scope=None,
            review_verdict=None,
            review_diff_loc=None,
        )
        == []
    )


def test_deleted_paths_only():
    out = _wsc_close._build_tail_args(
        deleted_paths=["a.md", "b.md"],
        kept_entries=None,
        review_sha_range=None,
        review_reviewer=None,
        review_scope=None,
        review_verdict=None,
        review_diff_loc=None,
    )
    assert out == ["--deleted-paths", "a.md", "b.md"]


def test_kept_entries_only():
    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=["x", "y", "z"],
        review_sha_range=None,
        review_reviewer=None,
        review_scope=None,
        review_verdict=None,
        review_diff_loc=None,
    )
    assert out == ["--kept-entries", "x", "y", "z"]


def test_deleted_and_kept_together_preserve_order():
    out = _wsc_close._build_tail_args(
        deleted_paths=["a.md"],
        kept_entries=["k1", "k2"],
        review_sha_range=None,
        review_reviewer=None,
        review_scope=None,
        review_verdict=None,
        review_diff_loc=None,
    )
    assert out == ["--deleted-paths", "a.md", "--kept-entries", "k1", "k2"]


def test_all_review_fields_supplied():
    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range="abc..def",
        review_reviewer="code-reviewer",
        review_scope="diff",
        review_verdict="APPROVE",
        review_diff_loc="120",
    )
    assert out == [
        "--review-sha-range", "abc..def",
        "--review-reviewer", "code-reviewer",
        "--review-scope", "diff",
        "--review-verdict", "APPROVE",
        "--review-diff-loc", "120",
    ]


def test_review_scope_kind_is_appended_when_supplied():
    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range="abc..def",
        review_reviewer="code-reviewer",
        review_scope="diff",
        review_verdict="APPROVE",
        review_diff_loc="120",
        review_scope_kind="diff",
    )
    assert out[-2:] == ["--review-scope-kind", "diff"]


def test_review_scope_kind_absent_does_not_trip_partial_supply():
    """It is the OPTIONAL sixth flag, outside the all-or-nothing five --
    omitting it must stay valid, never raise."""
    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range="abc..def",
        review_reviewer="code-reviewer",
        review_scope="diff",
        review_verdict="APPROVE",
        review_diff_loc="120",
    )
    assert "--review-scope-kind" not in out


def test_review_scope_kind_round_trips_through_wsc_close_own_parser():
    """Regression (example-retrieval-repo-em memo, 2026-07-28): the brief builder emitted
    `--scope-kind`, which `wsc-close tail-args`'s own parser rejected -> exit 2
    -> `d-run-wsc-tail` spliced an EMPTY argv, silently dropping ALL FIVE
    `--review-*` flags while the ceremony still reported success.

    Narrowed 2026-08-25 (C4/C12, DR-358): `directives_commit_tail.
    build_close_tail_args_directive` and `coordinator/bin/wsc-tail.py` — the
    directive-builder hop and the third argv hop this test used to also pin —
    were both retired; `d-close-tail-args` folds into `run_close_commit`'s own
    kwargs in-process, no CLI or directive layer above it. What survives here
    is the one hop that still exists: `wsc-close tail-args`'s own parser must
    still accept `--review-scope-kind` and `_build_tail_args` must still emit
    it, so a rename on either side of THIS boundary still fails loud."""
    parsed = _wsc_close._build_parser().parse_args(
        [
            "tail-args",
            "--review-sha-range", "abc..def",
            "--review-reviewer", "code-reviewer",
            "--review-scope", "diff",
            "--review-verdict", "APPROVE",
            "--review-diff-loc", "120",
            "--review-scope-kind", "diff",
        ]
    )
    assert parsed.review_scope_kind == "diff"

    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range=parsed.review_sha_range,
        review_reviewer=parsed.review_reviewer,
        review_scope=parsed.review_scope,
        review_verdict=parsed.review_verdict,
        review_diff_loc=parsed.review_diff_loc,
        review_scope_kind=parsed.review_scope_kind,
    )
    assert out[-2:] == ["--review-scope-kind", "diff"]


def test_reviewer_evidence_is_emitted_when_supplied():
    """Regression (DoE-claude memo 2026-08-13 § 2): `reviewer_evidence` was
    accepted by `decisions["review"]` but dropped by an intermediate argv
    layer, so the op-side delegate-reviewer gate never saw the correlation it
    checks.

    Narrowed 2026-08-25 (C4/C12, DR-358) — see
    `test_review_scope_kind_round_trips_through_wsc_close_own_parser`'s
    docstring for why: this now pins only `_build_tail_args`'s own emission,
    the one hop of the original three-hop splice still standing."""
    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range="abc..def",
        review_reviewer="code-reviewer",
        review_scope="chain",
        review_verdict="ok",
        review_diff_loc="120",
        review_reviewer_evidence="state/subagent-share/sid/coordinatorcode-reviewer-abc123.md",
    )
    assert out[-2:] == [
        "--review-reviewer-evidence",
        "state/subagent-share/sid/coordinatorcode-reviewer-abc123.md",
    ]


def test_reviewer_evidence_absent_does_not_trip_partial_supply():
    """Optional, outside the all-or-nothing five — same exemption
    `--review-scope-kind` has."""
    out = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range="abc..def",
        review_reviewer="code-reviewer",
        review_scope="diff",
        review_verdict="APPROVE",
        review_diff_loc="120",
    )
    assert "--review-reviewer-evidence" not in out


def test_reviewer_evidence_mixed_with_slices_is_refused():
    with pytest.raises(ValueError):
        _wsc_close._build_tail_args(
            deleted_paths=None,
            kept_entries=None,
            review_sha_range=None,
            review_reviewer=None,
            review_scope=None,
            review_verdict=None,
            review_diff_loc=None,
            review_slices=[
                {
                    "sha_range": "a1..a2",
                    "reviewer": "code-reviewer",
                    "scope": "chain",
                    "verdict": "ok",
                    "diff_loc": "10",
                }
            ],
            review_reviewer_evidence="state/subagent-share/sid/a.md",
        )


def test_slice_reviewer_evidence_is_carried_in_the_spliced_json():
    """Narrowed 2026-08-25 (C4/C12, DR-358) — see
    `test_review_scope_kind_round_trips_through_wsc_close_own_parser`'s
    docstring: `wsc-tail.py` (the parser this used to also round-trip
    through, via `_parse_review_slices`) is retired. What survives is
    `_build_tail_args`'s own JSON splice, still the only place this value is
    at risk of being dropped on this side of the boundary."""
    import json as _json

    evidence = "state/subagent-share/sid/a.md"
    spliced = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range=None,
        review_reviewer=None,
        review_scope=None,
        review_verdict=None,
        review_diff_loc=None,
        review_slices=[
            {
                "sha_range": "a1..a2",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "verdict": "ok",
                "diff_loc": "10",
                "reviewer_evidence": evidence,
            }
        ],
    )
    assert _json.loads(spliced[1])["reviewer_evidence"] == evidence


def test_partial_review_fields_raises_with_missing_named():
    with pytest.raises(ValueError) as exc_info:
        _wsc_close._build_tail_args(
            deleted_paths=None,
            kept_entries=None,
            review_sha_range="abc..def",
            review_reviewer="code-reviewer",
            review_scope=None,
            review_verdict=None,
            review_diff_loc=None,
        )
    msg = str(exc_info.value)
    assert "--review-scope" in msg
    assert "--review-verdict" in msg
    assert "--review-diff-loc" in msg
    assert "--review-sha-range" not in msg.split("missing:")[1]


def test_everything_together():
    out = _wsc_close._build_tail_args(
        deleted_paths=["d1"],
        kept_entries=["k1"],
        review_sha_range="r1",
        review_reviewer="r2",
        review_scope="r3",
        review_verdict="r4",
        review_diff_loc="r5",
    )
    assert out == [
        "--deleted-paths", "d1",
        "--kept-entries", "k1",
        "--review-sha-range", "r1",
        "--review-reviewer", "r2",
        "--review-scope", "r3",
        "--review-verdict", "r4",
        "--review-diff-loc", "r5",
    ]


# ---------------------------------------------------------------------------
# CLI wiring: tail-args subcommand end to end (stdout lines)
# ---------------------------------------------------------------------------


def test_cli_tail_args_stdout_lines():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _wsc_close.main(["tail-args", "--deleted-paths", "a.md", "b.md"])
    assert rc == 0
    assert buf.getvalue().splitlines() == ["--deleted-paths", "a.md", "b.md"]


def test_cli_tail_args_partial_review_exits_1():
    err = io.StringIO()
    with redirect_stderr(err):
        rc = _wsc_close.main(["tail-args", "--review-sha-range", "r1"])
    assert rc == 1
    assert "partial --review-*" in err.getvalue()


# ---------------------------------------------------------------------------
# archive-session subcommand
# ---------------------------------------------------------------------------


def test_archive_session_missing_sid_exits_1():
    err = io.StringIO()
    with redirect_stderr(err):
        rc = _wsc_close.main(["archive-session", "--sid", ""])
    assert rc == 1
    assert "--sid required" in err.getvalue()


def test_archive_session_success_calls_archive_with_sid():
    fake_scope_mod = mock.MagicMock()
    fake_scope_mod.archive = mock.MagicMock(return_value=True)

    with mock.patch.object(
        _wsc_close, "require_colocated_engine_on_path", return_value="/fake/claude-klabauter/root"
    ):
        with mock.patch.dict(
            sys.modules, {"coordinator_core.session.scope": fake_scope_mod}
        ):
            rc = _wsc_close.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    fake_scope_mod.archive.assert_called_once_with("sess-123")


def test_archive_session_false_return_is_non_fatal():
    fake_scope_mod = mock.MagicMock()
    fake_scope_mod.archive = mock.MagicMock(return_value=False)

    with mock.patch.object(
        _wsc_close, "require_colocated_engine_on_path", return_value="/fake/claude-klabauter/root"
    ):
        with mock.patch.dict(
            sys.modules, {"coordinator_core.session.scope": fake_scope_mod}
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = _wsc_close.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    assert "non-fatal" in err.getvalue()


def test_archive_session_raising_is_non_fatal():
    fake_scope_mod = mock.MagicMock()
    fake_scope_mod.archive = mock.MagicMock(side_effect=RuntimeError("boom"))

    with mock.patch.object(
        _wsc_close, "require_colocated_engine_on_path", return_value="/fake/claude-klabauter/root"
    ):
        with mock.patch.dict(
            sys.modules, {"coordinator_core.session.scope": fake_scope_mod}
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = _wsc_close.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    assert "boom" in err.getvalue()


def test_archive_session_claude_klabauter_root_unresolvable_is_non_fatal():
    with mock.patch.object(
        _wsc_close,
        "require_colocated_engine_on_path",
        side_effect=RuntimeError("no claude-klabauter checkout found"),
    ):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = _wsc_close.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    assert "no claude-klabauter checkout found" in err.getvalue()
