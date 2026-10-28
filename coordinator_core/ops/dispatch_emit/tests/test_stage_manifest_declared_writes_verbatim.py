"""An asks entry's declared writes reach the manifest verbatim; no path absent from the tree is invented."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import stage

SIZING_REL = "state/sizings/2026-10-27-fixture.yaml"
TESTS = "tests/remote_compute"
N = 30
SOURCES = ("remote_compute/bands.py", "remote_compute/repair.py", "remote_compute/provider.py")


def _sizing(repo: Path) -> None:
    doc = {
        "intent": "Cover every band.",
        "exit_criterion": {"statement": "all band tests pass"},
        "estimate": {"tshirt": "XS"},
    }
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _tests(n: int = N) -> list[str]:
    return [f"{TESTS}/test_band_{i:02d}.py" for i in range(n)]


def _on_disk(repo: Path, paths: list[str]) -> None:
    for p in paths:
        f = repo / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("", encoding="utf-8")


def _stage(repo: Path, writes: list[str]):
    _sizing(repo)
    return stage(repo, run_id="r1", sizing_rel=SIZING_REL, writes=writes)


def _x1(m):
    return next(r for r in m.rows if r.id == "X1")


def test_n_declared_test_writes_reach_every_manifest_surface(tmp_path):
    writes = _tests()
    _on_disk(tmp_path, writes)
    m = _stage(tmp_path, writes)
    assert set(writes) <= set(_x1(m).writes)
    assert set(writes) <= set(m.review_declared_paths)
    assert set(writes) <= set(m.test_targets)


def test_declared_writes_survive_when_none_exist_yet(tmp_path):
    writes = _tests()
    m = _stage(tmp_path, writes)
    assert set(writes) <= set(_x1(m).writes)
    assert set(writes) <= set(m.review_declared_paths)
    assert set(writes) <= set(m.test_targets)


def test_no_path_absent_from_the_tree_is_invented_beside_declared_sources(tmp_path):
    writes = [*_tests(), *SOURCES]
    _on_disk(tmp_path, writes)
    m = _stage(tmp_path, writes)
    emitted = {*_x1(m).writes, *m.review_declared_paths, *m.test_targets}
    invented = {p for p in emitted if p not in writes and not (tmp_path / p).is_file()}
    assert invented == set()
    assert set(writes) <= set(_x1(m).writes)
    assert set(writes) <= set(m.review_declared_paths)


def test_a_derived_test_that_exists_is_still_widened_in(tmp_path):
    existing = "remote_compute/tests/test_bands.py"
    _on_disk(tmp_path, [existing, *SOURCES])
    m = _stage(tmp_path, list(SOURCES))
    assert existing in _x1(m).writes
