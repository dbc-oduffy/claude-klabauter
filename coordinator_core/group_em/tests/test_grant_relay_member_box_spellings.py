"""A grant to a Machine-b session resolves every grantee spelling to the membership box id before relay."""

import base64
import json

import pytest

from coordinator_core.group_em import (
    box_key,
    box_registry,
    member_box,
    node_transport,
    nomination,
    session_registry,
    suite_grant,
)

NOW = 1_800_000_000.0
HOLDER, SID = "sid-holder", "6e46574c-65ae-4c2f-a79e-fec5f2cfcc01"
MEMBERS = [
    {"box": "Machine-b", "host": "Machine-b.local", "user": "example-operator"},
    {"box": "mac-mini", "host": "mac-mini.local", "user": "example-operator"},
]


class _Mem:
    class MembershipError(Exception):
        pass

    def verified_members(self, home, run=None):
        return MEMBERS


class _Transport:
    def __init__(self):
        self.envelopes = []

    def deliver_grant(self, box, envelope):
        self.envelopes.append(envelope)
        return {"ok": True}


@pytest.fixture
def relay(tmp_path, monkeypatch):
    directory = tmp_path / "state" / "group-em"
    directory.mkdir(parents=True)
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") == HOLDER, "live")
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    nomination.claim(str(tmp_path), HOLDER, directory=directory, prompt_id="p-1")
    monkeypatch.setattr(member_box, "_membership", lambda: _Mem())
    transport = _Transport()
    monkeypatch.setattr(node_transport, "select", lambda box, directory=None: transport)
    seen = []

    def far_box(box_id, directory, home):
        seen.append(box_id)
        return {"box_id": box_id, "transport": {"kind": "ssh", "host": "alias"}}, ""

    monkeypatch.setattr(suite_grant, "_far_box", far_box)
    return {"dir": directory, "transport": transport, "dialled": seen}


def _grant(relay, grantee):
    return suite_grant.issue_suite_grant(grantee, "ruling", directory=relay["dir"], now=NOW)


@pytest.mark.parametrize(
    "form", ["Machine-b", "machine-b", "MACHINE-B", "Machine-b.local", "machine-b.local", "example-operator@Machine-b.local"]
)
def test_every_machine_b_form_relays_under_the_membership_box_id(relay, form):
    result = _grant(relay, f"{form}:{SID}")
    assert result.ok, result.reason
    assert relay["dialled"] == ["Machine-b"]
    assert [e["grantee_box_id"] for e in relay["transport"].envelopes] == ["Machine-b"]
    assert relay["transport"].envelopes[0]["grantee_session_id"] == SID


@pytest.mark.parametrize("form", ["stranger", "stranger.local", "wrong-user@Machine-b.local"])
def test_a_head_the_membership_does_not_name_is_left_as_typed(relay, form):
    assert suite_grant._member_host_key(f"{form}:{SID}", None) == f"{form}:{SID}"


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    """Issuer home A signs; receiver home B is a box whose box.json id is ``Machine-b``."""
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    repo = tmp_path / "repo"
    repo.mkdir()
    ida = box_registry.ensure_identity(home_a)
    box_registry._write(
        box_registry._identity_path(home_b), {"schema_version": 1, "box_id": "Machine-b", "minted_at": 0}
    )
    key_b64 = base64.b64encode(box_key.ensure_key(home_a)).decode()
    box_key.pin_peer_key(ida, key_b64, home_b)
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: sid == SID)
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [session_registry.RegistryRow(session_id=SID, name="d", pid=1, cwd=str(repo), status="")],
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home_b))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.chdir(repo)
    return {"a": home_a, "b": home_b, "ida": ida, "repo": repo}


def _envelope(r, grantee_box):
    record = {
        "schema_version": suite_grant.SCHEMA_VERSION,
        "kind": suite_grant.KIND,
        "issuer_session_id": HOLDER,
        "issuer_name": "",
        "issuer_box_id": r["ida"],
        "grantee_session_id": SID,
        "grantee_box_id": grantee_box,
        "grantee_name": "",
        "issued_at": suite_grant._iso(NOW),
        "expires_at": suite_grant._iso(NOW + 600),
        "note": "ruling",
        "issuer_key_fp": box_key.own_fingerprint(r["a"]),
    }
    return box_key.sign(record, r["a"])


@pytest.mark.parametrize("stamped", ["Machine-b", "machine-b", "MACHINE-B", "machine-b.local", "Machine-b.local"])
def test_receiver_accepts_an_alias_of_its_own_box_and_check_reads_granted(receiver, stamped):
    envelope = _envelope(receiver, stamped)
    assert suite_grant.receive_relayed_grant(envelope, now=NOW) == {"ok": True, "reason": "received"}
    granted, _record = suite_grant.check_group_em_suite_grant(
        str(receiver["repo"]), session_id=SID, repo_root=str(receiver["repo"]), now=NOW
    )
    assert granted is True


@pytest.mark.parametrize("stamped", ["mac-mini", "Machineb2", "other.local", "local", ""])
def test_receiver_still_refuses_another_box_and_check_reads_ungranted(receiver, stamped):
    envelope = _envelope(receiver, stamped)
    assert suite_grant.receive_relayed_grant(envelope, now=NOW) == {"ok": False, "reason": "wrong_grantee_box"}
    planted = suite_grant._relayed_path(SID, None, receiver["b"])
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(json.dumps({"envelope": envelope, "scope_root": str(receiver["repo"])}), encoding="utf-8")
    granted, _record = suite_grant.check_group_em_suite_grant(
        str(receiver["repo"]), session_id=SID, repo_root=str(receiver["repo"]), now=NOW
    )
    assert granted is False


def test_an_unconfigured_receiver_never_folds_an_alias(tmp_path):
    assert suite_grant.names_this_box("local", tmp_path) is True
    assert suite_grant.names_this_box("LOCAL", tmp_path) is False


def test_a_membership_failure_leaves_the_grantee_as_typed(relay, monkeypatch):
    class _Broken(_Mem):
        def verified_members(self, home, run=None):
            raise self.MembershipError("unpinned")

    monkeypatch.setattr(member_box, "_membership", lambda: _Broken())
    assert suite_grant._member_host_key(f"machine-b:{SID}", None) == f"machine-b:{SID}"
