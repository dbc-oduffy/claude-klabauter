import pytest

from coordinator_core.group_em import box_registry, box_transport, member_box
from pathlib import Path


class _Mem:
    class MembershipError(Exception):
        pass

    def __init__(self, members=None, error=None) -> None:
        self.members, self.error = members or [], error
        self.calls = 0

    def verified_members(self, home, run=None):
        self.calls += 1
        if self.error:
            raise self.MembershipError(self.error)
        return list(self.members)


class _Autolink:
    def __init__(self, key: str = "/keys/relay") -> None:
        self.key = key

    def machine_local_key(self, roster, alias, run=None) -> None:
        return None

    def ssh_identity_file(self, roster, alias, run=None) -> None:
        return None

    def resolve_key(self, roster, alias, local_fn=None, ssh_fn=None) -> str:
        assert alias == "example-operator@machine-b.local"
        return self.key


MEMBER = {"box": "machine-b", "host": "machine-b.local", "user": "example-operator"}


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def wire(members=(MEMBER,), error=None, key: str = "/keys/relay") -> _Mem:
        mem = _Mem(list(members), error)
        monkeypatch.setattr(member_box, "_membership", lambda: mem)
        monkeypatch.setattr(member_box, "_autolink", lambda: _Autolink(key))
        monkeypatch.setattr(member_box, "_roster", lambda: object())
        return mem

    return wire


@pytest.mark.parametrize(
    ("name", "folded"),
    [
        ("Machine-b", "machine-b"),
        ("machine-b.local", "machine-b"),
        ("Machine-b-LAN", "machine-b"),
        ("machine-b-lan.local", "machine-b"),
        ("mac-mini", "mac-mini"),
        ("machine-b-lan2", "machine-b-lan2"),
        ("-lan", "-lan"),
        (".local", ".local"),
    ],
)
def test_fold_box_id_strips_only_case_dot_local_and_dash_lan(name, folded) -> None:
    assert box_registry.fold_box_id(name) == folded


def test_same_box_is_the_fold_equality() -> None:
    assert box_registry.same_box("Machine-b.local", "machine-b-lan")
    assert not box_registry.same_box("machine-b", "machine_b2")
    assert not box_registry.same_box("machine-b", None)


@pytest.mark.parametrize("token", ["machine-b-lan", "Machine-b-LAN", "example-operator@machine-b-lan"])
def test_lan_spelling_resolves_to_the_member_box_id(tmp_path: Path, wired, token) -> None:
    wired()
    assert member_box.member_box_id(token, home=tmp_path) == "machine-b"


@pytest.mark.parametrize("token", ["machine-b", "example-operator@machine-b", "machine-b.local", "example-operator@machine-b.local", "Machine-b"])
def test_member_token_forms_register_an_ssh_row(tmp_path: Path, wired, token) -> None:
    wired()
    out = member_box.resolve_member_box(token, home=tmp_path)
    assert out["ok"] is True
    assert out["box"]["box_id"] == "machine-b"
    assert out["box"]["transport"] == {"kind": "ssh", "host": "example-operator@machine-b.local", "relay_key": "/keys/relay"}
    directory = box_registry._group_em_dir(tmp_path)
    assert [b["box_id"] for b in box_registry.read_boxes(directory)] == ["machine-b"]


def test_registered_row_is_admitted_by_relay_registered(tmp_path: Path, wired, monkeypatch: pytest.MonkeyPatch) -> None:
    wired()
    box = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [box])
    assert box_transport._relay_registered(box, "example-operator@machine-b.local", "/keys/relay") is True


def test_unlisted_token_is_box_unknown_and_writes_nothing(tmp_path: Path, wired) -> None:
    wired()
    out = member_box.resolve_member_box("nobody", home=tmp_path)
    assert out == {"ok": False, "reason": member_box.BOX_UNKNOWN}
    assert box_registry.read_boxes(box_registry._group_em_dir(tmp_path)) == []


def test_wrong_user_is_box_unknown(tmp_path: Path, wired) -> None:
    wired()
    assert member_box.resolve_member_box("mallory@machine-b", home=tmp_path)["reason"] == member_box.BOX_UNKNOWN


