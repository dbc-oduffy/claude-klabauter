"""Two-box end-to-end: a Group EM on box A grants a session on box B through ``tier-u-grant-cli``;
box B verifies the relayed envelope against A's pinned key. Each leg runs under its own settings home.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import (
    box_key,
    box_registry,
    node_transport,
    nomination,
    session_registry,
    suite_grant,
)
from coordinator_core.ops import group_em_box_federation as op
from coordinator_core.session import grant as session_grant

HOLDER, PEER = "sid-holder", "sid-peer"
CLI = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "tier-u-grant-cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("tier_u_grant_cli_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._import_module = lambda: session_grant
    return mod


@pytest.fixture
def two_boxes(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    repo = tmp_path / "repo"
    repo.mkdir()
    id_a, id_b = box_registry.ensure_identity(home_a), box_registry.ensure_identity(home_b)
    dir_a, dir_b = home_a / "state" / "group-em", home_b / "state" / "group-em"
    box_registry.register(home_b, {"os": "linux"}, directory=dir_a, transport={"kind": "fs"})
    live = {HOLDER, PEER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: sid in live)
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [
            session_registry.RegistryRow(session_id=PEER, name="peer", pid=1, cwd=str(repo), status="")
        ],
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home_a))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    dir_a.mkdir(parents=True, exist_ok=True)
    nomination.claim(str(repo), HOLDER, directory=dir_a, prompt_id="p-1")
    return {"home_a": home_a, "home_b": home_b, "ida": id_a, "idb": id_b, "mp": monkeypatch, "repo": repo}


def _as_box(boxes, which, session):
    boxes["mp"].setenv("COORDINATOR_SETTINGS_HOME", str(boxes[f"home_{which}"]))
    boxes["mp"].setenv("COORDINATOR_SESSION_ID", session)


def _pin_a_on_b(boxes):
    _as_box(boxes, "a", HOLDER)
    key_b64 = base64.b64encode(box_key.ensure_key(boxes["home_a"])).decode()
    _as_box(boxes, "b", PEER)
    return op._groupem_box_key_pin({"box_id": boxes["ida"], "key_b64": key_b64})


def _relayed_file(boxes):
    return boxes["home_b"] / "state" / "group-em" / "suite-grants" / "relayed" / f"{PEER}.json"


def test_grant_on_a_for_a_session_on_b_reads_basis_grant_on_b(two_boxes, capsys):
    cli = _load_cli()
    assert _pin_a_on_b(two_boxes) == {"pinned": True, "box_id": two_boxes["ida"]}

    _as_box(two_boxes, "a", HOLDER)
    assert cli.main(["group-em-grant", f"{two_boxes['idb']}:{PEER}", "ruling"]) == 0
    assert _relayed_file(two_boxes).is_file()
    assert not (two_boxes["home_a"] / "state" / "group-em" / "suite-grants" / HOLDER).exists()
    capsys.readouterr()

    _as_box(two_boxes, "b", PEER)
    assert cli.main(["check"]) == 0
    assert "basis: group-em-grant" in capsys.readouterr().out


def test_without_a_pinned_key_the_grant_is_refused_and_b_reads_ungranted(two_boxes, capsys):
    cli = _load_cli()
    _as_box(two_boxes, "a", HOLDER)
    assert cli.main(["group-em-grant", f"{two_boxes['idb']}:{PEER}", "ruling"]) != 0
    assert not _relayed_file(two_boxes).exists()
    capsys.readouterr()

    _as_box(two_boxes, "b", PEER)
    assert cli.main(["check"]) != 0
    assert "basis:" not in capsys.readouterr().out


def _valid_envelope(boxes):
    _as_box(boxes, "a", HOLDER)
    issued = suite_grant.time.time()
    record = {
        "schema_version": suite_grant.SCHEMA_VERSION,
        "kind": suite_grant.KIND,
        "issuer_session_id": HOLDER,
        "issuer_name": "",
        "issuer_box_id": boxes["ida"],
        "grantee_session_id": PEER,
        "grantee_box_id": boxes["idb"],
        "grantee_name": "",
        "issued_at": suite_grant._iso(issued),
        "expires_at": suite_grant._iso(issued + 600),
        "note": "ruling",
    }
    return box_key.sign(record, boxes["home_a"])


def test_unsigned_and_forged_envelopes_are_rejected_by_the_op_and_write_nothing(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    envelope = _valid_envelope(two_boxes)
    _as_box(two_boxes, "b", PEER)

    unsigned = {k: v for k, v in envelope.items() if k != "sig"}
    tampered = {**envelope, "note": "altered after signing"}
    wrong_key = box_key.sign({k: v for k, v in envelope.items() if k != "sig"}, two_boxes["home_b"])

    for bad in (unsigned, tampered, wrong_key):
        reply = op._groupem_suite_grant_receive({"envelope": bad})
        assert reply == {"ok": False, "reason": "bad_signature"}
        assert not _relayed_file(two_boxes).exists()

    assert op._groupem_suite_grant_receive({"envelope": envelope}) == {"ok": True, "reason": "received"}
    assert _relayed_file(two_boxes).is_file()


def test_planted_relayed_file_with_a_bad_tag_reads_ungranted_on_b(two_boxes, capsys):
    cli = _load_cli()
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    forged = {**_valid_envelope(two_boxes), "sig": "0" * 64}
    target = _relayed_file(two_boxes)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"envelope": forged, "scope_root": str(two_boxes["repo"])}), encoding="utf-8")

    _as_box(two_boxes, "b", PEER)
    assert cli.main(["check"]) != 0
    assert "basis:" not in capsys.readouterr().out


def _signed(boxes, **override):
    base = {k: v for k, v in _valid_envelope(boxes).items() if k != "sig"}
    return box_key.sign({**base, **override}, boxes["home_a"])


_NOT_ABSOLUTE = suite_grant.SCOPE_ROOT_NOT_ABSOLUTE
_MSYS = "C:/Program Files/Git/Users/x/repo"  # abs-path-ok: MSYS fixture value


@pytest.mark.parametrize(
    "case",
    ["expired", "bad_ttl", "wrong_grantee_box", "grantee_not_live", "relative_root", "msys_root_on_posix"],
)
def test_receive_refuses_each_guard_reason_and_writes_nothing(two_boxes, case):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    if case in ("relative_root", "msys_root_on_posix"):
        two_boxes["mp"].setattr(suite_grant, "_path_flavour", lambda: suite_grant.PurePosixPath)
    now = suite_grant.time.time()
    iso = suite_grant._iso
    if case == "expired":
        envelope = _signed(two_boxes, issued_at=iso(now - 1200), expires_at=iso(now - 600))
    elif case == "bad_ttl":
        ceiling = suite_grant.TTL_CEILING_MINUTES * 60
        envelope = _signed(two_boxes, issued_at=iso(now), expires_at=iso(now + ceiling + 600))
    elif case == "wrong_grantee_box":
        envelope = _signed(two_boxes, grantee_box_id=two_boxes["ida"])
    elif case == "relative_root":
        envelope = _signed(two_boxes, scope_root="repo/sub")
    elif case == "msys_root_on_posix":
        envelope = _signed(two_boxes, scope_root=_MSYS)
    else:
        envelope = _signed(two_boxes, grantee_session_id="sid-not-live")
    expected = {
        "grantee_not_live": suite_grant.GRANTEE_NOT_LIVE,
        "relative_root": _NOT_ABSOLUTE,
        "msys_root_on_posix": _NOT_ABSOLUTE,
    }.get(case, case)
    _as_box(two_boxes, "b", PEER)
    reply = op._groupem_suite_grant_receive({"envelope": envelope})
    assert reply == {"ok": False, "reason": expected}
    assert not _relayed_file(two_boxes).exists()
    if expected == _NOT_ABSOLUTE:
        assert _audit_lines(two_boxes)[-1]["outcome"] == "refused"


def test_receive_refuses_a_relative_root_under_the_windows_flavour(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    two_boxes["mp"].setattr(suite_grant, "_path_flavour", lambda: suite_grant.PureWindowsPath)
    envelope = _signed(two_boxes, scope_root="repo\\sub")
    _as_box(two_boxes, "b", PEER)
    assert op._groupem_suite_grant_receive({"envelope": envelope}) == {"ok": False, "reason": _NOT_ABSOLUTE}
    assert not _relayed_file(two_boxes).exists()


def test_receive_accepts_a_drive_root_under_the_windows_flavour(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    two_boxes["mp"].setattr(suite_grant, "_path_flavour", lambda: suite_grant.PureWindowsPath)
    envelope = _signed(two_boxes, scope_root=_MSYS)
    _as_box(two_boxes, "b", PEER)
    assert op._groupem_suite_grant_receive({"envelope": envelope})["ok"] is True


def test_relayed_grant_outside_its_scope_root_reads_ungranted(two_boxes, capsys):
    cli = _load_cli()
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    envelope = _signed(two_boxes, scope_root=str(two_boxes["repo"] / "elsewhere"))
    target = _relayed_file(two_boxes)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"envelope": envelope, "scope_root": str(two_boxes["repo"])}), encoding="utf-8")
    _as_box(two_boxes, "b", PEER)
    assert cli.main(["check"]) != 0
    assert "basis:" not in capsys.readouterr().out


def _audit_lines(boxes):
    """Parsed lines of the far box's relay audit file."""
    path = boxes["home_b"] / "state" / "group-em" / suite_grant.AUDIT_FILE
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_far_box_audits_an_accepted_and_a_refused_arrival(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    envelope = _valid_envelope(two_boxes)
    _as_box(two_boxes, "b", PEER)
    assert op._groupem_suite_grant_receive({"envelope": {**envelope, "note": "tampered"}})["ok"] is False
    assert op._groupem_suite_grant_receive({"envelope": envelope})["ok"] is True
    refused, accepted = _audit_lines(two_boxes)
    assert (refused["outcome"], refused["reason"]) == ("refused", "bad_signature")
    assert accepted["outcome"] == "accepted"
    assert accepted["issuer_box_id"] == two_boxes["ida"]
    assert accepted["issuer_session_id"] == HOLDER
    assert accepted["grantee_session_id"] == PEER
    assert accepted["expires_at"] == envelope["expires_at"]
    assert accepted["basis"] == "box-key-hmac"


def test_refused_arrival_audit_is_bounded_and_carries_no_basis(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    envelope = _valid_envelope(two_boxes)
    _as_box(two_boxes, "b", PEER)
    forged = {**envelope, "issuer_session_id": "x" * 5000, "note": {"nested": 1}}
    assert op._groupem_suite_grant_receive({"envelope": forged})["ok"] is False
    (refused,) = _audit_lines(two_boxes)
    assert refused["outcome"] == "refused"
    assert len(refused["issuer_session_id"]) == suite_grant.AUDIT_FIELD_MAX
    assert refused["reason"] == "bad_signature"
    assert "basis" not in refused


def test_envless_relay_hop_grants_only_as_the_verified_live_holder(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    dir_a = two_boxes["home_a"] / "state" / "group-em"
    two_boxes["mp"].delenv("COORDINATOR_SESSION_ID", raising=False)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        two_boxes["mp"].delenv(var, raising=False)
    target = f"{two_boxes['idb']}:{PEER}"

    assert suite_grant.issue_suite_grant(target, "ruling", directory=dir_a).reason == suite_grant.ISSUER_UNRESOLVED
    non_holder = suite_grant.issue_suite_grant(target, "ruling", directory=dir_a, relay_issuer_sid=PEER)
    assert (non_holder.ok, non_holder.reason) == (False, suite_grant.NOT_GROUP_EM)
    local = suite_grant.issue_suite_grant(PEER, "ruling", directory=dir_a, relay_issuer_sid=HOLDER)
    assert local.reason == suite_grant.ISSUER_UNRESOLVED
    assert not _relayed_file(two_boxes).exists()

    ok = suite_grant.issue_suite_grant(target, "ruling", directory=dir_a, relay_issuer_sid=HOLDER)
    assert ok.ok is True
    assert _relayed_file(two_boxes).is_file()
    assert _audit_lines(two_boxes)[-1]["outcome"] == "accepted"


def test_relay_issuer_env_reaches_the_cli_path_and_keeps_the_holder_check(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    dir_a = two_boxes["home_a"] / "state" / "group-em"
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        two_boxes["mp"].delenv(var, raising=False)
    target = f"{two_boxes['idb']}:{PEER}"

    two_boxes["mp"].setenv(suite_grant.RELAY_ISSUER_ENV, PEER)
    refused = suite_grant.issue_suite_grant(target, "ruling", directory=dir_a)
    assert (refused.ok, refused.reason) == (False, suite_grant.NOT_GROUP_EM)
    assert not _relayed_file(two_boxes).exists()

    two_boxes["mp"].setenv(suite_grant.RELAY_ISSUER_ENV, HOLDER)
    assert suite_grant.issue_suite_grant(target, "ruling", directory=dir_a).ok is True
    assert _relayed_file(two_boxes).is_file()


RELAY_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "unimatrix-relay.py"


@pytest.fixture
def relay_route(two_boxes):
    """Box A's registry row for B is an ssh row with a relay key; the runner plays B's forced command."""
    from coordinator_core.group_em import box_transport

    spec = importlib.util.spec_from_file_location("unimatrix_relay_under_test", RELAY_BIN)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    dir_a = two_boxes["home_a"] / "state" / "group-em"
    box_registry.register(
        two_boxes["home_b"],
        {"os": "linux"},
        directory=dir_a,
        transport={"kind": "ssh", "host": "far-box", "relay_key": "/keys/unimatrix"},
    )
    box_transport._reset_limits()
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        two_boxes["mp"].delenv(var, raising=False)
    calls = []

    def runner(argv, timeout, stdin_text):
        calls.append(argv)
        parsed = relay.parse_mode(argv[-1])
        assert parsed is not None and parsed[0] == "grant"
        reply = relay.grant(
            parsed[1],
            stdin_text,
            receive=lambda env: suite_grant.receive_relayed_grant(env, home=two_boxes["home_b"]),
        )
        return subprocess.CompletedProcess(
            argv, 0 if reply.get("ok") is True else 1, json.dumps(reply) + "\n", ""
        )

    two_boxes["mp"].setattr(node_transport.SshTransport, "__init__", lambda self, r=None, directory=None: setattr(self, "runner", runner))
    return {**two_boxes, "dir_a": dir_a, "calls": calls, "runner": runner}


def test_holder_grant_reaches_b_through_the_relay_grant_mode_and_b_audits_it(relay_route):
    assert _pin_a_on_b(relay_route)["pinned"] is True
    _as_box(relay_route, "a", HOLDER)
    target = f"{relay_route['idb']}:{PEER}"
    result = suite_grant.issue_suite_grant(target, "ruling", directory=relay_route["dir_a"])
    assert (result.ok, result.reason) == (True, "issued")
    (argv,) = relay_route["calls"]
    assert argv[:3] == ["ssh", "-i", "/keys/unimatrix"] and argv[-2] == "far-box"
    assert argv[-1].startswith(f"grant {target} ruling 120 ")
    assert _relayed_file(relay_route).is_file()
    (line,) = _audit_lines(relay_route)
    assert (line["outcome"], line["issuer_session_id"], line["basis"]) == ("accepted", HOLDER, "box-key-hmac")


def test_non_holder_never_reaches_the_relay(relay_route):
    assert _pin_a_on_b(relay_route)["pinned"] is True
    _as_box(relay_route, "a", PEER)
    target = f"{relay_route['idb']}:{PEER}"
    result = suite_grant.issue_suite_grant(target, "ruling", directory=relay_route["dir_a"])
    assert (result.ok, result.reason) == (False, suite_grant.NOT_GROUP_EM)
    assert relay_route["calls"] == []
    assert not _relayed_file(relay_route).exists()


def test_unpinned_issuer_is_refused_on_b_through_the_relay_and_audited(relay_route):
    _as_box(relay_route, "a", HOLDER)
    target = f"{relay_route['idb']}:{PEER}"
    result = suite_grant.issue_suite_grant(target, "ruling", directory=relay_route["dir_a"])
    assert (result.ok, result.reason) == (False, f"{suite_grant.RELAY_FAILED}:bad_signature")
    assert not _relayed_file(relay_route).exists()
    assert _audit_lines(relay_route)[-1]["outcome"] == "refused"


def test_relay_transport_failure_and_unregistered_key_are_not_acceptances(relay_route):
    from coordinator_core.group_em import box_transport

    _as_box(relay_route, "a", HOLDER)
    box = next(b for b in box_registry.read_boxes(relay_route["dir_a"]) if b["box_id"] == relay_route["idb"])
    envelope = _valid_envelope(relay_route)
    down = box_transport.deliver_grant(
        box, envelope, runner=lambda argv, timeout, stdin: subprocess.CompletedProcess(argv, 255, "", "denied")
    )
    assert (down["ok"], down["reason"]) == (False, "ssh_exit_255")
    junk = box_transport.deliver_grant(
        box, envelope, runner=lambda argv, timeout, stdin: subprocess.CompletedProcess(argv, 0, "not json", "")
    )
    assert (junk["ok"], junk["reason"]) == (False, "bad_reply")
    forged_row = {**box, "transport": {**box["transport"], "relay_key": "/keys/other"}}
    assert box_transport.deliver_grant(forged_row, envelope, runner=relay_route["runner"])["reason"] == "box_not_registered"
    assert relay_route["calls"] == []


def test_harness_issuer_sid_reads_the_harness_ancestor_session_file(tmp_path, monkeypatch):
    import os

    from coordinator_core.session import core

    monkeypatch.setattr(core, "_is_harness_process", lambda name: True)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": HOLDER}), encoding="utf-8"
    )
    assert suite_grant.harness_issuer_sid(config_dir=tmp_path) == HOLDER
    (tmp_path / "sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": "../evil"}), encoding="utf-8"
    )
    assert suite_grant.harness_issuer_sid(config_dir=tmp_path) == ""


def test_cli_names_the_harness_session_as_relay_issuer_and_holder_check_still_refuses(two_boxes):
    assert _pin_a_on_b(two_boxes)["pinned"] is True
    _as_box(two_boxes, "a", HOLDER)
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        two_boxes["mp"].delenv(var, raising=False)
    two_boxes["mp"].setenv(suite_grant.RELAY_ISSUER_ENV, "")
    cli = _load_cli()
    target = f"{two_boxes['idb']}:{PEER}"

    two_boxes["mp"].setattr(suite_grant, "harness_issuer_sid", lambda *a, **k: PEER)
    assert cli.main(["group-em-grant", target, "ruling"]) != 0
    assert not _relayed_file(two_boxes).exists()

    two_boxes["mp"].setattr(suite_grant, "harness_issuer_sid", lambda *a, **k: HOLDER)
    assert cli.main(["group-em-grant", target, "ruling"]) == 0
    assert _relayed_file(two_boxes).is_file()


def test_harness_issuer_sid_is_empty_with_no_harness_ancestor(tmp_path, monkeypatch):
    from coordinator_core.session import core

    monkeypatch.setattr(core, "_is_harness_process", lambda name: False)
    assert suite_grant.harness_issuer_sid(start_pid=1, config_dir=tmp_path) == ""
