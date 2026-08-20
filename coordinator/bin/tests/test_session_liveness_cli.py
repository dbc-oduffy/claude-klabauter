"""test_session_liveness_cli.py — unit test for
coordinator/bin/session-liveness-cli's `session-live` subcommand (AC4/AC6,
docs/plans/2026-08-13-liveness-stops-conflating-dead-with-elsewhere.md § C2).

Pins the field-report's own negative controls (session a5efc7bc, unsolicited,
2026-08-13): an all-zeros sid, an ancient session dir, a live foreign-repo
session, and a live same-repo session. Each is fixture-based via a stub of
the CLI's own `_import_module` seam -- this suite never depends on this
machine's real live peer list, and never requires the engine root to resolve.

Asserted matrix (AC4: exit codes stay backward-compatible; every arm prints
an explicit verdict, none exits 0 silently):
    live in this repo         -> stdout "live (<basis>)"        -> exit 0
    live in another repo      -> stdout "live-elsewhere[: cwd]" -> exit 4
    dead (session dir, stale) -> stdout "dead (<basis>)"        -> exit 1
    unknown (no verdict)      -> stdout "unknown"               -> exit 1

Loaded by file path (`importlib.machinery.SourceFileLoader`) --
same load idiom as test_session_claim_cli.py's `_load_cli_module`.

Spec backlink: pln-the-liveness-surface-stops-ans-402265 § C2 / AC4 / AC6.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util

import pytest

from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "session_liveness_cli", str(_BIN_DIR / "session-liveness-cli.py")
    )
    spec = importlib.util.spec_from_loader("session_liveness_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _StubLiveness:
    """Stand-in for coordinator_core.session.liveness -- each attribute is a
    callable the test configures per-case; no live claude-klabauter import required."""

    def __init__(self, *, session_live=None, session_verdict=None):
        self.session_live = session_live or (lambda *a, **k: False)
        self.session_verdict = session_verdict or (lambda *a, **k: None)


@pytest.fixture()
def stub_import_module():
    orig = _cli._import_module

    def _apply(stub):
        _cli._import_module = lambda: stub

    yield _apply
    _cli._import_module = orig


# ---------------------------------------------------------------------------
# Negative controls (AC6), pinned in the field report's own shape.
# ---------------------------------------------------------------------------

def test_all_zeros_sid_unknown_exits_1(stub_import_module, capsys):
    # No session dir anywhere, no harness-registry record -> bare None.
    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: False, session_verdict=lambda *a, **k: None)
    )
    rc = _cli.main(["session-live", "00000000-0000-0000-0000-000000000000"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "unknown"


def test_ancient_session_dir_dead_exits_1(stub_import_module, capsys):
    # Session dir exists but is confirmed non-live -- a real verdict tuple
    # with a False bool, distinct from the no-verdict/unknown case above.
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (False, "recency-window", 999999),
        )
    )
    rc = _cli.main(["session-live", "ancient-sid"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "dead (recency-window)"


def test_live_foreign_repo_session_exits_4_distinct_from_dead(stub_import_module, capsys):
    # session_live() stays False (AC1: unchanged, unmigrated) even though
    # the session is live elsewhere -- C1's new state must be distinguishable
    # from "dead" at this CLI surface without touching that boolean.
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (True, "harness-registry-elsewhere", "/some/other/repo"),
        )
    )
    rc = _cli.main(["session-live", "peer-sid"])
    assert rc == _cli._EXIT_LIVE_ELSEWHERE
    assert rc not in (0, 1)
    out = capsys.readouterr().out.strip()
    assert out == "live-elsewhere: /some/other/repo"


def test_live_foreign_repo_session_no_cwd_still_reports_elsewhere(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (True, "harness-registry-elsewhere", None),
        )
    )
    rc = _cli.main(["session-live", "peer-sid-no-cwd"])
    assert rc == _cli._EXIT_LIVE_ELSEWHERE
    assert capsys.readouterr().out.strip() == "live-elsewhere"


def test_live_same_repo_session_exits_0_no_longer_silent(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: True,
            session_verdict=lambda *a, **k: (True, "stable-pid", None),
        )
    )
    rc = _cli.main(["session-live", "my-sid"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "live (stable-pid)"
    assert out != ""


def test_toctou_window_true_flagged_verdict_never_prints_dead(stub_import_module, capsys):
    # Review: staff-eng-review Finding 1 -- the two-read window between
    # session_live() and session_verdict() can leave `live` False while
    # `verdict` carries a True-flagged tuple on a non-elsewhere basis (e.g.
    # a session dir created, or Layer-2 recency crossing the threshold,
    # between the two calls). Printing "dead (stable-pid)" over that would
    # be a stdout/exit-code contradiction; the arm must fall through to
    # "unknown" instead.
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (True, "stable-pid", None),
        )
    )
    rc = _cli.main(["session-live", "toctou-sid"])
    assert rc == 1
    out = capsys.readouterr().out.strip()
    assert out == "unknown"
    assert "dead" not in out


# ---------------------------------------------------------------------------
# AC1 pin: exit-code contract for the live/dead booleans is unchanged.
# ---------------------------------------------------------------------------

def test_live_true_still_exits_0_when_verdict_raises(stub_import_module, capsys):
    def _raise(*a, **k):
        raise RuntimeError("verdict computation failed")

    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: True, session_verdict=_raise)
    )
    rc = _cli.main(["session-live", "some-sid"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "live (unknown)"


def test_dead_false_still_exits_1_when_verdict_raises(stub_import_module, capsys):
    def _raise(*a, **k):
        raise RuntimeError("verdict computation failed")

    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: False, session_verdict=_raise)
    )
    rc = _cli.main(["session-live", "some-sid"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "unknown"


def test_missing_sid_exits_2(stub_import_module):
    stub_import_module(_StubLiveness())
    rc = _cli.main(["session-live"])
    assert rc == 2


def test_no_output_case_is_gone_every_arm_prints(stub_import_module, capsys):
    """AC4's core assertion, stated directly: no arm leaves stdout empty."""
    cases = [
        (False, None),
        (False, (False, "recency-window", 5)),
        (False, (True, "harness-registry-elsewhere", "/peer/repo")),
        (True, (True, "stable-pid", None)),
    ]
    for live, verdict in cases:
        def _live(*a, _l=live, **k):
            return _l

        def _verdict(*a, _v=verdict, **k):
            return _v

        stub_import_module(_StubLiveness(session_live=_live, session_verdict=_verdict))
        _cli.main(["session-live", "some-sid"])
        assert capsys.readouterr().out.strip() != ""
