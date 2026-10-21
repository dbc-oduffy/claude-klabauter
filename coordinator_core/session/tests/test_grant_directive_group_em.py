from __future__ import annotations

import pytest

from coordinator_core.group_em import suite_grant
from coordinator_core.session import grant_directive as gd


def _result(ok, reason, record=None):
    return suite_grant.IssueResult(ok, reason, record)


@pytest.mark.parametrize(
    "rest",
    [
        [],
        ["g"],
        ["g", "n", "extra"],
        ["g", "n", "--ttl-minutes"],
        ["g", "n", "--ttl-minutes", "abc"],
        ["g", "n", "--repo"],
    ],
)
def test_grant_malformed_argv_is_usage(rest, monkeypatch):
    monkeypatch.setattr(suite_grant, "issue_suite_grant", lambda *a, **k: pytest.fail("called"))
    code, _ = gd.run_grant_directive(["group-em-grant", *rest])
    assert code == gd.EXIT_USAGE


@pytest.mark.parametrize("rest", [[], ["a", "b"], [""]])
def test_revoke_malformed_argv_is_usage(rest, monkeypatch):
    monkeypatch.setattr(suite_grant, "revoke_suite_grant", lambda *a, **k: pytest.fail("called"))
    code, _ = gd.run_grant_directive(["group-em-revoke", *rest])
    assert code == gd.EXIT_USAGE


def test_grant_valid_argv_forwards_flags_and_prints_summary(monkeypatch):
    seen = {}

    def fake(grantee, note, **kw):
        seen.update(grantee=grantee, note=note, **kw)
        return _result(
            True,
            "issued",
            {"grantee_session_id": "sid-g", "expires_at": "2026-10-21T10:00:00Z", "issuer_session_id": "sid-i"},
        )

    monkeypatch.setattr(suite_grant, "issue_suite_grant", fake)
    code, msg = gd.run_grant_directive(
        ["group-em-grant", "--repo", "/r", "peer", "ruled ok", "--ttl-minutes", "30"]
    )
    assert code == gd.EXIT_OK
    assert seen == {"grantee": "peer", "note": "ruled ok", "ttl_minutes": 30, "repo": "/r"}
    assert "sid-g" in msg and "2026-10-21T10:00:00Z" in msg and "sid-i" in msg


def test_grant_default_ttl_is_not_passed(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        suite_grant, "issue_suite_grant", lambda g, n, **kw: seen.update(kw) or _result(True, "issued", {})
    )
    gd.run_grant_directive(["group-em-grant", "peer", "n"])
    assert "ttl_minutes" not in seen


@pytest.mark.parametrize("reason", [suite_grant.NOT_GROUP_EM, suite_grant.TTL_OUT_OF_RANGE, suite_grant.GRANTEE_NOT_LIVE])
def test_refusal_is_exit_false_with_reason(reason, monkeypatch):
    monkeypatch.setattr(suite_grant, "issue_suite_grant", lambda *a, **k: _result(False, reason))
    code, msg = gd.run_grant_directive(["group-em-grant", "peer", "n"])
    assert code == gd.EXIT_FALSE
    assert msg.startswith(f"{reason}:")


def test_non_holder_caller_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    from coordinator_core.group_em import nomination

    monkeypatch.setattr(suite_grant.core, "resolve_session_id", lambda *a, **k: "peer-sid")
    monkeypatch.setattr(nomination, "box_holder", lambda *a, **k: {"session_id": "holder-sid", "live": True})
    monkeypatch.setattr(suite_grant, "_grants_dir", lambda d: tmp_path / "suite-grants")
    code, msg = gd.run_grant_directive(["group-em-grant", "peer-sid", "n"])
    assert code == gd.EXIT_FALSE
    assert msg.startswith("NOT_GROUP_EM:")
    assert not (tmp_path / "suite-grants").exists()


def test_holder_record_issuer_is_resolved_sid_not_argv(tmp_path, monkeypatch):
    from coordinator_core.group_em import nomination

    holder = {"session_id": "holder-sid", "live": True}
    monkeypatch.setattr(suite_grant.core, "resolve_session_id", lambda *a, **k: "holder-sid")
    monkeypatch.setattr(nomination, "box_holder", lambda *a, **k: holder)
    monkeypatch.setattr(nomination, "_holder_name", lambda h: "gem")
    monkeypatch.setattr(suite_grant, "_grants_dir", lambda d: tmp_path / "suite-grants")
    monkeypatch.setattr(suite_grant, "_resolve_grantee", lambda g: ("grantee-sid", None))
    monkeypatch.setattr(suite_grant, "session_live", lambda sid: True)
    code, _ = gd.run_grant_directive(["group-em-grant", "grantee-sid", "ruling", "--repo", str(tmp_path)])
    assert code == gd.EXIT_OK
    written = list((tmp_path / "suite-grants").glob("*/*.json"))
    assert [p.parent.name for p in written] == ["holder-sid"]
    assert gd.run_grant_directive(["group-em-grant", "grantee-sid", "ruling", "other-issuer"])[0] == gd.EXIT_USAGE

    code, _ = gd.run_grant_directive(["group-em-revoke", "grantee-sid"])
    assert code == gd.EXIT_OK
    assert not list((tmp_path / "suite-grants").glob("*/*.json"))
    assert gd.run_grant_directive(["group-em-revoke", "grantee-sid"])[0] == gd.EXIT_OK
