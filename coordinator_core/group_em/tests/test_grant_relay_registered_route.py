"""The grant relay dials the registered ssh alias, not the member's mDNS name; host-form grantees resolve."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.group_em import box_registry, box_transport, member_box, suite_grant
from coordinator_core.group_em.tests.test_suite_grant import NOW, env  # noqa: F401  (fixture)

MEMBER = {"box": "mac-mini", "host": "mac-mini.local", "user": "example-operator"}
ALIAS = "um01-macmini"


class _Mem:
    class MembershipError(Exception):
        pass

    def verified_members(self, home, run=None):
        return [MEMBER]


def _ssh_g(argv, **kw):
    return subprocess.CompletedProcess(argv, 0, f"hostname {MEMBER['host']}\n", "")


def _wire(monkeypatch, aliases):
    real = member_box._autolink()
    al = SimpleNamespace(
        far_boxes_status=lambda roster: (aliases, None),
        member_alias=lambda roster, box, host, aliases, run=None: real.member_alias(
            roster, box, host, aliases, run=_ssh_g
        ),
        machine_local_key=lambda *a, **k: None,
        ssh_identity_file=lambda *a, **k: None,
        resolve_key=lambda roster, alias, local_fn=None, ssh_fn=None: "/keys/relay",
    )
    monkeypatch.setattr(member_box, "_membership", lambda: _Mem())
    monkeypatch.setattr(member_box, "_autolink", lambda: al)
    monkeypatch.setattr(member_box, "_roster", lambda: SimpleNamespace(no_window=lambda: 0))


def _envelope():
    return {
        "grantee_session_id": "d68d0a27",
        "grantee_box_id": "mac-mini",
        "issued_at": "2026-11-14T00:00:00Z",
        "expires_at": "2026-11-14T01:30:00Z",
        "note": "ruling",
    }


def _relay_argv(box, monkeypatch):
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [box])
    seen = []

    def runner(argv, timeout, stdin_text):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}) + "\n", "")

    assert box_transport.deliver_grant(box, _envelope(), runner)["ok"] is True
    return seen[0]


def test_relay_argv_carries_the_registered_alias_not_the_mdns_name(tmp_path, monkeypatch):
    _wire(monkeypatch, [ALIAS])
    box = member_box.resolve_member_box("mac-mini", home=tmp_path, run=_ssh_g)["box"]
    argv = _relay_argv(box, monkeypatch)
    assert ALIAS in argv and not any("mac-mini.local" in part for part in argv)


def test_without_a_registered_alias_the_member_host_is_dialled(tmp_path, monkeypatch):
    _wire(monkeypatch, [])
    box = member_box.resolve_member_box("mac-mini", home=tmp_path, run=_ssh_g)["box"]
    assert box["transport"]["host"] == "example-operator@mac-mini.local"


def test_a_stale_mdns_row_is_re_resolved_to_the_alias(tmp_path, monkeypatch):
    _wire(monkeypatch, [])
    member_box.resolve_member_box("mac-mini", home=tmp_path, run=_ssh_g)
    _wire(monkeypatch, [ALIAS])
    directory = box_registry._group_em_dir(tmp_path)
    box, why = suite_grant._far_box("mac-mini", directory, tmp_path)
    assert why == "" and box["transport"]["host"] == ALIAS


@pytest.mark.parametrize("form", ["mac-mini.local:d68d0a27", "example-operator@mac-mini.local:d68d0a27"])
def test_member_host_forms_resolve_to_the_box_id_key(tmp_path, monkeypatch, form):
    _wire(monkeypatch, [ALIAS])
    assert suite_grant._member_host_key(form, tmp_path) == "mac-mini:d68d0a27"


@pytest.mark.parametrize("form", ["mac-mini:d68d0a27", "example-retrieval-repo-fe", "stranger.local:d68d0a27"])
def test_other_forms_are_left_alone(tmp_path, monkeypatch, form):
    _wire(monkeypatch, [ALIAS])
    assert suite_grant._member_host_key(form, tmp_path) == form


def test_a_row_already_on_a_registered_alias_is_not_re_resolved(tmp_path, monkeypatch):
    _wire(monkeypatch, [ALIAS])
    member_box.resolve_member_box("mac-mini", home=tmp_path, run=_ssh_g)
    directory = box_registry._group_em_dir(tmp_path)
    monkeypatch.setattr(member_box, "resolve_member_box", lambda *a, **k: pytest.fail("re-resolved"))
    box, why = suite_grant._far_box("mac-mini", directory, tmp_path)
    assert why == "" and box["transport"]["host"] == ALIAS


def test_a_bare_unregistered_name_is_refused_naming_the_accepted_forms(env):
    reason = suite_grant.issue_suite_grant("example-retrieval-repo-fe", "ruling", directory=env["dir"], now=NOW).reason
    assert reason.startswith(f"{suite_grant.GRANTEE_UNRESOLVED}:")
    assert "<box_id>:<session_id>" in reason and "<member_host>:<session_id>" in reason
