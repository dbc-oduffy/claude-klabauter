"""The brief's corpus scans open only what the session-start floor admits.

Runs `workstream_complete.brief` in-process on a scaled copy of the budget gate's fixture with an
open hook. Counted, not timed: every corpus file the brief opens is an above-floor file, a
ledger-named file, or a handoff the stale leg's claim index had to re-read.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest

from coordinator_core import workstream_complete
from coordinator_core.benchmarks import workstream_complete_brief_full_path as fx
from coordinator_core.session import handoff_claim_index, stale_claims

_SCALED_DIRS = (
    ("archive/handoffs", 200),
    ("state/handoffs", 200),
    ("state/cross-repo/inbox", 200),
    ("state/cross-repo/archive", 200),
)
_HANDOFFS = "state/handoffs"


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(fx, "HISTORY_COMMITS", 300)
    mp.setattr(fx, "WINDOW", 40)
    mp.setattr(fx, "CORPUS_DIRS", _SCALED_DIRS)
    try:
        yield fx.ensure_fixture(tmp_path_factory.mktemp("brief_scan_opens"))
    finally:
        mp.undo()


@pytest.fixture
def opens(monkeypatch):
    """Path argument of every `open` / `io.open` / `Path.open` call made while the fixture lives (unresolved)."""
    seen: list[Path] = []
    real = builtins.open

    def spy(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)):
            seen.append(Path(os.fsdecode(file)))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    return seen


def _corpus_opens(opens, repo: Path) -> list[Path]:
    root = repo.resolve()
    out = []
    for p in opens:
        try:
            rel = (p if p.is_absolute() else Path.cwd() / p).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if any(rel.startswith(d + "/") for d, _ in _SCALED_DIRS):
            out.append(root / rel)
    return out


def _handoff_opens(opens, repo: Path) -> set[Path]:
    return {
        p for p in _corpus_opens(opens, repo)
        if p.parent.name == "handoffs" and p.parent.parent.name == "state"
    }


def _touched(repo: Path) -> set[Path]:
    root = repo.resolve()
    return {
        root / f"{d}/2026-01-01-h{i}.md"
        for d, count in _SCALED_DIRS
        for i in range(fx._touched_count(count))
    }


def _run_brief(repo: Path) -> None:
    envelope = workstream_complete.brief({"stage_paths": []}, repo_root=repo)
    assert (envelope.get("gates") or {}).get("run_close"), "brief did not run"


def test_warm_brief_opens_only_the_above_floor_touched_set(fixture_repo, opens, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", fx.SESSION_ID)
    _run_brief(fixture_repo)  # builds the claim index
    opens.clear()
    _run_brief(fixture_repo)
    opened = set(_corpus_opens(opens, fixture_repo))
    ledger = {
        fixture_repo.resolve() / _HANDOFFS / name
        for name in stale_claims._ledger_names(stale_claims.git_common_dir(fixture_repo))
    }
    # Detector C reads the scope paths of each stale-claimed handoff; that read is not floored.
    stale_claimed = {
        Path(e.path).resolve() for e in stale_claims.list_stale_claim_handoffs(str(fixture_repo))
    }
    assert stale_claimed, "fixture has no stale-claimed handoff; the allowance below would be vacuous"
    expected = _touched(fixture_repo) | stale_claimed | (ledger & opened)
    assert opened == expected, (
        f"unexpected corpus opens: {sorted(p.name for p in opened ^ expected)[:10]}"
    )


def test_cold_brief_opens_every_handoff_head_to_build_the_index(fixture_repo, opens, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", fx.SESSION_ID)
    cache = handoff_claim_index.cache_path_for(stale_claims.git_common_dir(fixture_repo))
    cache.unlink(missing_ok=True)
    _run_brief(fixture_repo)
    handoffs = _handoff_opens(opens, fixture_repo)
    all_handoffs = set((fixture_repo.resolve() / _HANDOFFS).glob("*.md"))
    assert handoffs == all_handoffs


def test_second_stale_enumeration_opens_no_handoff_head(fixture_repo, opens):
    stale_claims.list_stale_claim_handoffs(str(fixture_repo))
    opens.clear()
    stale_claims.list_stale_claim_handoffs(str(fixture_repo))
    assert _handoff_opens(opens, fixture_repo) == set()
