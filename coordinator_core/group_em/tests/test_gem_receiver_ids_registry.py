"""G-EM receiver ids extend from the manifest's identity.groupEmReceiverIds."""

import pytest

from coordinator_core.group_em import gem_home
from coordinator_core.ops.fleet import _memo_resolver as R


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(gem_home, "_receiver_ids_cache", None)


def test_key_absent_is_sender_only(monkeypatch):
    monkeypatch.setattr(R, "read_doe_identity", lambda: {})
    assert gem_home.receiver_ids() == frozenset({"group-em-em"})
    assert gem_home.is_group_em_receiver("group-em-em")
    assert not gem_home.is_group_em_receiver("group-em-fb")


def test_key_present_extends_case_insensitively(monkeypatch):
    monkeypatch.setattr(R, "read_doe_identity", lambda: {"groupEmReceiverIds": ["Group-EM-FB", " ", 3]})
    assert gem_home.is_group_em_receiver("group-em-fb")
    assert gem_home.is_group_em_receiver("  GROUP-EM-FB ")
    assert gem_home.is_group_em_receiver("group-em-em")
    assert not gem_home.is_group_em_receiver("coordinator-content-repo-em")


def test_identity_read_once_per_process(monkeypatch):
    calls = []

    def _ident():
        calls.append(1)
        return {"groupEmReceiverIds": ["group-em-fb"]}

    monkeypatch.setattr(R, "read_doe_identity", _ident)
    gem_home.is_group_em_receiver("a")
    gem_home.is_group_em_receiver("b")
    assert len(calls) == 1
