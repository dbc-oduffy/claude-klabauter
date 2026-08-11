"""
coordinator_core.tests.test_chain_ancestry_waiver_live_foreign_owner_refusal —
behavioural pin for `record_chain_ancestry_waiver`'s 2026-08-10 mint-site
refusal: a sha whose own `Session-Id:` trailer names a DIFFERENT, LIVE
session must never be minted into the CLOSING session's own chain-ancestry
waiver directory — defence-in-depth for the session-shape-misclassification
incident (cross-repo/inbox/2026-08-10-example-retrieval-repo-em-wsc-misdetection-
wrote-to-a-live-peers-plan.md) alongside `plan_status_transition._refuse_
if_live_foreign_holder` and `coordinator_complete_entry._refuse_if_live_
foreign_entry_holder`.

Drives the real `record_chain_ancestry_waiver` entry point, through the
real `chain_waiver_dir`, monkeypatching only `subprocess.run` (the git
trailer read) and `coordinator_core.session.liveness.session_live` (the
liveness oracle) — never asserting a default-parameter value in place of
behaviour.

Spec backlink: state/audits/2026-08-10 session-shape-misclassification
fallout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import coordinator_core.chain_ancestry_waivers as _mod

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

_CHAIN_ID = "abcdef01-2345-6789-abcd-ef0123456789"
_FOREIGN_SID = "11111111-1111-1111-1111-111111111111"
_SHA = "2222222222222222222222222222222222222b"


def _fake_git_trailer(trailer_line: str):
    """Stands in for the batched `git log --no-walk` trailer read, in its own
    NUL-delimited `%x00%H%x00<trailers>` framing — one record per requested
    sha, which is what the production parser splits on."""
    def _run(cmd, capture_output, text, check, cwd, **kwargs):
        shas = [arg for arg in cmd if not arg.startswith("-") and arg not in ("git", "log")]
        stdout = "".join(f"\x00{sha}\x00{trailer_line}\n" for sha in shas)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return _run


def test_live_foreign_trailer_owner_is_refused(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    monkeypatch.setattr(_mod.subprocess, "run", _fake_git_trailer(f"{_FOREIGN_SID}\n"))
    monkeypatch.setattr(
        "coordinator_core.session.liveness.session_live",
        lambda sid, cwd=None: sid == _FOREIGN_SID,
    )

    refused = _mod.record_chain_ancestry_waiver(str(cwd), frozenset({_SHA}), _CHAIN_ID)

    assert refused == frozenset({_SHA})
    target = _mod.chain_waiver_dir(str(cwd), _CHAIN_ID) / f"{_SHA}.json"
    assert not target.exists()


def test_dead_foreign_trailer_owner_proceeds(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    monkeypatch.setattr(_mod.subprocess, "run", _fake_git_trailer(f"{_FOREIGN_SID}\n"))
    monkeypatch.setattr(
        "coordinator_core.session.liveness.session_live",
        lambda sid, cwd=None: False,
    )

    refused = _mod.record_chain_ancestry_waiver(str(cwd), frozenset({_SHA}), _CHAIN_ID)

    assert refused == frozenset()
    target = _mod.chain_waiver_dir(str(cwd), _CHAIN_ID) / f"{_SHA}.json"
    assert target.exists()


def test_self_owned_trailer_proceeds(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    monkeypatch.setattr(_mod.subprocess, "run", _fake_git_trailer(f"{_CHAIN_ID}\n"))
    monkeypatch.setattr(
        "coordinator_core.session.liveness.session_live",
        lambda sid, cwd=None: True,
    )

    refused = _mod.record_chain_ancestry_waiver(str(cwd), frozenset({_SHA}), _CHAIN_ID)

    assert refused == frozenset()
    target = _mod.chain_waiver_dir(str(cwd), _CHAIN_ID) / f"{_SHA}.json"
    assert target.exists()


def test_untrailered_sha_proceeds(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    monkeypatch.setattr(_mod.subprocess, "run", _fake_git_trailer(""))
    monkeypatch.setattr(
        "coordinator_core.session.liveness.session_live",
        lambda sid, cwd=None: True,
    )

    refused = _mod.record_chain_ancestry_waiver(str(cwd), frozenset({_SHA}), _CHAIN_ID)

    assert refused == frozenset()
    target = _mod.chain_waiver_dir(str(cwd), _CHAIN_ID) / f"{_SHA}.json"
    assert target.exists()
