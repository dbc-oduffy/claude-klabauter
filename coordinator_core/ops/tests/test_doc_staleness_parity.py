"""Regression pin of the doc-staleness report on a deterministic fixture.

The baseline in data/doc_staleness_parity_baseline.json is the full report, every entry field
included, for fixture repos whose commit shas reproduce on any box. Regenerate it with
`PYTHONPATH=. python <this file> --print` from the repo root, only for a deliberate output change.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import test_doc_staleness as fx
from coordinator_core.ops.doc_staleness import build_doc_staleness_report

pytestmark = [pytest.mark.spawns_process]

BASELINE = Path(__file__).parent / "data" / "doc_staleness_parity_baseline.json"
BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)
THRESHOLD_COMMITS = 3
THRESHOLD_DAYS = 21
_PINNED_ENV = {
    "GIT_AUTHOR_NAME": "Parity",
    "GIT_AUTHOR_EMAIL": "parity@example.invalid",
    "GIT_COMMITTER_NAME": "Parity",
    "GIT_COMMITTER_EMAIL": "parity@example.invalid",
}

MAIN_DOCS = [
    "stale.md",
    "commits.md",
    "sweep.md",
    "ws.md",
    "link.md",
    "RENAMED.md",
    "missing.md",
    "sweeponly.md",
]
DAYS_DOCS = ["days.md"]


def _body(name: str, **kw) -> str:
    return fx._doc([f"Doc {name}."], **kw)


def _build_main(repo: Path) -> None:
    fx._init(repo)
    fx._commit(
        repo,
        {n: _body(n) for n in ("stale.md", "sweep.md", "ws.md", "link.md", "OLD.md")},
        100,
        "authored",
    )
    sweep = {f"src/f{i}.txt": "x\n" for i in range(10)}
    sweep["sweeponly.md"] = "short\n"
    fx._commit(repo, sweep, 95, "sweep only")
    fx._fillers(repo, 4, 30)
    drive_by = {f"src/g{i}.txt": "x\n" for i in range(10)}
    drive_by["sweep.md"] = fx._doc(["Doc sweep.md.", "Swept line."])
    fx._commit(repo, drive_by, 25, "drive-by")
    spaced = _body("ws.md").replace("Line 3 of the guide.", "Line 3 of the guide.   ") + "\n\n"
    fx._commit(repo, {"ws.md": spaced}, 24, "whitespace")
    relinked = _body("link.md", link="See [the guide](docs/new.md) for more.")
    fx._commit(repo, {"link.md": relinked}, 23, "relink")
    fx._git(repo, "mv", "OLD.md", "RENAMED.md")
    fx._git(repo, "commit", "-q", "-m", "rename", when=fx._at(22))
    fx._commit(
        repo, {"commits.md": _body("commits.md")}, 8, "authored recently"
    )
    fx._fillers(repo, 4, 5)


def _build_days(repo: Path) -> None:
    fx._init(repo)
    fx._commit(repo, {"days.md": _body("days.md")}, 90, "authored")
    fx._fillers(repo, 2, 30)


def build_report(root: Path) -> dict:
    main, days = root / "main", root / "days"
    _build_main(main)
    _build_days(days)
    docs = build_doc_staleness_report(
        main, MAIN_DOCS, THRESHOLD_COMMITS, THRESHOLD_DAYS, today=TODAY
    )["docs"]
    docs += build_doc_staleness_report(
        days, DAYS_DOCS, THRESHOLD_COMMITS, THRESHOLD_DAYS, today=TODAY
    )["docs"]
    return {"docs": docs}


@pytest.fixture
def pinned(monkeypatch):
    for key, value in _PINNED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(fx, "_at", lambda days_ago: BASE - timedelta(days=days_ago))


def test_report_matches_the_baseline(tmp_path, pinned):
    got = build_report(tmp_path)
    assert got == json.loads(BASELINE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    if "--print" not in sys.argv:
        raise SystemExit("usage: test_doc_staleness_parity.py --print")
    import os
    import tempfile

    os.environ.update(_PINNED_ENV)
    fx._at = lambda days_ago: BASE - timedelta(days=days_ago)
    with tempfile.TemporaryDirectory() as tmp:
        sys.stdout.write(json.dumps(build_report(Path(tmp)), indent=2, sort_keys=True) + "\n")
