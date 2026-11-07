from __future__ import annotations

import json
import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.group_em import nomination, session_registry, suite_grant
from coordinator_core.session import grant as session_grant

NOW = 1_800_000_000.0
HOLDER, PEER, OTHER = "sid-holder", "sid-peer", "sid-other"


def _row(sid, name, cwd):
    return session_registry.RegistryRow(session_id=sid, name=name, pid=1, cwd=cwd, status="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    records = tmp_path / "group-em"
    records.mkdir()
    live = {HOLDER, PEER, OTHER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: sid in live)
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [
            _row(HOLDER, "holder-name", str(repo)),
            _row(PEER, "peer-name", str(repo)),
            _row(OTHER, "other-name", str(repo)),
        ],
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    nomination.claim(str(repo), HOLDER, directory=records, prompt_id="p-1")
    return {"repo": str(repo), "dir": records, "live": live, "mp": monkeypatch}


def _issue(env, grantee=PEER, **kw):
    return suite_grant.issue_suite_grant(grantee, "ruled: one broad run", directory=env["dir"], now=NOW, **kw)


def _check(env, sid=PEER, root=None, now=NOW + 60):
    return suite_grant.check_group_em_suite_grant(
        env["repo"], session_id=sid, repo_root=root or env["repo"], directory=env["dir"], now=now
    )


def _grant_files(env):
    return list((env["dir"] / "suite-grants").rglob("*.json"))


def test_holder_issues_and_grantee_checks_granted(env):
    result = _issue(env)
    assert result.ok and result.record["issuer_session_id"] == HOLDER
    assert result.record["grantee_session_id"] == PEER
    assert result.record["note"] == "ruled: one broad run"
    granted, record = _check(env)
    assert granted and record["grantee_session_id"] == PEER


def test_grantee_resolves_by_registry_name(env):
    assert _issue(env, "peer-name").record["grantee_session_id"] == PEER


def test_holder_may_grant_itself(env):
    _issue(env, HOLDER)
    assert _check(env, HOLDER)[0]


def test_non_holder_issue_refused_and_writes_nothing(env):
    env["mp"].setenv("COORDINATOR_SESSION_ID", PEER)
    result = _issue(env)
    assert (result.ok, result.reason) == (False, suite_grant.NOT_GROUP_EM)
    assert _grant_files(env) == []


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_unverified_holder_is_refused(env, status):
    env["mp"].setattr(nomination, "entry_status", lambda *a, **k: {"status": status})
    assert _issue(env).reason == suite_grant.NOT_GROUP_EM
    assert _grant_files(env) == []


def test_dead_holder_is_refused(env):
    env["live"].discard(HOLDER)
    assert _issue(env).reason == suite_grant.GROUP_EM_NOT_LIVE


def test_unresolved_issuer_is_refused(env):
    env["mp"].setattr(suite_grant.core, "resolve_session_id", lambda cwd=None: "")
    assert _issue(env).reason == suite_grant.ISSUER_UNRESOLVED


def test_empty_note_ttl_and_grantee_refusals(env):
    assert suite_grant.issue_suite_grant(PEER, "  ", directory=env["dir"], now=NOW).reason == suite_grant.NOTE_EMPTY
    assert _issue(env, ttl_minutes=0).reason == suite_grant.TTL_OUT_OF_RANGE
    assert _issue(env, ttl_minutes=721).reason == suite_grant.TTL_OUT_OF_RANGE
    assert _issue(env, "nobody").reason == suite_grant.GRANTEE_UNRESOLVED
    env["live"].discard(PEER)
    assert _issue(env).reason == suite_grant.GRANTEE_NOT_LIVE
    assert _grant_files(env) == []


def test_ttl_ceiling_is_accepted(env):
    assert _issue(env, ttl_minutes=720).ok and _check(env)[0]


def test_file_planted_under_a_non_holder_key_is_never_read(env):
    _issue(env)
    holder_file = env["dir"] / "suite-grants" / HOLDER / f"{PEER}.json"
    planted = env["dir"] / "suite-grants" / OTHER / f"{PEER}.json"
    planted.parent.mkdir(parents=True)
    planted.write_text(holder_file.read_text(encoding="utf-8"), encoding="utf-8")
    holder_file.unlink()
    assert _check(env) == (False, None)


def test_another_session_reads_ungranted(env):
    _issue(env)
    assert _check(env, OTHER) == (False, None)


def test_expired_grant_reads_ungranted_with_its_record(env):
    _issue(env, ttl_minutes=10)
    granted, record = _check(env, now=NOW + 601)
    assert not granted and record["grantee_session_id"] == PEER
    assert not _check(env, now=NOW - 1)[0]


def test_record_over_the_ceiling_reads_ungranted(env):
    _issue(env)
    path = _grant_files(env)[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expires_at"] = suite_grant._iso(NOW + 721 * 60)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not _check(env, now=NOW + 60)[0]


def test_displaced_holder_grant_reads_ungranted(env):
    _issue(env)
    nomination.claim(env["repo"], OTHER, directory=env["dir"], prompt_id="p-2")
    assert not _check(env)[0]


def test_standing_down_holder_grant_reads_ungranted(env):
    _issue(env)
    env["live"].discard(HOLDER)
    assert not _check(env)[0]


def test_unverified_holder_grant_reads_ungranted(env):
    _issue(env)
    env["mp"].setattr(nomination, "entry_status", lambda *a, **k: {"status": "pending"})
    assert not _check(env)[0]


def test_dead_grantee_reads_ungranted(env):
    _issue(env)
    env["live"].discard(PEER)
    assert not _check(env)[0]


def test_scope_root_is_component_wise(env, tmp_path):
    _issue(env, repo=str(tmp_path / "r" / "a"))
    assert _check(env, root=str(tmp_path / "r" / "a"))[0]
    assert _check(env, root=str(tmp_path / "r" / "a" / "sub"))[0]
    assert not _check(env, root=str(tmp_path / "r" / "ab"))[0]
    assert not _check(env, root=str(tmp_path / "r"))[0]
    assert not _check(env, root=env["repo"])[0]


def test_malformed_json_reads_ungranted(env):
    _issue(env)
    _grant_files(env)[0].write_text("{not json", encoding="utf-8")
    assert _check(env) == (False, None)
    _grant_files(env)[0].write_text("[1]", encoding="utf-8")
    assert _check(env) == (False, None)


def _stamp(env, box_id):
    path = _grant_files(env)[0]
    rec = json.loads(path.read_text(encoding="utf-8"))
    if box_id is None:
        rec.pop("box_id", None)
    else:
        rec["box_id"] = box_id
    path.write_text(json.dumps(rec), encoding="utf-8")


def test_issue_stamps_local_box_id(env):
    env["mp"].setattr(suite_grant.box_registry, "local_box_id", lambda home=None: "box-aaaa1111")
    assert _issue(env).record["box_id"] == "box-aaaa1111"
    assert _check(env)[0] is True


def test_grant_from_another_box_reads_ungranted_with_its_record(env):
    env["mp"].setattr(suite_grant.box_registry, "local_box_id", lambda home=None: "box-aaaa1111")
    _issue(env)
    _stamp(env, "box-bbbb2222")
    granted, record = _check(env)
    assert granted is False and record["box_id"] == "box-bbbb2222"


@pytest.mark.parametrize("stamped", [None, "local"])
def test_unstamped_or_local_grant_stays_valid_after_identity_minted(env, stamped):
    _issue(env)
    _stamp(env, stamped)
    env["mp"].setattr(suite_grant.box_registry, "local_box_id", lambda home=None: "box-aaaa1111")
    assert _check(env)[0] is True


def test_unsafe_session_id_reads_ungranted(env):
    assert _check(env, "../x") == (False, None)
    assert _check(env, "") == (False, None)


def test_reissue_overwrites_and_revoke_is_idempotent(env):
    _issue(env, ttl_minutes=5)
    _issue(env, ttl_minutes=60)
    assert len(_grant_files(env)) == 1 and _check(env, now=NOW + 3000)[0]
    first = suite_grant.revoke_suite_grant(PEER, directory=env["dir"])
    again = suite_grant.revoke_suite_grant(PEER, directory=env["dir"])
    assert (first.ok, first.reason, again.ok, again.reason) == (True, "revoked", True, "absent")
    assert not _check(env)[0]


def test_revoke_touches_only_the_callers_key(env):
    _issue(env)
    env["mp"].setenv("COORDINATOR_SESSION_ID", OTHER)
    suite_grant.revoke_suite_grant(PEER, directory=env["dir"])
    assert _check(env)[0]


MSYS_ROOT = "C:/Program Files/Git/Users/x/repo"  # abs-path-ok: MSYS fixture value


@pytest.mark.parametrize(
    "path",
    [MSYS_ROOT, "c:\\msys64\\home\\x", "D:/cygwin64/home/x", "C:/Program Files (x86)/Git/usr/x"],  # abs-path-ok: MSYS fixture values
)
def test_msys_prefixed_roots_are_detected(path):
    assert suite_grant._msys_mangled(path)


@pytest.mark.parametrize("path", ["/Users/x/repo", "C:/Users/x/repo", "C:/Program Files/Other/x", ""])
def test_ordinary_roots_are_not_msys_mangled(path):
    assert not suite_grant._msys_mangled(path)


def test_remote_issue_with_msys_mangled_repo_is_refused_and_delivers_nothing(env, monkeypatch):
    delivered = []
    monkeypatch.setattr(
        suite_grant.node_transport, "select", lambda *a, **k: delivered.append(a) or None
    )
    result = _issue(env, "box-b:sess1", repo=MSYS_ROOT)
    assert (result.ok, result.reason) == (False, suite_grant.SCOPE_ROOT_MSYS_MANGLED)
    assert delivered == []


def test_local_issue_with_a_msys_looking_repo_is_unaffected(env):
    assert _issue(env, repo=MSYS_ROOT).ok


def test_cli_message_for_msys_refusal_names_msys_no_pathconv():
    from coordinator_core.session import grant_directive

    assert "MSYS_NO_PATHCONV=1" in grant_directive._refusal(suite_grant.SCOPE_ROOT_MSYS_MANGLED)


@pytest.mark.parametrize("flavour,path,expected", [
    ("posix", "/Users/x/repo", True),
    ("posix", "C:/Program Files/Git/Users/x", False),
    ("posix", "repo/sub", False),
    ("windows", "C:/Program Files/Git/Users/x", True),
    ("windows", "repo\\sub", False),
])
def test_absolute_here_follows_the_os_flavour(monkeypatch, flavour, path, expected):
    cls = suite_grant.PurePosixPath if flavour == "posix" else suite_grant.PureWindowsPath
    monkeypatch.setattr(suite_grant, "_path_flavour", lambda: cls)
    assert suite_grant._absolute_here(path) is expected


@pytest.mark.parametrize("grantee", ["box-b:sess1", "box-b:"])
def test_revoke_of_a_remote_grantee_is_refused_loudly(env, grantee):
    result = suite_grant.revoke_suite_grant(grantee, directory=env["dir"])
    assert (result.ok, result.reason) == (False, suite_grant.REVOKE_REMOTE_UNSUPPORTED)
    from coordinator_core.session import grant_directive

    assert "expires_at" in grant_directive._refusal(result.reason)


def test_check_spawns_nothing(env, monkeypatch):
    _issue(env)

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert _check(env)[0]


def test_check_never_raises(env):
    env["mp"].setattr(nomination, "box_holder", lambda *a, **k: 1 / 0)
    assert _check(env) == (False, None)


def test_human_grant_enum_is_unchanged():
    assert session_grant._VALID_GRANTED_BY == frozenset({"pm", "ceremony"})


# --- cross-box relay: box A (holder) issues for a session living on box B --------------------


@pytest.fixture
def boxes(tmp_path, monkeypatch):
    import base64

    from coordinator_core.group_em import box_key, box_registry

    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    dir_a, dir_b = home_a / "state" / "group-em", home_b / "state" / "group-em"
    repo = tmp_path / "repo"
    repo.mkdir()
    id_a, id_b = box_registry.ensure_identity(home_a), box_registry.ensure_identity(home_b)
    box_registry.register(home_b, {"os": "linux"}, directory=dir_a)
    box_key.pin_peer_key(id_a, base64.b64encode(box_key.ensure_key(home_a)).decode(), home_b)
    live = {HOLDER, PEER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: sid in live)
    monkeypatch.setattr(
        session_registry, "read_rows", lambda directory=None: [_row(PEER, "peer-name", str(repo))]
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    monkeypatch.setattr(suite_grant.time, "time", lambda: NOW + 1)
    dir_a.mkdir(parents=True, exist_ok=True)
    nomination.claim(str(repo), HOLDER, directory=dir_a, prompt_id="p-1")
    return {"a": dir_a, "b": dir_b, "ida": id_a, "idb": id_b, "repo": str(repo), "live": live,
            "home_a": home_a, "home_b": home_b, "mp": monkeypatch}


def _relay(boxes, grantee=None, **kw):
    grantee = grantee or f"{boxes['idb']}:{PEER}"
    return suite_grant.issue_suite_grant(grantee, "ruled", directory=boxes["a"], now=NOW, **kw)


def _check_b(boxes, now=NOW + 60, root=None):
    return suite_grant.check_group_em_suite_grant(
        boxes["repo"], session_id=PEER, repo_root=root or boxes["repo"], directory=boxes["b"], now=now
    )


def test_relay_issue_delivers_and_grantee_box_reads_granted_with_no_local_holder(boxes):
    result = _relay(boxes)
    assert result.ok and result.record["grantee_box_id"] == boxes["idb"] and "sig" in result.record
    assert (boxes["b"] / "suite-grants" / "relayed" / f"{PEER}.json").is_file()
    assert list((boxes["a"] / "suite-grants").rglob("*.json")) == []
    granted, record = _check_b(boxes)
    assert granted and record["issuer_session_id"] == HOLDER and record["scope_root"] == boxes["repo"]


def test_relay_passes_through_node_transport_deliver_grant(boxes):
    from coordinator_core.group_em import node_transport

    seen = []

    class T:
        def deliver_grant(self, box, envelope):
            seen.append((box["box_id"], envelope))
            return {"ok": True, "reply": {}}

    boxes["mp"].setattr(node_transport, "select", lambda box, directory=None, runner=None: T())
    assert _relay(boxes).ok
    assert seen[0][0] == boxes["idb"] and seen[0][1]["issuer_box_id"] == boxes["ida"]


def test_relay_far_refusal_is_surfaced_and_writes_nothing(boxes):
    boxes["live"].discard(PEER)
    result = _relay(boxes)
    assert not result.ok and result.reason == f"{suite_grant.RELAY_FAILED}:{suite_grant.GRANTEE_NOT_LIVE}"
    assert not (boxes["b"] / "suite-grants").exists()


def test_relay_unknown_box_and_non_holder_are_refused(boxes):
    assert _relay(boxes, grantee=f"box-nope:{PEER}").reason == suite_grant.BOX_UNKNOWN
    boxes["mp"].setenv("COORDINATOR_SESSION_ID", PEER)
    assert _relay(boxes).reason == suite_grant.NOT_GROUP_EM


def _envelope(boxes, **over):
    from coordinator_core.group_em import box_key

    rec = {
        "schema_version": 1, "kind": suite_grant.KIND, "issuer_session_id": HOLDER,
        "issuer_name": "h", "issuer_box_id": boxes["ida"], "grantee_session_id": PEER,
        "grantee_box_id": boxes["idb"], "grantee_name": "",
        "issued_at": suite_grant._iso(NOW), "expires_at": suite_grant._iso(NOW + 3600), "note": "n",
    }
    rec.update(over)
    return box_key.sign(rec, boxes["home_a"])


def _receive(boxes, envelope, now=NOW + 1):
    return suite_grant.receive_relayed_grant(envelope, directory=boxes["b"], now=now)


def test_receive_accepts_a_valid_envelope_and_scope_comes_from_the_local_registry(boxes):
    assert _receive(boxes, _envelope(boxes))["ok"] is True
    assert _check_b(boxes)[0]
    assert not _check_b(boxes, root=str(boxes["home_b"]))[0]


def test_receive_refuses_unsigned_forged_tampered_and_unpinned(boxes):
    good = _envelope(boxes)
    unsigned = {k: v for k, v in good.items() if k != "sig"}
    tampered = {**good, "expires_at": suite_grant._iso(NOW + 7200)}
    assert _receive(boxes, unsigned)["reason"] == "bad_signature"
    assert _receive(boxes, tampered)["reason"] == "bad_signature"
    forged = {**good, "sig": "0" * 64}
    assert _receive(boxes, forged)["reason"] == "bad_signature"
    assert _receive(boxes, {**good, "issuer_box_id": "box-other"})["reason"] == "bad_signature"
    assert _receive(boxes, "nope")["reason"] == "bad_envelope"
    assert not (boxes["b"] / "suite-grants").exists()


def test_receive_refuses_expired_ceiling_wrong_box_and_not_live(boxes):
    assert _receive(boxes, _envelope(boxes), now=NOW + 7200)["reason"] == "expired"
    over = _envelope(boxes, expires_at=suite_grant._iso(NOW + 721 * 60))
    assert _receive(boxes, over)["reason"] == "bad_ttl"
    assert _receive(boxes, _envelope(boxes, grantee_box_id="box-else"))["reason"] == "wrong_grantee_box"
    boxes["live"].discard(PEER)
    assert _receive(boxes, _envelope(boxes))["reason"] == suite_grant.GRANTEE_NOT_LIVE
    assert not (boxes["b"] / "suite-grants").exists()


def test_planted_relayed_file_reads_ungranted(boxes):
    import json as _json

    target = boxes["b"] / "suite-grants" / "relayed" / f"{PEER}.json"
    target.parent.mkdir(parents=True)
    forged = {**_envelope(boxes), "sig": "f" * 64}
    target.write_text(_json.dumps({"envelope": forged, "scope_root": boxes["repo"]}), encoding="utf-8")
    assert _check_b(boxes) == (False, None)
    unsigned = {k: v for k, v in _envelope(boxes).items() if k != "sig"}
    target.write_text(_json.dumps({"envelope": unsigned, "scope_root": boxes["repo"]}), encoding="utf-8")
    assert _check_b(boxes) == (False, None)


def test_relayed_grant_expiry_liveness_and_envelope_scope_are_enforced(boxes):
    assert _receive(boxes, _envelope(boxes, scope_root=str(boxes["repo"]) + "/sub"))["ok"]
    assert not _check_b(boxes)[0]
    assert _receive(boxes, _envelope(boxes))["ok"]
    assert _check_b(boxes)[0]
    assert not _check_b(boxes, now=NOW + 7200)[0]
    boxes["live"].discard(PEER)
    assert not _check_b(boxes)[0]


def test_bare_sid_still_takes_the_same_box_path(boxes):
    boxes["mp"].setattr(
        session_registry, "read_rows", lambda directory=None: [_row(PEER, "peer-name", boxes["repo"])]
    )
    result = suite_grant.issue_suite_grant(PEER, "ruled", directory=boxes["a"], now=NOW)
    assert result.ok and "grantee_box_id" not in result.record
    assert (boxes["a"] / "suite-grants" / HOLDER / f"{PEER}.json").is_file()
