"""test_session_reachability_cli.py — unit test for
coordinator/bin/session-reachability-cli.py, the CLI trampoline over
coordinator_core.session.reachability / peer_roster / artifact_owner.

Fixture-based via a stub of the CLI's own `_import_modules` seam -- this
suite never depends on this machine's real live peer list, and never
requires the engine root to resolve. Uses `types.SimpleNamespace` fixtures
rather than the real dataclasses, so this suite pins the CLI's OWN
attribute-access contract (session_id/name/ref/address/... ) independently
of coordinator_core.session's internal dataclass shapes -- same
independence session-liveness-cli's own test suite has from
coordinator_core.session.liveness.

Asserted per subcommand: one happy-path case whose JSON stdout round-trips
through `json.loads` and carries the expected fields, one usage-error case
(exit 2), and (for resolve-address) the "own_session" outcome maps through
identically to "reachable" -- neither is special-cased.

Loaded by file path (`importlib.machinery.SourceFileLoader`) --
same load idiom as test_session_liveness_cli.py's `_load_cli_module`.

Spec backlink: cross-repo/inbox/2026-08-13-doe-claude-em-peer-roster-
doctrine-reply.md § Counter 1, state/handoffs/2026-08-13-session-owner-
reachability-registry.md.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import types

import pytest

from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "session_reachability_cli", str(_BIN_DIR / "session-reachability-cli.py")
    )
    spec = importlib.util.spec_from_loader("session_reachability_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

from coordinator_core.session import messaging_gate as _real_messaging_gate  # noqa: E402


def _candidate(session_id="peer-sid", name="peer-repo-ab12", ref="abcdef", address="peer-repo-ab12 [abcdef]"):
    return types.SimpleNamespace(session_id=session_id, name=name, ref=ref, address=address)


def _resolve_result(outcome, session_id=None, address=None, candidates=None, reason=None):
    return types.SimpleNamespace(
        outcome=outcome,
        session_id=session_id,
        address=address,
        candidates=candidates or [],
        reason=reason,
    )


@pytest.fixture()
def stub_import_modules():
    orig = _cli._import_modules

    def _apply(
        *,
        reachability=None,
        peer_roster=None,
        artifact_owner=None,
        messaging_gate=_real_messaging_gate,
    ):
        # `messaging_gate` defaults to the REAL module rather than a stub: it
        # is a pure read of a mapping this fixture does not touch, so no test
        # needs to fake it, and defaulting it to `None` would make every
        # existing resolve-address case fail on an unrelated attribute error.
        _cli._import_modules = lambda: (
            reachability,
            peer_roster,
            artifact_owner,
            messaging_gate,
        )

    yield _apply
    _cli._import_modules = orig


# ---------------------------------------------------------------------------
# resolve-address
# ---------------------------------------------------------------------------

def test_resolve_address_reachable_happy_path(stub_import_modules, capsys):
    result = _resolve_result("reachable", session_id="peer-sid", address="peer-repo-ab12 [abcdef]")
    reachability = types.SimpleNamespace(resolve_address=lambda sid: result)
    stub_import_modules(reachability=reachability)

    rc = _cli.main(["resolve-address", "peer-sid"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    gate = payload.pop("caller_messaging_gate")
    assert payload == {
        "outcome": "reachable",
        "session_id": "peer-sid",
        "address": "peer-repo-ab12 [abcdef]",
        "reason": None,
        "candidates": [],
    }
    # Popped rather than pinned to a value: the gate block is about the
    # CALLING session, so its `state` depends on the environment this suite
    # runs under, not on the stubbed resolver result. Its SHAPE is the
    # contract here; its values are pinned in
    # coordinator_core/session/tests/test_messaging_gate.py.
    assert set(gate) == {"state", "requested", "inbox_bound", "note"}


def test_resolve_address_own_session_maps_through_unchanged(stub_import_modules, capsys):
    result = _resolve_result("own_session", session_id="self-sid")
    reachability = types.SimpleNamespace(resolve_address=lambda sid: result)
    stub_import_modules(reachability=reachability)

    rc = _cli.main(["resolve-address", "self-sid"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "own_session"
    assert payload["session_id"] == "self-sid"


def test_resolve_address_not_reachable_exits_0(stub_import_modules, capsys):
    # not_reachable is a legitimate answer, not an error -- never a
    # nonzero exit (module header comment's exit-code table).
    result = _resolve_result("not_reachable", reason="no-live-record")
    reachability = types.SimpleNamespace(resolve_address=lambda sid: result)
    stub_import_modules(reachability=reachability)

    rc = _cli.main(["resolve-address", "unknown-sid"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "not_reachable"


def test_resolve_address_not_reachable_reasons_are_distinguishable(stub_import_modules, capsys):
    # The whole point of the `reason` slot on this surface: a session that
    # is live and busy but unaddressable must not print the same thing as
    # a session that does not exist. Both are `outcome == "not_reachable"`,
    # so `outcome` alone cannot carry the distinction -- the CLI's own
    # stdout has to.
    unaddressable = _resolve_result("not_reachable", reason="peer-messaging-unavailable")
    stub_import_modules(reachability=types.SimpleNamespace(resolve_address=lambda sid: unaddressable))
    assert _cli.main(["resolve-address", "live-but-unaddressable-sid"]) == 0
    live_out = capsys.readouterr().out
    assert json.loads(live_out)["reason"] == "peer-messaging-unavailable"

    absent = _resolve_result("not_reachable", reason="no-live-record")
    stub_import_modules(reachability=types.SimpleNamespace(resolve_address=lambda sid: absent))
    assert _cli.main(["resolve-address", "bogus-sid"]) == 0
    absent_out = capsys.readouterr().out
    assert json.loads(absent_out)["reason"] == "no-live-record"

    assert live_out != absent_out


def test_resolve_address_reason_is_passed_through_never_re_derived(stub_import_modules, capsys):
    # The resolver owns the classification. The CLI must not synthesize a
    # reason from `outcome`, nor tolerate its absence with a default --
    # a serializer reading `result.reason` through `getattr(..., None)`
    # would silently report "no reason" for a resolver that had one.
    result = _resolve_result("not_reachable", reason="peer-inbox-absent")
    stub_import_modules(reachability=types.SimpleNamespace(resolve_address=lambda sid: result))

    assert _cli.main(["resolve-address", "inbox-less-sid"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "peer-inbox-absent"

    no_reason_slot = types.SimpleNamespace(
        outcome="not_reachable", session_id=None, address=None, candidates=[]
    )
    stub_import_modules(
        reachability=types.SimpleNamespace(resolve_address=lambda sid: no_reason_slot)
    )
    with pytest.raises(AttributeError):
        _cli.main(["resolve-address", "some-sid"])


def test_resolve_address_ambiguous_carries_candidates(stub_import_modules, capsys):
    result = _resolve_result("ambiguous", candidates=[_candidate(), _candidate(session_id="peer-sid-2")])
    reachability = types.SimpleNamespace(resolve_address=lambda sid: result)
    stub_import_modules(reachability=reachability)

    rc = _cli.main(["resolve-address", "dup-sid"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "ambiguous"
    assert len(payload["candidates"]) == 2
    assert payload["candidates"][0]["session_id"] == "peer-sid"


def test_resolve_address_missing_arg_exits_2(stub_import_modules):
    stub_import_modules(reachability=types.SimpleNamespace(resolve_address=lambda sid: None))
    rc = _cli.main(["resolve-address"])
    assert rc == 2


# ---------------------------------------------------------------------------
# peer-roster
# ---------------------------------------------------------------------------

def _peer_row(**overrides):
    base = dict(
        session_id="peer-sid",
        address="peer-repo-ab12 [abcdef]",
        name="peer-repo-ab12",
        ref="abcdef",
        cwd="/repo/peer",
        status="running",
        running_seconds=12.5,
        is_self=False,
        self_determination="resolved",
        messaging_available=True,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_peer_roster_happy_path(stub_import_modules, capsys):
    captured = {}

    def _build_roster(repo_root, *, raise_on_failure=False):
        captured["repo_root"] = repo_root
        captured["raise_on_failure"] = raise_on_failure
        return [_peer_row()]

    peer_roster = types.SimpleNamespace(build_roster=_build_roster)
    stub_import_modules(peer_roster=peer_roster)

    rc = _cli.main(["peer-roster"])

    assert rc == 0
    assert captured["repo_root"] is None
    # `raise_on_failure=True` is what keeps an unreadable registry from
    # degrading to a roster indistinguishable from a genuinely empty one.
    assert captured["raise_on_failure"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["session_id"] == "peer-sid"
    assert payload["rows"][0]["is_self"] is False


def test_peer_roster_repo_flag_forwards_verbatim(stub_import_modules, capsys):
    captured = {}

    def _build_roster(repo_root, *, raise_on_failure=False):
        captured["repo_root"] = repo_root
        captured["raise_on_failure"] = raise_on_failure
        return []

    peer_roster = types.SimpleNamespace(build_roster=_build_roster)
    stub_import_modules(peer_roster=peer_roster)

    rc = _cli.main(["peer-roster", "--repo", "/some/other/repo"])

    assert rc == 0
    assert captured["repo_root"] == "/some/other/repo"
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"rows": []}


def test_peer_roster_rows_carry_messaging_available(stub_import_modules, capsys):
    # An `address: null` row with no other signal reads as "this peer is
    # gone". `messaging_available: false` is what tells the reader the
    # harness's cross-session inbox is unbound box-wide instead -- so it
    # has to reach the CLI's own stdout, on every row.
    rows = [
        _peer_row(session_id="peer-a", address=None, messaging_available=False),
        _peer_row(session_id="peer-b", address=None, messaging_available=False),
    ]
    stub_import_modules(
        peer_roster=types.SimpleNamespace(
            build_roster=lambda repo_root, *, raise_on_failure=False: rows
        )
    )

    assert _cli.main(["peer-roster"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["messaging_available"] for r in payload["rows"]] == [False, False]
    assert all(r["address"] is None for r in payload["rows"])
    # Negative-spec: not a per-row reachability claim. A `True` value with
    # an unresolved address is the per-peer case and must serialize just as
    # faithfully -- the CLI never reconciles the two into one field.
    stub_import_modules(
        peer_roster=types.SimpleNamespace(
            build_roster=lambda repo_root, *, raise_on_failure=False: [
                _peer_row(address=None, messaging_available=True)
            ]
        )
    )
    assert _cli.main(["peer-roster"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["messaging_available"] is True
    assert row["address"] is None


def test_peer_roster_row_serializer_carries_every_dataclass_field(stub_import_modules, capsys):
    # Guards the drop this CLI's separate serializer is prone to: it is a
    # hand-written mirror of the op veneer, so a field added to `PeerRow`
    # lands on the wire only if this dict is edited too.
    stub_import_modules(
        peer_roster=types.SimpleNamespace(
            build_roster=lambda repo_root, *, raise_on_failure=False: [_peer_row()]
        )
    )

    assert _cli.main(["peer-roster"]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert set(row) == {
        "session_id",
        "address",
        "name",
        "ref",
        "cwd",
        "status",
        "running_seconds",
        "is_self",
        "self_determination",
        "messaging_available",
    }


def test_peer_roster_unknown_flag_exits_2(stub_import_modules):
    stub_import_modules(peer_roster=types.SimpleNamespace(build_roster=lambda repo_root, *, raise_on_failure=False: []))
    rc = _cli.main(["peer-roster", "--bogus"])
    assert rc == 2


def test_peer_roster_repo_flag_missing_value_exits_2(stub_import_modules):
    stub_import_modules(peer_roster=types.SimpleNamespace(build_roster=lambda repo_root, *, raise_on_failure=False: []))
    rc = _cli.main(["peer-roster", "--repo"])
    assert rc == 2


# ---------------------------------------------------------------------------
# artifact-owner
# ---------------------------------------------------------------------------

def test_artifact_owner_happy_path(stub_import_modules, capsys):
    owner_result = _resolve_result("reachable", session_id="owner-sid", address="owner-repo-ab12 [abcdef]")
    owner_record = types.SimpleNamespace(
        session_id="owner-sid",
        source_field="claimed_by",
        claim_live=None,
        claim_stage=None,
    )
    owner_resolution = types.SimpleNamespace(owner=owner_record, result=owner_result)
    artifact_result = types.SimpleNamespace(
        artifact_path="state/handoffs/example.md", owners=[owner_resolution], file_error=None
    )
    artifact_owner = types.SimpleNamespace(resolve_artifact_owner=lambda path: artifact_result)
    stub_import_modules(artifact_owner=artifact_owner)

    rc = _cli.main(["artifact-owner", "state/handoffs/example.md"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifact_path"] == "state/handoffs/example.md"
    assert payload["file_error"] is None
    assert payload["owners"][0]["session_id"] == "owner-sid"
    assert payload["owners"][0]["source_field"] == "claimed_by"
    assert payload["owners"][0]["outcome"] == "reachable"
    assert payload["owners"][0]["address"] == "owner-repo-ab12 [abcdef]"


def test_artifact_owner_file_error_exits_0(stub_import_modules, capsys):
    # A read/parse failure degrades to owners=[] with file_error set --
    # never a nonzero exit (this is a read, not a resolution failure of
    # this trampoline itself).
    artifact_result = types.SimpleNamespace(
        artifact_path="nope.md", owners=[], file_error="[Errno 2] No such file or directory: 'nope.md'"
    )
    artifact_owner = types.SimpleNamespace(resolve_artifact_owner=lambda path: artifact_result)
    stub_import_modules(artifact_owner=artifact_owner)

    rc = _cli.main(["artifact-owner", "nope.md"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["owners"] == []
    assert payload["file_error"] is not None


def test_artifact_owner_missing_arg_exits_2(stub_import_modules):
    stub_import_modules(artifact_owner=types.SimpleNamespace(resolve_artifact_owner=lambda path: None))
    rc = _cli.main(["artifact-owner"])
    assert rc == 2


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------

def test_missing_subcommand_exits_2():
    rc = _cli.main([])
    assert rc == 2


def test_unknown_subcommand_exits_2(stub_import_modules):
    stub_import_modules()
    rc = _cli.main(["bogus-subcommand"])
    assert rc == 2


def test_help_flag_exits_0_without_importing():
    # --help must short-circuit before _import_modules is ever called --
    # asserted by NOT stubbing it and confirming no ImportError/attribute
    # error surfaces (mirrors session-liveness-cli's own --help contract).
    rc = _cli.main(["--help"])
    assert rc == 0


def test_transport_failure_maps_to_exit_3(stub_import_modules, monkeypatch):
    def _raise():
        raise RuntimeError("engine root not found")

    monkeypatch.setattr(_cli, "_import_modules", _raise)
    rc = _cli.main(["resolve-address", "some-sid"])
    assert rc == _cli._TRANSPORT_FAIL


def test_resolve_address_runtime_raise_maps_to_exit_3(stub_import_modules, capsys):
    # A runtime raise from the wrapped resolve_address call (e.g. a
    # harness_registry.snapshot() I/O error) is a state the module header's
    # exit-code table names exhaustively as _TRANSPORT_FAIL -- never an
    # uncaught traceback exiting 1 where JSON was promised on stdout.
    def _raise(sid):
        raise OSError("registry directory unreadable")

    reachability = types.SimpleNamespace(resolve_address=_raise)
    stub_import_modules(reachability=reachability)

    rc = _cli.main(["resolve-address", "some-sid"])

    assert rc == _cli._TRANSPORT_FAIL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "registry directory unreadable" in captured.err


def test_peer_roster_runtime_raise_maps_to_exit_3(stub_import_modules, capsys):
    def _raise(repo_root, *, raise_on_failure=False):
        raise OSError("registry directory unreadable")

    peer_roster = types.SimpleNamespace(build_roster=_raise)
    stub_import_modules(peer_roster=peer_roster)

    rc = _cli.main(["peer-roster"])

    assert rc == _cli._TRANSPORT_FAIL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "registry directory unreadable" in captured.err


def test_artifact_owner_runtime_raise_maps_to_exit_3(stub_import_modules, capsys):
    def _raise(path):
        raise OSError("registry directory unreadable")

    artifact_owner = types.SimpleNamespace(resolve_artifact_owner=_raise)
    stub_import_modules(artifact_owner=artifact_owner)

    rc = _cli.main(["artifact-owner", "some/path.md"])

    assert rc == _cli._TRANSPORT_FAIL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "registry directory unreadable" in captured.err


def test_resolve_address_carries_caller_messaging_gate(stub_import_modules, capsys):
    # A `not_reachable` / `peer-messaging-unavailable` pair reads identically
    # whether nothing on this box ever asked the harness to open its
    # cross-session inbox or whether this session asked and it did not open.
    # Only the second is a claude-klabauter defect, and only this block separates them
    # -- three repos read the collapsed rendering as "the remote GrowthBook
    # flag is still off" and one planned around a human relay on it.
    result = _resolve_result("not_reachable", reason="peer-messaging-unavailable")
    stub_import_modules(
        reachability=types.SimpleNamespace(resolve_address=lambda sid: result),
        messaging_gate=types.SimpleNamespace(
            classify=lambda: "gate-object",
            to_dict=lambda gate: {
                "state": "requested-unbound",
                "requested": True,
                "inbox_bound": False,
                "note": "n",
            },
        ),
    )

    assert _cli.main(["resolve-address", "live-but-unaddressable-sid"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["reason"] == "peer-messaging-unavailable"
    assert payload["caller_messaging_gate"]["state"] == "requested-unbound"


def test_caller_messaging_gate_is_serialized_by_the_owning_module(stub_import_modules, capsys):
    # Serialized through `messaging_gate.to_dict`, never hand-rolled here --
    # that is what keeps this trampoline and
    # coordinator_core/ops/session_resolve_address.py from drifting on the
    # payload shape, which the module header pins as a contract.
    seen = {}

    def _to_dict(gate):
        seen["gate"] = gate
        return {"state": "open", "requested": True, "inbox_bound": True, "note": "n"}

    stub_import_modules(
        reachability=types.SimpleNamespace(
            resolve_address=lambda sid: _resolve_result("reachable", session_id="s", address="a [b]")
        ),
        messaging_gate=types.SimpleNamespace(classify=lambda: "sentinel", to_dict=_to_dict),
    )

    assert _cli.main(["resolve-address", "s"]) == 0
    assert seen["gate"] == "sentinel"
    assert json.loads(capsys.readouterr().out)["caller_messaging_gate"]["state"] == "open"


def test_peer_roster_rows_do_not_carry_the_caller_gate(stub_import_modules, capsys):
    # The gate block is self-scoped. A roster row is about a PEER, whose
    # environment is not knowable from here -- attaching it there would be
    # the confidently-wrong shape reachability.py's Anti-scope forbids.
    peer_roster = types.SimpleNamespace(
        build_roster=lambda repo_root, *, raise_on_failure=False: [_peer_row()]
    )
    stub_import_modules(peer_roster=peer_roster)

    assert _cli.main(["peer-roster"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "caller_messaging_gate" not in payload["rows"][0]
