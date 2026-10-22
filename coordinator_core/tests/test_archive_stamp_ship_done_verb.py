"""ship-handoff as the one done verb: stamp, flip, release outcome, archive, terminality.

Subject: `archive_stamp.cs_ship_handoff` stdout contract and exit codes. Every case
builds a git repo under `tmp_path`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.archive_stamp as arstamp
import coordinator_core.ops.handoff_archive_transition  # noqa: F401
import coordinator_core.ops.handoff_stamp  # noqa: F401
import coordinator_core.ops.handoff_transition  # noqa: F401
import coordinator_core.ops.session.record_pickup  # noqa: F401
from coordinator_core.session import claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "22222222-2222-2222-2222-222222222222"
_OTHER_SID = "33333333-3333-3333-3333-333333333333"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    args_list = list(args)
    if args_list[:2] == ["commit", "-m"] and "Session-Id:" not in args_list[2]:
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {_SID}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True, text=True, env=_GIT_ENV, timeout=30,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("init\n", encoding="utf-8")
    _git(r, "add", "README.md")
    _git(r, "commit", "-m", "init")
    return r


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _baton(repo: Path, name: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Baton {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        "deployment_state: in_flight\n"
        f"scope:\n  - state/handoffs/{name}\n"
        f"{extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _plan(repo: Path, name: str, status: str, **fields: str) -> str:
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"title: {name}", f"status: {status}"] + [f"{k}: {v}" for k, v in fields.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path.relative_to(repo).as_posix()


def _claim_dir(repo: Path, basename: str) -> Path:
    return repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename


def _archived(repo: Path, name: str) -> list[Path]:
    return sorted((repo / "archive" / "handoffs").rglob(name))


def _lines(out: str, prefix: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(prefix)]


def _fm_line(path: Path, key: str) -> str:
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith(f"{key}:"):
            return ln
    return ""


def test_ac1_ac2_ac3_ship_releases_stamps_and_archives(repo, capsys):
    hp = _baton(repo, "done1.md")
    assert claims.claim_artifact("handoff", "done1.md", cwd=str(repo)) is True

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    out = capsys.readouterr().out
    assert rc == 0
    assert not _claim_dir(repo, "done1.md").exists()
    assert _lines(out, "release=") == ["release=released"]
    assert len(_lines(out, "plan-terminality=")) == 1
    assert out.splitlines().index("release=released") > out.splitlines().index(
        _lines(out, "plan-terminality=")[0]
    )
    twin = _archived(repo, "done1.md")
    assert len(twin) == 1 and not hp.exists()
    text = twin[0].read_text(encoding="utf-8")
    assert "deployment_state: shipped" in text and "shipped_in:" in text


def test_ac3_unresolvable_sha_exits_nonzero_and_stamps_nothing(repo, capsys):
    hp = _baton(repo, "nosha.md")
    before = hp.read_bytes()

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha="not-a-sha")

    out = capsys.readouterr().out
    assert rc != 0
    assert hp.read_bytes() == before
    assert _lines(out, "release=") == []


def test_ac4_ac12_status_and_claim_fields_are_byte_unchanged(repo):
    hp = _baton(repo, "bytes1.md", extra="claimed_by: someone\nclaimed_at: 2026-01-01T00:00:00Z\n")
    before = {k: _fm_line(hp, k) for k in ("status", "claimed_by", "claimed_at")}
    assert all(before.values())

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    assert rc in (0, arstamp.SHIP_RC_CLAIM_NOT_RELEASED)
    (twin,) = _archived(repo, "bytes1.md")
    assert {k: _fm_line(twin, k) for k in before} == before


def test_ac5_non_terminal_plan_warns_and_still_ships(repo, capsys):
    rel = _plan(repo, "p.md", "executing", deliverable_id="dlv-x")
    hp = _baton(repo, "nt.md", extra="deliverable_id: dlv-x\n")

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    out = capsys.readouterr().out
    assert rc == 0
    assert _lines(out, "plan-terminality=") == [
        f"plan-terminality=non-terminal plan={rel} status=executing"
    ]
    assert len(_archived(repo, "nt.md")) == 1


def test_ac6_unlinked_baton_prints_no_plan_linked(repo, capsys):
    hp = _baton(repo, "nolink.md")

    arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    assert _lines(capsys.readouterr().out, "plan-terminality=") == [
        "plan-terminality=no-plan-linked"
    ]


def test_ac8_second_run_on_archived_baton_changes_nothing(repo, capsys):
    hp = _baton(repo, "twice.md")
    sha = _head(repo)
    assert arstamp.cs_ship_handoff(str(hp), archive=True, sha=sha) == 0
    capsys.readouterr()
    (twin,) = _archived(repo, "twice.md")
    before = twin.read_bytes()
    head_before = _head(repo)

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=sha)

    out = capsys.readouterr().out
    assert rc == 0
    assert _lines(out, "release=") == ["release=already-absent"]
    assert len(_lines(out, "plan-terminality=")) == 1
    assert twin.read_bytes() == before
    assert _head(repo) == head_before
    assert not hp.exists()


def test_ac10_claim_held_by_another_session_exits_5_and_ship_stands(repo, capsys, monkeypatch):
    hp = _baton(repo, "other.md")
    monkeypatch.setenv("CLAUDE_SESSION_ID", _OTHER_SID)
    assert claims.claim_artifact("handoff", "other.md", cwd=str(repo)) is True
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    out = capsys.readouterr().out
    assert rc == arstamp.SHIP_RC_CLAIM_NOT_RELEASED == 5
    assert _lines(out, "release=") == ["release=held-by-another-session-not-released"]
    assert len(_archived(repo, "other.md")) == 1
    assert _claim_dir(repo, "other.md").is_dir()


def test_ac11_retained_op_result_prints_triple_false_and_releases_nothing(repo, capsys, monkeypatch):
    hp = _baton(repo, "ret.md")
    monkeypatch.setattr(
        arstamp, "_call_handoff_archive_transition",
        lambda p, params: {"exit_code": 0, "retained": True, "retain_reason": "x"},
    )
    spy: list = []
    monkeypatch.setattr(claims, "release_artifact_outcome", lambda *a, **k: spy.append(a) or "released")

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    out = capsys.readouterr().out
    assert rc == arstamp.SHIP_RC_RETAINED == 3
    assert spy == []
    assert _lines(out, "release=") == []
    assert out.splitlines()[-1] == "stamped=false released=false archived=false"
    assert len(_lines(out, "plan-terminality=")) == 1


def test_refused_op_prints_terminality_and_no_release_line(repo, capsys, monkeypatch):
    hp = _baton(repo, "refused.md")
    monkeypatch.setattr(
        arstamp, "_call_handoff_archive_transition",
        lambda p, params: {"exit_code": 4, "error": "refused"},
    )

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    out = capsys.readouterr().out
    assert rc == 4
    assert len(_lines(out, "plan-terminality=")) == 1
    assert _lines(out, "release=") == []


def test_archive_true_whose_move_did_not_land_is_not_a_success(repo, capsys, monkeypatch):
    hp = _baton(repo, "nomove.md")
    monkeypatch.setattr(
        arstamp, "_call_handoff_archive_transition",
        lambda p, params: {"exit_code": 0, "moved": True},
    )

    rc = arstamp.cs_ship_handoff(str(hp), archive=True, sha=_head(repo))

    assert rc != 0
    assert _lines(capsys.readouterr().out, "release=") == []
