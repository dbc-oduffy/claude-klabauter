"""The sizing CLI opens the XS by-hand lane on an XS sizing and refuses a re-size down to XS."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import xs_lane_budget
from coordinator_core.session.touch_record import KIND_WRITE, append_touch_claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_XS_REL = "state/sizings/2026-10-22-xs.yaml"
_S_REL = "state/sizings/2026-10-22-s.yaml"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _touch_sizing(repo: Path, rel: str, tshirt: str) -> None:
    (repo / rel).write_text(f"estimate:\n  tshirt: {tshirt}\n", encoding="utf-8")
    append_touch_claims([rel], _SID, repo, kind=KIND_WRITE)


def test_express_lane_xs_opens_lane_at_head_without_a_sizing_object(repo, capsys):
    assert sa.main(["--tshirt", "XS", "--express-lane"]) == sa.EXIT_OK
    rec = xs_lane_budget.read_lane(repo, _SID)
    assert rec["kind"] == "xs" and rec["state"] == "open"
    assert rec["base_sha"] == _git(repo, "rev-parse", "HEAD")
    assert list((repo / "state" / "sizings").iterdir()) == []


def test_express_lane_without_xs_opens_no_lane(repo):
    assert sa.main(["--tshirt", "S", "--express-lane"]) == sa.EXIT_OK
    assert xs_lane_budget.read_lane(repo, _SID) is None


def test_second_xs_is_refused_whatever_the_lane_state(repo, capsys):
    assert sa.main(["--tshirt", "XS", "--express-lane"]) == sa.EXIT_OK
    assert sa.main(["--tshirt", "XS", "--express-lane"]) == sa.EXIT_USAGE
    xs_lane_budget.mark_spent(repo, _SID)
    assert sa.main(["--tshirt", "XS", "--express-lane"]) == sa.EXIT_USAGE
    assert "XS refused" in capsys.readouterr().err


def test_xs_after_s_or_larger_sizing_is_refused_and_opens_no_lane(repo):
    _touch_sizing(repo, _S_REL, "S")
    assert sa.main(["--tshirt", "XS", "--express-lane"]) == sa.EXIT_USAGE
    assert xs_lane_budget.read_lane(repo, _SID) is None


def test_written_xs_object_opens_lane(repo, monkeypatch):
    monkeypatch.setattr(sa, "_open_routed_obligation", lambda *_a, **_k: None)
    (repo / _XS_REL).write_text("schema: sizing-object\n", encoding="utf-8")
    monkeypatch.setattr(
        sa, "write_back", lambda *a, **k: {"pm_verbatims": {"embedded": 0, "turns": None}}
    )
    rc = sa.main(["--tshirt", "XS", "--write", _XS_REL, "--no-plan-hand-off"])
    assert rc == sa.EXIT_OK
    assert xs_lane_budget.read_lane(repo, _SID)["kind"] == "xs"


def test_refused_written_xs_does_not_write(repo, monkeypatch):
    _touch_sizing(repo, _S_REL, "M")
    called = []
    monkeypatch.setattr(sa, "write_back", lambda *a, **k: called.append(1))
    rc = sa.main(["--tshirt", "XS", "--write", _XS_REL, "--no-plan-hand-off"])
    assert rc == sa.EXIT_USAGE and not called
