"""
coordinator_core/ops/tests/test_plan_mise_prep_probe_set.py — the set form of
"plan.mise_prep_probe" and its on-disk verdict store.

Subject: `coordinator_core.ops.plan_mise_prep_probe` (`_probe_set`, `_probe_one`). Every path is
under tmp_path. Files are aged to the epoch after they are written so the store's racy-clean
rule trusts them; the cache directory exists before the first call so saving the store never
moves the root directory's mtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_mise_prep_probe as mod
from coordinator_core.ops.dispatch_emit import spine_read
from coordinator_core.ops.tests.test_plan_mise_prep_probe import (
    _DERIVABLE_FM,
    _PREPPED_FM,
    _SPINE,
    _plan,
    _repo,
)
from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap import probe_store

PREPPED = "docs/plans/2026-09-07-prepped.md"
UPGRADEABLE = "docs/plans/2026-09-07-upgradeable.md"
SCHEMA_ONLY = "docs/plans/2026-09-07-schema-only.md"
OTHER_SIZING = "state/sizings/other.yaml"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    mod._CORPUS_CACHE.clear()
    monkeypatch.setattr(mod, "engine_key", lambda sha: "engine-key-1")
    real = mod.gate_plan_with_corpus

    def gate(plan_path, corpus, *, text=None):
        report = real(plan_path, corpus, text=text)
        if "schema-only" in plan_path.name:
            report["verdict"] = pg.NOT_PREPPED
            report["classes"] = {
                name: {"status": "PASS", "kind": None, "detail": "", "withheld": []}
                for name in report["classes"]
            }
            report["classes"]["SCHEMA"] = {
                "status": "DEFECT", "kind": "x", "detail": "d", "withheld": [],
            }
        return report

    monkeypatch.setattr(mod, "gate_plan_with_corpus", gate)
    yield
    mod._CORPUS_CACHE.clear()


def _age(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(os.path.join(dirpath, name), ns=(10**9, 10**9))
    os.utime(root, ns=(10**9, 10**9))


def _tree(tmp_path: Path) -> Path:
    common = _repo(tmp_path)
    (tmp_path / ".coordinator-local" / "cache").mkdir(parents=True)
    for rel in ("state/sizings/2026-09-07-fixture.yaml", OTHER_SIZING):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("register: []\n", encoding="utf-8")
    _plan(tmp_path, frontmatter=_PREPPED_FM, spine=_SPINE, name=PREPPED)
    _plan(tmp_path, frontmatter=_DERIVABLE_FM, spine=None, name=UPGRADEABLE)
    _plan(
        tmp_path,
        frontmatter=_PREPPED_FM.replace("2026-09-07-fixture.yaml", "other.yaml"),
        spine=_SPINE,
        name=SCHEMA_ONLY,
    )
    _age(tmp_path)
    return common


def _set(common: Path, plans: list, **extra) -> dict:
    return mod._probe_handler({"plans": plans, **extra}, common)


def _single(common: Path, plan: str, **extra) -> dict:
    reply = mod._probe_handler({"plan": plan, **extra}, common)
    reply.pop("process_ms")
    return reply


def _store_file(root: Path) -> Path:
    return probe_store.store_path(root)


PLANS = [PREPPED, UPGRADEABLE, SCHEMA_ONLY]


def test_each_set_entry_equals_the_single_form_reply(tmp_path):
    common = _tree(tmp_path)
    reply = _set(common, PLANS, upgrade=True)
    assert set(reply) == {"probes", "store_hits", "store_misses", "process_ms"}
    for plan in PLANS:
        assert reply["probes"][plan] == _single(common, plan, upgrade=True)
    assert reply["probes"][PREPPED]["gate"]["verdict"] == pg.PREPPED
    assert reply["probes"][UPGRADEABLE]["upgrade"]["changed"] is True
    assert reply["probes"][SCHEMA_ONLY]["upgrade"] == {"skipped": "not upgradeable"}


def test_corpus_and_engine_build_are_each_called_once_per_set(tmp_path, monkeypatch):
    common = _tree(tmp_path)
    corpus_calls, build_calls = [], []
    real_corpus, real_build = mod._corpus, mod.engine_build
    monkeypatch.setattr(mod, "_corpus", lambda root: corpus_calls.append(root) or real_corpus(root))
    monkeypatch.setattr(mod, "engine_build", lambda: build_calls.append(1) or real_build())
    _set(common, PLANS)
    assert len(corpus_calls) == 1
    assert len(build_calls) == 1


def test_an_absent_plan_and_an_escaping_plan_get_error_entries(tmp_path):
    common = _tree(tmp_path)
    escaping = str(tmp_path.parent / "elsewhere.md")
    reply = _set(common, ["docs/plans/absent.md", PREPPED, escaping])
    assert "no such plan" in reply["probes"]["docs/plans/absent.md"]["error"]
    assert "escapes" in reply["probes"][escaping]["error"]
    assert reply["probes"][PREPPED]["gate"]["verdict"] == pg.PREPPED
    assert reply["store_misses"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"plan": PREPPED, "plans": [PREPPED]},
        {},
        {"plans": PREPPED},
        {"plans": []},
        {"plans": [PREPPED, 7]},
        {"plans": [PREPPED, "  "]},
    ],
)
def test_malformed_selectors_raise_value_error(tmp_path, params):
    common = _tree(tmp_path)
    with pytest.raises(ValueError):
        mod._probe_handler(params, common)


def test_the_set_form_writes_no_plan_file_and_only_the_store(tmp_path):
    common = _tree(tmp_path)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (tmp_path / "docs").rglob("*.md")}
    _set(common, PLANS, upgrade=True)
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (tmp_path / "docs").rglob("*.md")}
    assert after == before
    assert _store_file(tmp_path).is_file()
    written = {
        p for p in tmp_path.rglob("*")
        if p.is_file() and p.stat().st_mtime_ns != 10**9
    }
    assert written == {_store_file(tmp_path)}


def test_a_second_set_call_at_an_unchanged_tree_parses_no_yaml(tmp_path, monkeypatch):
    common = _tree(tmp_path)
    first = _set(common, PLANS, upgrade=True)
    assert (first["store_hits"], first["store_misses"]) == (0, 3)

    loads = []
    for name in ("load", "safe_load"):
        real = getattr(yaml, name)
        monkeypatch.setattr(yaml, name, lambda *a, _r=real, _n=name, **k: loads.append(_n) or _r(*a, **k))
    for memo in (spine_read._FM_DOCS, spine_read._FM_SPLITS, spine_read._ROWS_MEMO):
        memo.clear()
    mod._CORPUS_CACHE.clear()

    second = _set(common, PLANS, upgrade=True)
    assert loads == []
    assert second["store_hits"] == len(PLANS)
    assert second["store_misses"] == 0
    assert second["probes"] == first["probes"]


def _warm(common: Path) -> None:
    assert _set(common, PLANS, upgrade=True)["store_misses"] == 3


def test_editing_one_plan_body_misses_exactly_that_plan(tmp_path):
    common = _tree(tmp_path)
    _warm(common)
    path = tmp_path / PREPPED
    path.write_text(path.read_text(encoding="utf-8") + "\nmore prose\n", encoding="utf-8")
    _age(tmp_path)
    reply = _set(common, PLANS, upgrade=True)
    assert (reply["store_hits"], reply["store_misses"]) == (2, 1)


def test_editing_one_plan_frontmatter_misses_exactly_that_plan(tmp_path):
    common = _tree(tmp_path)
    _warm(common)
    path = tmp_path / UPGRADEABLE
    path.write_text(path.read_text(encoding="utf-8").replace("status: draft", "status: active"),
                    encoding="utf-8")
    _age(tmp_path)
    reply = _set(common, PLANS, upgrade=True)
    assert (reply["store_hits"], reply["store_misses"]) == (2, 1)


def test_editing_a_named_sizing_misses_exactly_the_plan_naming_it(tmp_path):
    common = _tree(tmp_path)
    _warm(common)
    (tmp_path / OTHER_SIZING).write_text("register: [changed]\n", encoding="utf-8")
    reply = _set(common, PLANS, upgrade=True)
    assert (reply["store_hits"], reply["store_misses"]) == (2, 1)


def test_a_moved_head_misses_every_plan(tmp_path):
    common = _tree(tmp_path)
    _warm(common)
    (common / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="utf-8")
    reply = _set(common, PLANS, upgrade=True)
    assert (reply["store_hits"], reply["store_misses"]) == (0, 3)


def test_the_upgrade_flag_is_a_key_leg(tmp_path):
    common = _tree(tmp_path)
    _warm(common)
    reply = _set(common, PLANS, upgrade=False)
    assert (reply["store_hits"], reply["store_misses"]) == (0, 3)


def test_a_none_engine_key_misses_every_plan_and_writes_no_store(tmp_path, monkeypatch):
    common = _tree(tmp_path)
    monkeypatch.setattr(mod, "engine_key", lambda sha: None)
    for _ in range(2):
        reply = _set(common, PLANS, upgrade=True)
        assert (reply["store_hits"], reply["store_misses"]) == (0, 3)
    assert not _store_file(tmp_path).exists()


def test_a_corrupt_store_answers_as_a_cold_call(tmp_path):
    common = _tree(tmp_path)
    _warm(common)
    _store_file(tmp_path).write_text("{not json", encoding="utf-8")
    reply = _set(common, PLANS, upgrade=True)
    assert (reply["store_hits"], reply["store_misses"]) == (0, 3)
    assert _set(common, PLANS, upgrade=True)["store_hits"] == 3


def test_an_error_plan_is_never_stored(tmp_path):
    common = _tree(tmp_path)
    _set(common, ["docs/plans/absent.md", PREPPED])
    store = probe_store.ProbeStore.load(tmp_path)
    assert store._records.keys() == {f"{PREPPED}|0"}


def test_the_single_form_neither_reads_nor_writes_the_store(tmp_path):
    common = _tree(tmp_path)
    reply = mod._probe_handler({"plan": PREPPED, "upgrade": True}, common)
    assert set(reply) == {"gate", "census", "upgrade", "process_ms"}
    assert not _store_file(tmp_path).exists()
