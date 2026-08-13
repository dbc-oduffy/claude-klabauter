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

Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md (WSC-3 chunk).

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


def _load_wsc_tail_module():
    """`wsc-tail.py` is the OTHER end of the tail-args splice — loaded the
    same by-path way (hyphenated filename) so a cross-parser flag mismatch
    is a test failure here rather than a silent argv drop at ceremony time."""
    spec = importlib.util.spec_from_file_location(
        "wsc_tail_test_module", _BIN_DIR / "wsc-tail.py"
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


def test_tail_args_parser_accepts_every_flag_the_directive_builder_emits():
    """Regression (example-retrieval-repo-em memo, 2026-07-28): the brief builder emitted
    `--scope-kind`, which this parser rejected -> exit 2 -> `d-run-wsc-tail`
    spliced an EMPTY argv, silently dropping ALL FIVE `--review-*` flags while
    the ceremony still reported success. Pins the two ends of that splice
    together, so a flag rename on either side fails here instead of in a live
    ceremony."""
    from coordinator_core.workstream_complete import directives_commit_tail

    directive = directives_commit_tail.build_close_tail_args_directive(
        {
            "review": {
                "sha_range": "abc..def",
                "reviewer": "code-reviewer",
                "scope": "diff",
                "verdict": "APPROVE",
                "diff_loc": "120",
                "scope_kind": "diff",
            }
        }
    )
    emitted_argv = directive["args"]
    assert "--review-scope-kind" in emitted_argv

    # Hop 1: the brief builder's argv must parse under `wsc-close tail-args`
    # (`args[0]` is the `tail-args` subcommand token itself).
    parsed = _wsc_close._build_parser().parse_args(emitted_argv)
    assert parsed.review_scope_kind == "diff"

    # Hop 2: what `tail-args` then prints on stdout is spliced verbatim into
    # `wsc-tail.py`'s own argv, so THAT parser must accept it too. Pinning
    # only hop 1 would still let a wsc-tail-side rename break the ceremony.
    spliced = _wsc_close._build_tail_args(
        deleted_paths=None,
        kept_entries=None,
        review_sha_range=parsed.review_sha_range,
        review_reviewer=parsed.review_reviewer,
        review_scope=parsed.review_scope,
        review_verdict=parsed.review_verdict,
        review_diff_loc=parsed.review_diff_loc,
        review_scope_kind=parsed.review_scope_kind,
    )
    # `--subject` is wsc-tail's own required base arg; at apply time the
    # spliced tokens are APPENDED to a base argv that already carries it.
    tail_args = _load_wsc_tail_module()._build_arg_parser().parse_args(
        ["--subject", "s", *spliced]
    )
    assert tail_args.review_scope_kind == "diff"


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
