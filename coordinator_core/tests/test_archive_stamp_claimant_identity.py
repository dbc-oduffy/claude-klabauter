"""
coordinator_core.tests.test_archive_stamp_claimant_identity — coverage for
`archive_stamp._record_claimant_identity_best_effort`, the claim-time stamp of
the claiming SESSION's harness identity (`claimed_by_name` / `claimed_by_address`)
written beside the unchanged UUID-carrying `claimed_by`/`picked_up_by` on both
the handoff claim path (`cs_claim_handoff`) and the memo claim path
(`cs_claim_memo_stamp`).

PM ruling, 2026-08-30. The gap this closes is narrow and worth stating, because
the resolver it complements has shipped since 2026-08-13: while the claimant is
LIVE, `session.resolve_address` already answers "who holds this baton" and no
stamp is needed. Once the claiming session EXITS, its registry record is gone,
the UUID resolves to `not_reachable`/`no-live-record`, and a claimed, in-flight
baton is left with an owner nobody can even name.

This module pins:

  1. `cs_claim_handoff` writes both fields beside an unchanged `claimed_by`.
  2. `cs_claim_memo_stamp` writes both fields on its own claim path too.
  3. A registry record carrying a name but NO socket (the harness omits
     `messagingSocketPath` whenever its cross-session-inbox gate is off —
     measured 44/44 records on 2026-08-14) stamps the name and omits the
     address key entirely. No sentinel, no empty string.
  4. `self_record()` returning None omits BOTH keys — no partial stamp.
  5. A raising `self_record()` is non-fatal: the claim still lands rc=0 with
     the lifecycle transition intact and neither key written.
  6. Re-claiming a record that already carries the fields never overwrites
     them — the first claimant's identity is the forensic record.

Negative-spec: `claimed_by_address` IS NOT A SEND TARGET and this module does
not test it as one. `session.work_state._resolve_send_message_addresses`'
negative-spec still holds — a dead socket can be reused by an unrelated LATER
session — so the stamped value is forensic/staleness-detection only, and the
sanctioned read is to re-resolve `claimed_by` and COMPARE.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.handoff_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.memo_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.session.record_pickup  # noqa: F401 — @register_op side effect

import coordinator_core.archive_stamp as arstamp
from coordinator_core.session import harness_registry

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "22222222-2222-2222-2222-222222222222"
_NAME = "claude-klabauter-4f"
_SOCKET = r"\\.\pipe\LOCAL\cc-msg-9d6c94f6b101ab51917ba9f"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_memo(repo: Path, name: str, extra: str = "") -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        "status: open\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _record(name, socket) -> harness_registry.RegistryRecord:
    return harness_registry.RegistryRecord(
        pid=1,
        start_epoch=1000.0,
        cwd=None,
        name=name,
        messaging_socket_path=socket,
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # Isolate from the human-claimant stamp, which shares the claim path.
    monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {})


def _set_self_record(monkeypatch, value):
    if callable(value):
        monkeypatch.setattr(harness_registry, "self_record", value)
    else:
        monkeypatch.setattr(harness_registry, "self_record", lambda: value)


class TestHandoffClaimStampsIdentity:
    def test_both_fields_land_beside_an_unchanged_claimed_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h1.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        # The UUID stays the durable identity, unchanged.
        assert "claimed_by: sess-abc" in text
        assert f"claimed_by_name: {_NAME}" in text
        assert _SOCKET in text

    def test_record_without_socket_stamps_name_and_omits_address(self, tmp_path, monkeypatch):
        """The harness omits messagingSocketPath whenever its cross-session-inbox
        gate is off. That must degrade to a name-only stamp, never an empty
        address key a reader could mistake for 'resolved to nothing'."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h2.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, None)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert f"claimed_by_name: {_NAME}" in text
        assert "claimed_by_address" not in text

    def test_absent_registry_record_omits_both_keys(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h3.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, None)

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-abc" in text
        assert "claimed_by_name" not in text
        assert "claimed_by_address" not in text

    def test_raising_resolver_is_non_fatal_and_the_claim_still_lands(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h4.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")

        def _boom():
            raise RuntimeError("registry read failed")

        _set_self_record(monkeypatch, _boom)

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        # The lifecycle transition is intact — identity is advisory, never a gate.
        assert "status: claimed" in text
        assert "claimed_by: sess-abc" in text
        assert "claimed_by_name" not in text
        assert "claimed_by_address" not in text

    def test_existing_identity_is_never_overwritten(self, tmp_path, monkeypatch):
        """The FIRST claimant's identity is the forensic record. A re-claim
        carrying a different registry record must not rewrite it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "h5.md",
            extra="claimed_by_name: claude-klabauter-old\nclaimed_by_address: old-socket\n",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by_name: claude-klabauter-old" in text
        assert "claimed_by_address: old-socket" in text
        assert _NAME not in text


class TestMemoClaimStampsIdentity:
    def test_both_fields_land_beside_an_unchanged_picked_up_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m1.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_memo_stamp(str(mp))

        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "picked_up_by: sess-abc" in text
        assert f"claimed_by_name: {_NAME}" in text
        assert _SOCKET in text
