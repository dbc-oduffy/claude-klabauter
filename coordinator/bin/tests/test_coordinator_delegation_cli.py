"""test_coordinator_delegation_cli.py — unit test for
coordinator/bin/coordinator-delegation.py. Asserts the CLI's exit-code
contract and its own local validation (the 12h lease ceiling, the printed
ceiling sentence) in isolation from any live claude-klabauter checkout: the imported
`fleet_delegation` module is stubbed via a monkeypatch of the CLI's own
`_import_module` seam, and `--pid` resolution is stubbed via
`_resolve_designated` — same idiom as
coordinator/bin/tests/test_tier_u_grant_cli.py.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-delegation.py` doesn't sit on `sys.path` as an importable
module (hyphenated filename).

Spec backlink: docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-check.md § chunk C7
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_delegation_cli", str(_BIN_DIR / "coordinator-delegation.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_delegation_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_NEVER_DELEGABLE = frozenset(
    {
        "irreversible-action",
        "outward-facing-action",
        "scope-change",
        "deliverable-change",
        "product-direction",
    }
)


class _StubFleetDelegation:
    """Stand-in for coordinator_core.session.fleet_delegation."""

    def __init__(
        self,
        *,
        write_fleet_delegation=None,
        read_fleet_delegation=None,
        check_fleet_delegation=None,
        never_delegable=_NEVER_DELEGABLE,
        grant_file=None,
    ):
        self.write_fleet_delegation = write_fleet_delegation or (lambda **k: (True, None))
        self.read_fleet_delegation = read_fleet_delegation or (lambda: None)
        self.check_fleet_delegation = check_fleet_delegation or (lambda cls: (False, None))
        self.NEVER_DELEGABLE = never_delegable
        self._grant_file = grant_file or (lambda: None)


@pytest.fixture()
def stub_import_module():
    orig_import = _cli._import_module
    orig_resolve = _cli._resolve_designated

    def _apply(stub_mod, *, designated=(4242, 111.0)):
        _cli._import_module = lambda: stub_mod
        _cli._resolve_designated = lambda mod, pid_text: designated

    yield _apply
    _cli._import_module = orig_import
    _cli._resolve_designated = orig_resolve


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(argv)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# grant subcommand
# ---------------------------------------------------------------------------

def test_grant_true_exits_0(stub_import_module):
    stub_import_module(_StubFleetDelegation(write_fleet_delegation=lambda **k: (True, None)))
    rc, out = _run(
        ["grant", "--pid", "4242", "--classes", "review-schedule", "--lease-hours", "6", "--note", "ok"]
    )
    assert rc == 0
    assert _cli.CEILING_SENTENCE in out


def test_grant_false_exits_1(stub_import_module):
    stub_import_module(
        _StubFleetDelegation(write_fleet_delegation=lambda **k: (False, "authorship-refused"))
    )
    rc, out = _run(
        ["grant", "--pid", "4242", "--classes", "review-schedule", "--lease-hours", "6", "--note", "ok"]
    )
    assert rc == 1
    assert _cli.CEILING_SENTENCE in out


def test_grant_forwards_expected_fields(stub_import_module):
    seen = {}

    def _write(**k):
        seen.update(k)
        return True, None

    stub_import_module(_StubFleetDelegation(write_fleet_delegation=_write), designated=(99, 55.5))
    rc, _out = _run(
        ["grant", "--pid", "99", "--classes", "a,b", "--lease-hours", "1", "--note", "verbatim note"]
    )
    assert rc == 0
    assert seen["designated_pid"] == 99
    assert seen["designated_create_time"] == 55.5
    assert seen["classes"] == ["a", "b"]
    assert seen["granted_by"] == "human"
    assert seen["note"] == "verbatim note"
    assert seen["expires_at"] is not None
    assert seen["granted_at"] is not None


def test_grant_lease_over_ceiling_rejected_before_writer(stub_import_module):
    called = []
    stub_import_module(
        _StubFleetDelegation(write_fleet_delegation=lambda **k: called.append(k) or (True, None))
    )
    rc, _out = _run(
        ["grant", "--pid", "4242", "--classes", "a", "--lease-hours", "12.5", "--note", "ok"]
    )
    assert rc == 2
    assert called == []


def test_grant_never_delegable_class_rejected_before_writer(stub_import_module):
    called = []
    stub_import_module(
        _StubFleetDelegation(write_fleet_delegation=lambda **k: called.append(k) or (True, None))
    )
    rc, _out = _run(
        ["grant", "--pid", "4242", "--classes", "scope-change", "--lease-hours", "1", "--note", "ok"]
    )
    assert rc == 2
    assert called == []


def test_grant_unresolvable_pid_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation(), designated=None)
    rc, _out = _run(
        ["grant", "--pid", "999999", "--classes", "a", "--lease-hours", "1", "--note", "ok"]
    )
    assert rc == 2


def test_grant_missing_flags_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation())
    rc, _out = _run(["grant", "--pid", "4242"])
    assert rc == 2


def test_grant_no_args_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation())
    rc, _out = _run(["grant"])
    assert rc == 2


def test_grant_against_real_writer_reader_lands_live_grant(monkeypatch, tmp_path):
    """Exercises the REAL fleet_delegation writer/reader (not the stub) --
    review finding: `grant`'s own stub-based tests never prove a grant
    issued via the real CLI path actually lands a live,
    check_fleet_delegation-passing record on disk (the `revoke` path got
    this adversarial real-writer coverage; `grant` -- the path that
    actually creates the capability this CLI exists to gate -- did not).
    Drives `_cmd_grant` against the real writer with a real psutil-resolved
    `--pid` (this process), then asserts `check_fleet_delegation` reports
    the grant as live and routes to the designated pid."""
    from coordinator_core.session import fleet_delegation as fd
    from coordinator_core.session.grant_authorship import AuthorshipVerdict, Verdict

    monkeypatch.setattr(fd, "settings_home", lambda: tmp_path)
    monkeypatch.setattr(
        fd,
        "authorship_verdict",
        lambda start_pid=None: AuthorshipVerdict(Verdict.HUMAN, "test-fixture"),
    )

    import os

    orig_import = _cli._import_module
    _cli._import_module = lambda: fd
    try:
        rc, out = _run(
            [
                "grant",
                "--pid",
                str(os.getpid()),
                "--classes",
                "review-schedule",
                "--lease-hours",
                "1",
                "--note",
                "real-writer grant",
            ]
        )
    finally:
        _cli._import_module = orig_import
    assert rc == 0, out
    assert _cli.CEILING_SENTENCE in out

    granted, record = fd.check_fleet_delegation("review-schedule")
    assert granted is True
    assert record is not None
    assert record["designated"]["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# show subcommand
# ---------------------------------------------------------------------------

def test_show_no_grant(stub_import_module):
    stub_import_module(_StubFleetDelegation(read_fleet_delegation=lambda: None))
    rc, out = _run(["show"])
    assert rc == 0
    assert "no live grant" in out


def test_show_absent_via_check_prints_no_live_grant(stub_import_module):
    record = {"classes": ["a"], "designated": {"pid": 1, "create_time": 2.0}}
    stub_import_module(
        _StubFleetDelegation(
            read_fleet_delegation=lambda: record,
            check_fleet_delegation=lambda cls: (False, record),
        )
    )
    rc, out = _run(["show"])
    assert rc == 0
    assert "no live grant" in out


def test_show_live_grant_prints_fields(stub_import_module):
    record = {
        "designated": {"pid": 1, "create_time": 2.0},
        "classes": ["a"],
        "granted_at": "2026-08-29T00:00:00Z",
        "expires_at": "2026-08-29T06:00:00Z",
        "granted_by": "human",
        "note": "sometimes setup",
    }
    stub_import_module(
        _StubFleetDelegation(
            read_fleet_delegation=lambda: record,
            check_fleet_delegation=lambda cls: (True, record),
        )
    )
    rc, out = _run(["show"])
    assert rc == 0
    assert "sometimes setup" in out
    assert "no live grant" not in out


def test_show_extra_args_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation())
    rc, _out = _run(["show", "unexpected"])
    assert rc == 2


def test_show_multi_class_record_probes_first_class_but_matches_any_class(monkeypatch, tmp_path):
    """`_cmd_show` probes `check_fleet_delegation` with only `classes[0]` --
    review finding: unverified whether a live grant could ever be
    misreported as absent because the FIRST listed class alone were
    rejected while a LATER class in the same record was actually live.
    Exercises the REAL `fleet_delegation.write_fleet_delegation` /
    `check_fleet_delegation` (not the stub) with a genuine multi-class
    record: `check_fleet_delegation`'s liveness/expiry/authorship checks
    are record-level, so a live record must grant EVERY class in
    `classes`, not just the first -- this proves the CLI's classes[0]
    probe choice does not matter for the outcome `show` reports, rather
    than merely asserting it against a same-verdict-for-every-class stub."""
    import os

    from datetime import datetime, timedelta, timezone

    import psutil

    from coordinator_core.session import fleet_delegation as fd
    from coordinator_core.session.grant_authorship import AuthorshipVerdict, Verdict

    monkeypatch.setattr(fd, "settings_home", lambda: tmp_path)
    monkeypatch.setattr(
        fd,
        "authorship_verdict",
        lambda start_pid=None: AuthorshipVerdict(Verdict.HUMAN, "test-fixture"),
    )

    this_proc = psutil.Process(os.getpid())
    now = datetime.now(timezone.utc)
    ok, reason = fd.write_fleet_delegation(
        designated_pid=os.getpid(),
        designated_create_time=this_proc.create_time(),
        classes=["z-class", "a-class"],
        granted_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        granted_by="human",
        note="multi-class",
    )
    assert ok, reason

    granted_first, _ = fd.check_fleet_delegation("z-class")
    granted_second, _ = fd.check_fleet_delegation("a-class")
    assert granted_first is True
    assert granted_second is True

    orig_import = _cli._import_module
    _cli._import_module = lambda: fd
    try:
        rc, out = _run(["show"])
    finally:
        _cli._import_module = orig_import
    assert rc == 0
    assert "no live grant" not in out
    assert "multi-class" in out


# ---------------------------------------------------------------------------
# revoke subcommand
# ---------------------------------------------------------------------------

def test_revoke_unlinks_existing_grant_file_exits_0(stub_import_module, tmp_path):
    grant_file = tmp_path / "fleet-delegation.json"
    grant_file.write_text("{}", encoding="utf-8")
    stub_import_module(_StubFleetDelegation(grant_file=lambda: grant_file))
    rc, _out = _run(["revoke"])
    assert rc == 0
    assert not grant_file.exists()


def test_revoke_absent_grant_file_is_idempotent_exits_0(stub_import_module, tmp_path):
    grant_file = tmp_path / "fleet-delegation.json"
    assert not grant_file.exists()
    stub_import_module(_StubFleetDelegation(grant_file=lambda: grant_file))
    rc, _out = _run(["revoke"])
    assert rc == 0


def test_revoke_oserror_exits_1(stub_import_module, monkeypatch):
    class _UnlinkFails:
        def unlink(self, missing_ok=False):
            raise OSError("permission denied")

    stub_import_module(_StubFleetDelegation(grant_file=lambda: _UnlinkFails()))
    rc, _out = _run(["revoke"])
    assert rc == 1


def test_revoke_extra_args_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation())
    rc, _out = _run(["revoke", "unexpected"])
    assert rc == 2


def test_revoke_against_real_writer_reader_clears_live_grant(monkeypatch, tmp_path):
    """Exercises the REAL fleet_delegation writer/reader (not the stub) —
    writes a live, unexpired, HUMAN-authored grant on disk via
    write_fleet_delegation, revokes it through the CLI's real `_grant_file()`
    seam, then asserts check_fleet_delegation returns the ABSENT value
    post-revoke. This is what `stub_import_module`-only coverage cannot
    catch: the stub's fake writer has no `granted_at` tolerance check, so a
    back-dated revoke record that the REAL writer rejects would still read
    as a passing test against the stub alone."""
    from datetime import datetime, timedelta, timezone

    from coordinator_core.session import fleet_delegation as fd
    from coordinator_core.session.grant_authorship import AuthorshipVerdict, Verdict

    monkeypatch.setattr(fd, "settings_home", lambda: tmp_path)
    monkeypatch.setattr(
        fd,
        "authorship_verdict",
        lambda start_pid=None: AuthorshipVerdict(Verdict.HUMAN, "test-fixture"),
    )

    now = datetime.now(timezone.utc)
    ok, reason = fd.write_fleet_delegation(
        designated_pid=1234,
        designated_create_time=1000.5,
        classes=["some-class"],
        granted_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        granted_by="human",
        note="pm said so",
    )
    assert ok, reason
    assert fd.read_fleet_delegation() is not None

    orig_import = _cli._import_module
    _cli._import_module = lambda: fd
    try:
        rc, _out = _run(["revoke"])
    finally:
        _cli._import_module = orig_import
    assert rc == 0

    granted, record = fd.check_fleet_delegation("some-class")
    assert granted is False
    assert record is None


# ---------------------------------------------------------------------------
# usage / transport
# ---------------------------------------------------------------------------

def test_no_subcommand_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation())
    rc, _out = _run([])
    assert rc == 2


def test_unknown_subcommand_exits_2(stub_import_module):
    stub_import_module(_StubFleetDelegation())
    rc, _out = _run(["frobnicate"])
    assert rc == 2


def test_help_exits_0():
    orig_import = _cli._import_module
    _cli._import_module = lambda: (_ for _ in ()).throw(AssertionError("should not import"))
    try:
        rc, out = _run(["--help"])
    finally:
        _cli._import_module = orig_import
    assert rc == 0
    assert "usage" in out


def test_transport_failure_on_unresolvable_engine_root():
    orig_import = _cli._import_module

    def _raise():
        raise RuntimeError("engine root not found")

    _cli._import_module = _raise
    try:
        rc, _out = _run(["show"])
    finally:
        _cli._import_module = orig_import
    assert rc == 3
