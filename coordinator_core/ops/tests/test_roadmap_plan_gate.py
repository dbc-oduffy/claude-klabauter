"""
coordinator_core/ops/tests/test_roadmap_plan_gate.py — the "roadmap.plan_gate" op.

Subject: `coordinator_core.ops.roadmap_plan_gate`, the RPC wrapper over the
two-gate resolver. The resolver's own semantics are pinned in
`coordinator_core/roadmap/tests/test_plan_gate.py`; what this module pins is the
WIRE: registration, param validation, and the one-baton `verdict` a caller reads
instead of re-deriving the gate from the `batons` list.

Zero spawns; every case builds its corpus in `tmp_path`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops import roadmap_plan_gate as op_module


def _write(path: Path, frontmatter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter.strip()}\n---\n\nbody\n", encoding="utf-8")


def _baton(root: Path, stub_id: str, extra: str = "") -> None:
    _write(
        root / "state" / "handoffs" / f"{stub_id}.md",
        f"kind: roadmap-baton\ntitle: {stub_id}\nstub_id: {stub_id}\n"
        f"status: open\ndeployment_state: ready_to_fire\nbaton_role: work\n{extra}",
    )


def _call(root: Path, **params):
    return asyncio.run(op_module._handler(params, repo_root=root))


@pytest.fixture()
def corpus(tmp_path):
    """A blocker whose plan cleared review, and the dependent it no longer holds
    back from PLANNING but still holds back from EXECUTION. This is the whole
    point of the op in four files."""
    _write(
        tmp_path / "docs" / "plans" / "blocker.md",
        "title: blocker plan\nstatus: approved\nplan_id: pln-blocker",
    )
    _baton(tmp_path, "blocker-1", "origin_plan_id: pln-blocker")
    _baton(tmp_path, "dependent-1", "blocked_by: [blocker-1]")
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def test_the_op_is_registered_under_its_wire_name():
    from coordinator_core.ipc import _REGISTRY

    assert "roadmap.plan_gate" in _REGISTRY


def test_the_op_is_reachable_through_the_lazy_import_map():
    """A missing map entry degrades to a full-package import rather than a
    broken dispatch, so this is a cost gate, not a correctness one — but a
    silently-unmapped op re-imports ~80 modules on every call."""
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP["roadmap.plan_gate"] == "coordinator_core.ops.roadmap_plan_gate"


def test_the_op_declares_a_key_scope():
    """An op reading main-worktree-rooted `state/` must be keyed "common_dir",
    or a call from a linked worktree resolves that worktree's own empty tree and
    returns a confident, well-formed empty answer."""
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert _OP_KEY_SCOPE["roadmap.plan_gate"] == "common_dir"


def test_the_op_writes_nothing(corpus):
    before = {p: p.read_bytes() for p in corpus.rglob("*.md")}
    _call(corpus)
    after = {p: p.read_bytes() for p in corpus.rglob("*.md")}
    assert before == after


def test_whole_repo_report_carries_both_gates_per_baton(corpus):
    report = _call(corpus)

    dependent = next(b for b in report["batons"] if b["id"] == "dependent-1")
    assert dependent["planning_gate"]["open"] is True
    assert dependent["execution_gate"]["open"] is False
    assert report["verdict"] is None, "no subject means no single-baton verdict"


@pytest.mark.parametrize(
    "gate, expected_open",
    [("planning", True), ("execution", False)],
)
def test_subject_verdict_answers_the_admission_question(corpus, gate, expected_open):
    report = _call(corpus, subject="dependent-1", gate=gate)

    assert report["verdict"]["resolved"] is True
    assert report["verdict"]["gate"] == gate
    assert report["verdict"]["open"] is expected_open


def test_subject_verdict_defaults_to_reporting_both_gates(corpus):
    verdict = _call(corpus, subject="dependent-1")["verdict"]

    assert verdict["planning_gate"]["open"] is True
    assert verdict["execution_gate"]["open"] is False


def test_an_unknown_subject_is_refused_rather_than_reported_open(corpus):
    """Silence would read as "no gate holds this". A caller asking about a baton
    that does not exist must not be told to proceed."""
    verdict = _call(corpus, subject="not-a-baton", gate="execution")["verdict"]

    assert verdict["resolved"] is False
    assert verdict["open"] is False


def test_subject_narrows_the_report_but_not_the_scan(corpus):
    """A baton's gates are a function of the whole corpus, so a subject filter
    must not shrink what the resolver reads — only what it prints."""
    full = _call(corpus)
    narrowed = _call(corpus, subject="dependent-1")

    assert len(narrowed["batons"]) == 1
    assert narrowed["scanned"] == full["scanned"]
    assert narrowed["waves"] == full["waves"]
    # A count whose denominator moves with a display filter is a count nobody can act on:
    # "candidates: 2" beside "planning_open: 0" would read as "none of the 2".
    assert narrowed["counts"] == full["counts"]


def test_roadmap_id_filters_the_candidate_set(tmp_path):
    (tmp_path / ".git").mkdir()
    _baton(tmp_path, "in-scope-1", "roadmap_id: alpha")
    _baton(tmp_path, "out-of-scope-1", "roadmap_id: beta")

    report = _call(tmp_path, roadmap_id="alpha")

    assert [b["id"] for b in report["batons"]] == ["in-scope-1"]


@pytest.mark.parametrize(
    "params",
    [
        {"gate": "whenever"},
        {"subject": 7},
        {"roadmap_id": ["alpha"]},
    ],
)
def test_bad_params_raise_rather_than_degrade(corpus, params):
    with pytest.raises(ValueError):
        _call(corpus, **params)


def test_a_missing_repo_root_is_refused(corpus):
    """No cwd fallback: deriving a root from where the caller happened to stand
    makes the answer depend on the caller's directory."""
    with pytest.raises(ValueError, match="repo_root"):
        asyncio.run(op_module._handler({}, repo_root=None))
