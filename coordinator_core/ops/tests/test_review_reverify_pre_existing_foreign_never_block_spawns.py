"""The ea06ca shape through `review.reverify` revert mode: six reds, four red at the run base and two
caused by a sibling's dirty file. Handed the base export and the failure text, the op classes the four
`pre_existing` and the two `foreign_red`, and `from_reverify` finds no blocker. Without either input the
same six stay blockers, so the classification rides on what the caller passes.

Trap: `land_run.foreign_halted_withheld` and the HEAD blob lookup are stubbed, as in the sibling-owned tests.
Trap: the first four tests hand-write the base export; the last two drive `reverify_base_measure.main` on a real
git fixture repo, including ea06ca's shape (ids absent at the base commit, so the base run exits 4)."""

from __future__ import annotations

import subprocess

import pytest
from pathlib import Path

_REAL_POPEN = subprocess.Popen  # the `world` fixture swaps Popen for a refusal; measuring needs the real one

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.ops.tests.test_review_reverify_sibling_owned import SIBLING_FILE, _dirty_sibling

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

AT_BASE = [f"bench/tests/test_slow.py::test_red_{i}" for i in range(4)]
SIBLING_DIRT = [f"bench/tests/test_census.py::test_dirt[{i}]" for i in range(2)]
TRACE = f"bench/tests/test_census.py:9: in test_dirt\n    {SIBLING_FILE}:4: SyntaxError\n"
FAILING = [*AT_BASE, *SIBLING_DIRT]
EXPORT = "scratch/reverify-base-export-run.txt"


def _revert(root, **kw):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=FAILING, run_id="me", **kw
    )


def _output() -> dict[str, str]:
    return {n: TRACE for n in SIBLING_DIRT}


_SLOW = "".join(f"def test_red_{i}():\n    assert False\n\n\n" for i in range(4))
_CENSUS = "import pytest\n\n\n@pytest.mark.parametrize('i', [0, 1])\ndef test_dirt(i):\n    assert True\n"


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo, files, message) -> str:
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _measure_export(tmp_path, monkeypatch, *, ids_at_base):
    """Run `reverify_base_measure.main` in a real fixture repo and return the export file's text.
    With `ids_at_base` False the six ids first appear in the head commit, so the base run exits 4."""
    from coordinator_core.ops import reverify_base_measure as rbm

    refusal = subprocess.Popen
    repo = tmp_path.parent / (tmp_path.name + "-fixture")
    repo.mkdir()
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    try:
        _git(repo, "init", "-q")
        tests = {"bench/tests/test_slow.py": _SLOW, "bench/tests/test_census.py": _CENSUS}
        base = _commit(repo, tests if ids_at_base else {"README.md": "x\n"}, "base")
        head = base if ids_at_base else _commit(repo, tests, "add the six")
        monkeypatch.chdir(repo)
        assert rbm.main(["--sha", base, "--head-sha", head, "--out", "measured.txt", "--", *FAILING]) == 0
    finally:
        monkeypatch.setattr(subprocess, "Popen", refusal)
    return (repo / "measured.txt").read_text(encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.parametrize("ids_at_base", [True, False], ids=["ids-at-base", "ids-added-after-base"])
def test_measure_on_a_real_git_fixture_feeds_review_reverify_pre_existing_and_foreign(
    world, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ids_at_base
) -> None:
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    text = _measure_export(tmp_path, monkeypatch, ids_at_base=ids_at_base)
    assert sorted(ln[len("FAILED "):].split(" - ")[0] for ln in text.splitlines() if ln.startswith("FAILED ")) == sorted(AT_BASE)
    (root / EXPORT).parent.mkdir(parents=True, exist_ok=True)
    (root / EXPORT).write_text(text, encoding="utf-8")
    out = _revert(root, base_export_path=EXPORT, failing_output=_output())
    assert out["error"] is None and out["base_unmeasured"] is False
    assert out["pre_existing"] == sorted(AT_BASE)
    assert sorted(out["foreign_red"]) == sorted(SIBLING_DIRT)
    assert out["unattributed"] == [] and out["unmeasured"] == []
    assert from_reverify({**out, "failing": FAILING}) == []
