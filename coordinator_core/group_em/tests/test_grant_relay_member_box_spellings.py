"""A grant to a Machine-b session resolves every grantee spelling to the membership box id before relay."""

import pytest

from coordinator_core.group_em import member_box, node_transport, nomination, suite_grant

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


def test_a_membership_failure_leaves_the_grantee_as_typed(relay, monkeypatch):
    class _Broken(_Mem):
        def verified_members(self, home, run=None):
            raise self.MembershipError("unpinned")

    monkeypatch.setattr(member_box, "_membership", lambda: _Broken())
    assert suite_grant._member_host_key(f"machine-b:{SID}", None) == f"machine-b:{SID}"
