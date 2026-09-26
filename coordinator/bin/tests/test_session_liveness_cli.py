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


def test_all_zeros_sid_unknown_exits_1(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: False, session_verdict=lambda *a, **k: None)
    )
    rc = _cli.main(["session-live", "00000000-0000-0000-0000-000000000000"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "unknown"


def test_ancient_session_dir_dead_exits_1(stub_import_module, capsys):
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
