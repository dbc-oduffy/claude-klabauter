"""A halted run records landed when its marker also withholds halt records of superseded predecessor runs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "warp-2026-11-09-x-20261109T000000-abcdef"
OLD = ["warp-2026-11-09-x-20261109T000000-f925b7", "warp-2026-11-09-x-20261109T010000-5d435c"]


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(hrl, "_launch", lambda root, run_id: True)
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)


def _fixture(tmp_path: Path, extra: list):
    """A run withholding two product paths (landed by the returned sha), predecessor halt records, and *extra*."""
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(tmp_path),
                              check=True, capture_output=True, text=True, **no_console_creationflags()).stdout.strip()

    product = ["prod0.py", "prod1.py"]
    prior = [f"state/warp/{r}/{f}" for r in OLD for f in ("halted.json", "run.json")]
    git("init", "-q", "-b", "main")
    for p in product + prior + extra:
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("v0\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    for p in product:
        (tmp_path / p).write_text("work\n", encoding="utf-8")
    git("commit", "-q", "-am", "product")
    landed = git("rev-parse", "HEAD")
    d = tmp_path / "state" / "warp" / RID
    d.mkdir(parents=True, exist_ok=True)
    (d / "halted.json").write_text(json.dumps({
        "status": "halted", "halted": "x", "sizing_rel": None, "run_base_sha": base,
        "run_artifacts": {"withheld": {p: "halt-record-only" for p in product + prior + extra}},
    }), encoding="utf-8")
    for p in extra:
        (tmp_path / p).write_text("dirty\n", encoding="utf-8")
    return landed, product, prior


def test_predecessor_halt_records_do_not_block_the_landing(tmp_path):
    landed, product, prior = _fixture(tmp_path, [])

    out = hrl._handler({"run_id": RID, "sha": landed}, tmp_path)

    assert out["ok"] is True and out["written"] is True, out
    assert out["covers"] == {landed: product}
    assert sorted(out["voided"]) == sorted(prior)


def test_unchanged_product_path_still_refuses(tmp_path):
    landed, _product, _prior = _fixture(tmp_path, ["other.py"])

    out = hrl._handler({"run_id": RID, "sha": landed}, tmp_path)

    assert out["ok"] is False and "did not change withheld other.py" in out["error"]


def test_own_run_record_path_still_refuses(tmp_path):
    own = f"state/warp/{RID}/requirements.md"
    landed, _product, _prior = _fixture(tmp_path, [own])

    out = hrl._handler({"run_id": RID, "sha": landed}, tmp_path)

    assert out["ok"] is False and own in out["error"]


@pytest.mark.parametrize("path,expected", [
    (f"state/warp/{OLD[0]}/halted.json", True),
    (f"state/warp/{RID}/halted.json", False),
    (f"state/warp/{OLD[0]}/sub/x.json", False),
    ("coordinator_core/x.py", False),
])
def test_predecessor_record_predicate(path, expected):
    assert hrl._predecessor_record(path, RID) is expected
