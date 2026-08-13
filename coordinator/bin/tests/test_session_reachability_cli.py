"""test_session_reachability_cli.py — unit test for
coordinator/bin/session-reachability-cli.py, the CLI trampoline over
coordinator_core.session.reachability / peer_roster / artifact_owner.

Fixture-based via a stub of the CLI's own `_import_modules` seam -- this
suite never depends on this machine's real live peer list, and never
requires CLAUDE_KLABAUTER_ROOT to resolve. Uses `types.SimpleNamespace` fixtures
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

Spec backlink: cross-repo/inbox/2026-08-13-coordinator-claude-em-peer-roster-
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


def _candidate(session_id="peer-sid", name="peer-repo-ab12", ref="abcdef", address="peer-repo-ab12 [abcdef]"):
    return types.SimpleNamespace(session_id=session_id, name=name, ref=ref, address=address)


def _resolve_result(outcome, session_id=None, address=None, candidates=None):
    return types.SimpleNamespace(
        outcome=outcome, session_id=session_id, address=address, candidates=candidates or []
    )


@pytest.fixture()
def stub_import_modules():
    orig = _cli._import_modules

    def _apply(*, reachability=None, peer_roster=None, artifact_owner=None):
        _cli._import_modules = lambda: (reachability, peer_roster, artifact_owner)

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
    assert payload == {
        "outcome": "reachable",
        "session_id": "peer-sid",
        "address": "peer-repo-ab12 [abcdef]",
        "candidates": [],
    }


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
    result = _resolve_result("not_reachable")
    reachability = types.SimpleNamespace(resolve_address=lambda sid: result)
    stub_import_modules(reachability=reachability)

    rc = _cli.main(["resolve-address", "unknown-sid"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "not_reachable"


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
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_peer_roster_happy_path(stub_import_modules, capsys):
    captured = {}

    def _build_roster(repo_root):
        captured["repo_root"] = repo_root
        return [_peer_row()]

    peer_roster = types.SimpleNamespace(build_roster=_build_roster)
    stub_import_modules(peer_roster=peer_roster)

    rc = _cli.main(["peer-roster"])

    assert rc == 0
    assert captured["repo_root"] is None
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["session_id"] == "peer-sid"
    assert payload["rows"][0]["is_self"] is False


def test_peer_roster_repo_flag_forwards_verbatim(stub_import_modules, capsys):
    captured = {}

    def _build_roster(repo_root):
        captured["repo_root"] = repo_root
        return []

    peer_roster = types.SimpleNamespace(build_roster=_build_roster)
    stub_import_modules(peer_roster=peer_roster)

    rc = _cli.main(["peer-roster", "--repo", "/some/other/repo"])

    assert rc == 0
    assert captured["repo_root"] == "/some/other/repo"
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"rows": []}


def test_peer_roster_unknown_flag_exits_2(stub_import_modules):
    stub_import_modules(peer_roster=types.SimpleNamespace(build_roster=lambda repo_root: []))
    rc = _cli.main(["peer-roster", "--bogus"])
    assert rc == 2


def test_peer_roster_repo_flag_missing_value_exits_2(stub_import_modules):
    stub_import_modules(peer_roster=types.SimpleNamespace(build_roster=lambda repo_root: []))
    rc = _cli.main(["peer-roster", "--repo"])
    assert rc == 2


# ---------------------------------------------------------------------------
# artifact-owner
# ---------------------------------------------------------------------------

def test_artifact_owner_happy_path(stub_import_modules, capsys):
    owner_result = _resolve_result("reachable", session_id="owner-sid", address="owner-repo-ab12 [abcdef]")
    owner_record = types.SimpleNamespace(session_id="owner-sid", source_field="claimed_by")
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
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT not found")

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
    def _raise(repo_root):
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
