"""The stale-versus-live decision in `reap-stale-subagent-sidecars.py`.

This reaper IS the sidecar retention policy: a sidecar is reapable only when
its owning session is dead AND its age is at or past the floor AND its status
is not `blocked`/`thrashing`. These pin `classify_sidecars` directly, with the
session-liveness verdict injected, so the rule is asserted without git, the
repo-root resolver, or any delete.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "reap-stale-subagent-sidecars.py"

_LIVE = "sid-live"
_DEAD = "sid-dead"
_FLOOR_DAYS = 14
_DAY = 86400.0


@pytest.fixture(scope="module")
def reap():
    spec = importlib.util.spec_from_file_location("_reap_stale_subagent_sidecars_decision", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _session_live(session_id: str, cwd=None) -> bool:
    return session_id == _LIVE


def _sidecar(share: Path, session: str, name: str, *, age_days: float, status: str = "") -> str:
    path = share / session / name
    path.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nstatus: {status}\n---\n" if status else ""
    path.write_text(f"{front}body\n", encoding="utf-8")
    stamp = time.time() - age_days * _DAY
    os.utime(path, (stamp, stamp))
    return str(path)


def _classify(reap, share: Path):
    to_reap, live, status, young = reap.classify_sidecars(
        [str(share)], _session_live,
        repo_root=str(share.parent), now=time.time(), age_floor_days=_FLOOR_DAYS,
    )
    return set(to_reap), live, status, young


@pytest.fixture
def share(tmp_path: Path) -> Path:
    return tmp_path / "subagent-share"


def test_a_live_sessions_young_sidecar_is_kept(reap, share):
    _sidecar(share, _LIVE, "young.md", age_days=1)

    to_reap, live, _status, _young = _classify(reap, share)

    assert to_reap == set()
    assert live == 1


def test_a_live_sessions_sidecar_past_the_floor_is_still_kept(reap, share):
    _sidecar(share, _LIVE, "old.md", age_days=_FLOOR_DAYS + 30)

    to_reap, live, _status, _young = _classify(reap, share)

    assert to_reap == set()
    assert live == 1


def test_a_dead_sessions_sidecar_past_the_floor_is_reaped(reap, share):
    stale = _sidecar(share, _DEAD, "stale.md", age_days=_FLOOR_DAYS + 1, status="complete")

    to_reap, _live, _status, _young = _classify(reap, share)

    assert to_reap == {stale}


def test_a_dead_sessions_sidecar_under_the_floor_is_kept(reap, share):
    _sidecar(share, _DEAD, "young.md", age_days=_FLOOR_DAYS - 1)

    to_reap, _live, _status, young = _classify(reap, share)

    assert to_reap == set()
    assert young == 1


@pytest.mark.parametrize("status", ["blocked", "thrashing", "Blocked"])
def test_the_blocked_and_thrashing_carve_out_survives_a_dead_session_past_the_floor(
    reap, share, status
):
    _sidecar(share, _DEAD, "held.md", age_days=_FLOOR_DAYS + 30, status=status)

    to_reap, _live, preserved_status, _young = _classify(reap, share)

    assert to_reap == set()
    assert preserved_status == 1


def test_one_dead_session_reaps_only_its_stale_unheld_sidecar(reap, share):
    stale = _sidecar(share, _DEAD, "stale.md", age_days=_FLOOR_DAYS + 1)
    _sidecar(share, _DEAD, "young.md", age_days=1)
    _sidecar(share, _DEAD, "held.md", age_days=_FLOOR_DAYS + 1, status="blocked")
    _sidecar(share, _LIVE, "live.md", age_days=_FLOOR_DAYS + 1)

    assert _classify(reap, share) == ({stale}, 1, 1, 1)
