"""rea_ledger.write: registration, canonical-import refusal, chain checks, path safety, append-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.rea_ledger_write import _rea_ledger_write as write
from coordinator_core.rea_ledger.canonical import canonical_bytes, revision_digest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "contract" / "vendored" / "unknown-revision" / "v1" / "vectors.json")
    .read_text(encoding="utf-8")
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _text(rec: dict) -> str:
    return canonical_bytes(rec).decode("utf-8")


def _unknown(rev: int, prev, uid: str = "u-1", **over) -> dict:
    rec = {
        "unknown_id": uid, "revision": rev, "previous_revision_digest": prev, "question": "q?",
        "coverage": "partial", "coverage_scope": "claim", "epistemic_mode": "observed",
        "source_authority": "shipped-artifact", "observed_at": "2026-11-01T12:00:00+00:00",
    }
    rec.update(over)
    return rec


def _ledger(repo) -> Path:
    return repo / "state" / "rea-ledger"


_RACER = (
    "import sys, json; from pathlib import Path; "
    "from coordinator_core.ops.rea_ledger_write import _rea_ledger_write as w; "
    "print(json.dumps(w({'kind': 'unknown_revision', 'records': [sys.argv[2]]}, repo_root=Path(sys.argv[1]))))"
)


def test_concurrent_appends_of_the_same_revision_land_exactly_once(repo):
    import subprocess
    import sys

    r1 = _unknown(1, None)
    assert write({"kind": "unknown_revision", "records": [_text(r1)]}, repo_root=repo)["written"] == 1
    prev = revision_digest(r1)
    root = Path(__file__).resolve().parents[3]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, str(repo), _text(_unknown(2, prev, question=f"racer-{n}"))],
            cwd=root, stdout=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for n in range(2)
    ]
    results = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert sorted("error" in r for r in results) == [False, True], results
    path = _ledger(repo) / "unknown_revision" / "u-1.jsonl"
    assert path.read_bytes().count(b"\n") == 2
