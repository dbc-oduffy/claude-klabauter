import pytest

from coordinator_core.group_em import box_registry, box_transport, member_box


class _Mem:
    class MembershipError(Exception):
        pass

    def __init__(self, members=None, error=None):
        self.members, self.error = members or [], error
        self.calls = 0

    def verified_members(self, home, run=None):
        self.calls += 1
        if self.error:
            raise self.MembershipError(self.error)
        return list(self.members)


class _Autolink:
    def __init__(self, key="/keys/relay"):
        self.key = key

    def machine_local_key(self, roster, alias, run=None):
        return None

    def ssh_identity_file(self, roster, alias, run=None):
        return None

    def resolve_key(self, roster, alias, local_fn=None, ssh_fn=None):
        assert alias == "example-operator@machine-b.local"
        return self.key


MEMBER = {"box": "machine-b", "host": "machine-b.local", "user": "example-operator"}


@pytest.fixture
def wired(monkeypatch):
    def wire(members=(MEMBER,), error=None, key="/keys/relay"):
        mem = _Mem(list(members), error)
        monkeypatch.setattr(member_box, "_membership", lambda: mem)
        monkeypatch.setattr(member_box, "_autolink", lambda: _Autolink(key))
        monkeypatch.setattr(member_box, "_roster", lambda: object())
        return mem

    return wire


@pytest.mark.parametrize("token", ["machine-b", "example-operator@machine-b", "machine-b.local", "example-operator@machine-b.local", "Machine-b"])
def test_member_token_forms_register_an_ssh_row(tmp_path, wired, token):
    wired()
    out = member_box.resolve_member_box(token, home=tmp_path)
    assert out["ok"] is True
    assert out["box"]["box_id"] == "machine-b"
    assert out["box"]["transport"] == {"kind": "ssh", "host": "example-operator@machine-b.local", "relay_key": "/keys/relay"}
    directory = box_registry._group_em_dir(tmp_path)
    assert [b["box_id"] for b in box_registry.read_boxes(directory)] == ["machine-b"]


def test_registered_row_is_admitted_by_relay_registered(tmp_path, wired, monkeypatch):
    wired()
    box = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [box])
    assert box_transport._relay_registered(box, "example-operator@machine-b.local", "/keys/relay") is True


def test_unlisted_token_is_box_unknown_and_writes_nothing(tmp_path, wired):
    wired()
    out = member_box.resolve_member_box("nobody", home=tmp_path)
    assert out == {"ok": False, "reason": member_box.BOX_UNKNOWN}
    assert box_registry.read_boxes(box_registry._group_em_dir(tmp_path)) == []


def test_wrong_user_is_box_unknown(tmp_path, wired):
    wired()
    assert member_box.resolve_member_box("mallory@machine-b", home=tmp_path)["reason"] == member_box.BOX_UNKNOWN


def test_unverifiable_membership_is_named_and_writes_nothing(tmp_path, wired):
    wired(error="bad signature")
    out = member_box.resolve_member_box("machine-b", home=tmp_path)
    assert out == {"ok": False, "reason": "membership_unverified:bad signature"}
    assert box_registry.read_boxes(box_registry._group_em_dir(tmp_path)) == []


def test_no_snapshot_is_membership_unverified(tmp_path, wired):
    wired(members=())
    assert member_box.resolve_member_box("machine-b", home=tmp_path)["reason"] == "membership_unverified:no_snapshot"


def test_blank_token_is_box_unknown(tmp_path, wired):
    wired()
    assert member_box.resolve_member_box("  ", home=tmp_path)["reason"] == member_box.BOX_UNKNOWN
    assert member_box.resolve_member_box("@box", home=tmp_path)["reason"] == member_box.BOX_UNKNOWN


def test_reresolve_keeps_row_and_preserves_remote_shell(tmp_path, wired):
    wired()
    first = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    again = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    assert again["registered_at"] == first["registered_at"]
    directory = box_registry._group_em_dir(tmp_path)
    box_registry.register(
        tmp_path, {}, directory=directory, box_id="machine-b",
        transport={**first["transport"], "remote_shell": "cmd"},
    )
    third = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    assert third["transport"]["remote_shell"] == "cmd"


def test_key_change_rewrites_row(tmp_path, wired):
    wired()
    member_box.resolve_member_box("machine-b", home=tmp_path)
    wired(key="/keys/other")
    box = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    assert box["transport"]["relay_key"] == "/keys/other"
