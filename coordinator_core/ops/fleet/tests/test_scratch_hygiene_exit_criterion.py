"""Exit criterion (1) of the fleet.scratch_hygiene plan, driven through the registered handler."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import pytest

from coordinator_core.group_em import session_registry
from coordinator_core.install.junction import is_junction
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops import _eager_import_all
from coordinator_core.ops.fleet.scratch_hygiene_records import repo_relative_path
from coordinator_core.ops.fleet.tests._scratch_hygiene_fixture import build_two_root_fixture

OP = "fleet.scratch_hygiene"
_KEYS = ["op", "kind", "repo", "path", "bytes", "age_days", "readme", "finding"]


def _call(params: dict) -> dict:
    _eager_import_all()
    return asyncio.run(_REGISTRY[OP](params))


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for n in sorted(filenames):
            p = Path(dirpath) / n
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _snapshot(*roots: Path) -> set[str]:
    seen: set[str] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            seen.update(os.path.join(dirpath, n) for n in dirnames + filenames)
    return seen


def _backdate(path: Path, seconds: float) -> None:
    """Plain os.utime that never descends a link; the shared fixture's age() ages nothing on Windows."""
    stamp = time.time() - seconds
    stack = [path]
    while stack:
        p = stack.pop()
        os.utime(p, (stamp, stamp))
        if p.is_dir() and not p.is_symlink() and not is_junction(p):
            stack.extend(p.iterdir())


@pytest.fixture
def fx(tmp_path, monkeypatch):
    f = build_two_root_fixture(tmp_path, monkeypatch)
    for path, action in f.expected.items():
        if action not in ("skipped-young", "skipped-link"):
            _backdate(path, 10 * 24 * 3600)
    os.utime(f.temp_root / "pytest", (time.time() - 864000,) * 2)
    for held in (f.hold_readme, f.hold_missing):
        _backdate(held, 400 * 24 * 3600)
    monkeypatch.setattr(session_registry, "registry_dir", lambda: f.registry_dir)
    yield f
    f.close()


def _params(f, **extra) -> dict:
    return {
        "repo_root": str(f.repo_root),
        "cadence": "workday_complete",
        "verify_copy": f.verify_copy,
        **extra,
    }


def test_dry_run_classifies_every_entry_and_deletes_nothing(fx):
    before = _snapshot(fx.scratch, fx.temp_root, fx.outside)
    start = time.process_time()
    result = _call(_params(fx))
    cost = time.process_time() - start

    assert result["exit_code"] == 0 and result["contract_exit"] == 1
    assert result["records"][-1] == {"op": OP, "summary": True, "entries": len(result["records"]) - 1,
                                     "bytes": result["records"][-1]["bytes"], "applied": False,
                                     "capabilities": ["hold_pending_marker"]}
    purge = [r for r in result["records"] if r.get("kind") == "purge"]
    assert {r["action"] for r in purge} <= {"would-delete", "skipped-live", "skipped-young",
                                            "skipped-unverified", "skipped-link"}
    assert len(purge) == len(fx.expected)
    by_path = {r["path"]: r for r in purge}
    for path, action in fx.expected.items():
        rec = by_path[repo_relative_path(path, fx.repo_root)]
        assert rec["action"] == action, (path, rec)
        want_reason = fx.expected_reason.get(path)
        if want_reason:
            assert rec["reason"] == want_reason
    assert _snapshot(fx.scratch, fx.temp_root, fx.outside) == before
    assert cost < 0.5


def test_apply_deletes_exactly_the_would_delete_set(fx):
    outside_digest = _digest(fx.outside)
    planned = {r["path"] for r in _call(_params(fx))["records"]
               if r.get("kind") == "purge" and r["action"] == "would-delete"}
    assert planned == {repo_relative_path(p, fx.repo_root) for p in fx.would_delete()}

    result = _call(_params(fx, apply=True))
    deleted = {r["path"] for r in result["records"] if r.get("kind") == "purge" and r["action"] == "deleted"}
    assert deleted == planned
    assert result["records"][-1]["applied"] is True
    for p in fx.would_delete():
        assert not os.path.lexists(p)
    for p, action in fx.expected.items():
        if action != "would-delete":
            assert os.path.lexists(p), p
    assert _digest(fx.outside) == outside_digest
    assert (fx.outside / "keep.txt").read_bytes() == b"must survive"


def test_workweek_complete_emits_the_hold_nag_line(fx):
    result = _call(_params(fx, cadence="workweek_complete", nag_only=True))
    nags = [r for r in result["records"] if r.get("kind") == "hold-nag"]
    assert len(nags) == 2 and all(list(r) == _KEYS for r in nags)
    missing = next(r for r in nags if r["finding"] == "missing-readme")
    assert missing["path"] == "scratch-hold/held-no-readme"
    assert missing["readme"] is None and missing["age_days"] > 365
    assert not any(r.get("kind") == "purge" for r in result["records"])
    assert result["contract_exit"] == 1

    held = _call(_params(fx, cadence="workweek_start"))
    assert any(r.get("kind") == "hold-nag" for r in held["records"])
    assert not any(r.get("kind") == "hold-nag" for r in _call(_params(fx, cadence="distill"))["records"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("repo_root"),
        lambda p: p.update(repo_root=p["repo_root"] + "-absent"),
        lambda p: p.update(cadence="hourly"),
        lambda p: p.update(apply="yes"),
        lambda p: p.update(quiescence_hours=-1),
        lambda p: p.update(verify_copy={"source": "x"}),
    ],
)
def test_bad_input_is_contract_exit_2(fx, mutate):
    params = _params(fx)
    mutate(params)
    result = _call(params)
    assert result["exit_code"] == 2 and result["contract_exit"] == 2 and result["records"] == []