def test_unverifiable_membership_is_named_and_writes_nothing(tmp_path: Path, wired) -> None:
    wired(error="bad signature")
    out = member_box.resolve_member_box("machine-b", home=tmp_path)
    assert out == {"ok": False, "reason": "membership_unverified:bad signature"}
    assert box_registry.read_boxes(box_registry._group_em_dir(tmp_path)) == []


def test_no_snapshot_is_membership_unverified(tmp_path: Path, wired) -> None:
    wired(members=())
    assert member_box.resolve_member_box("machine-b", home=tmp_path)["reason"] == "membership_unverified:no_snapshot"


def test_blank_token_is_box_unknown(tmp_path: Path, wired) -> None:
    wired()
    assert member_box.resolve_member_box("  ", home=tmp_path)["reason"] == member_box.BOX_UNKNOWN
    assert member_box.resolve_member_box("@box", home=tmp_path)["reason"] == member_box.BOX_UNKNOWN


def test_reresolve_keeps_row_and_preserves_remote_shell(tmp_path: Path, wired) -> None:
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


RENAMED = {"box": "machine-a", "host": "machine-a.local", "user": "example-operator"}


def _registry_row(home: Path, box_id: str, name: str, prior: list) -> None:
    box_registry._write(
        box_registry._boxes_dir(box_registry._group_em_dir(home)) / (box_id + ".json"),
        {"schema_version": 1, "kind": box_registry.KIND, "box_id": box_id, "root": str(home),
         "capability": {"name": name}, "registered_at": 1.0, "names": prior},
    )


@pytest.mark.parametrize("token", ["box-df624266", "Gypsy", "example-operator@gypsy", "machine-a", "MACHINE-A.local"])
def test_renamed_box_resolves_by_stable_id_current_and_prior_name(tmp_path: Path, wired, monkeypatch, token) -> None:
    mem = wired(members=(RENAMED,))
    monkeypatch.setattr(member_box, "_autolink", lambda: _Autolink())
    monkeypatch.setattr(_Autolink, "resolve_key", lambda self, roster, alias, local_fn=None, ssh_fn=None: "/keys/relay")
    _registry_row(tmp_path, "box-df624266", "Gypsy", ["machine-a"])
    assert member_box.member_box_id(token, home=tmp_path) == "machine-a"
    out = member_box.resolve_member_box(token, home=tmp_path)
    assert out["ok"] is True and out["box"]["box_id"] == "machine-a"
    assert mem.calls == 2


def test_renamed_box_tokens_stay_unknown_without_a_linking_registry_row(tmp_path: Path, wired) -> None:
    wired(members=(RENAMED,))
    _registry_row(tmp_path, "box-65fdb5b0", "Mac-mini", [])
    for token in ("box-df624266", "Gypsy", "box-65fdb5b0", "Mac-mini"):
        assert member_box.member_box_id(token, home=tmp_path) is None
        assert member_box.resolve_member_box(token, home=tmp_path)["reason"] == member_box.BOX_UNKNOWN


def test_mini_legacy_id_requires_signed_member_and_matching_user(tmp_path: Path, wired) -> None:
    mini = {"box": "Mac-mini", "host": "Mac-mini.local", "user": "example-operator"}
    wired(members=(mini,))
    assert member_box.member_box_id("box-65fdb5b0", home=tmp_path) == "Mac-mini"
    assert member_box.member_box_id("mallory@box-65fdb5b0", home=tmp_path) is None


def test_registry_alias_does_not_bypass_the_user_check(tmp_path: Path, wired) -> None:
    wired(members=(RENAMED,))
    _registry_row(tmp_path, "box-df624266", "Gypsy", ["machine-a"])
    assert member_box.member_box_id("mallory@gypsy", home=tmp_path) is None


def test_key_change_rewrites_row(tmp_path: Path, wired) -> None:
    wired()
    member_box.resolve_member_box("machine-b", home=tmp_path)
    wired(key="/keys/other")
    box = member_box.resolve_member_box("machine-b", home=tmp_path)["box"]
    assert box["transport"]["relay_key"] == "/keys/other"
