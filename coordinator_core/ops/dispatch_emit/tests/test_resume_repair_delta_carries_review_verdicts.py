"""A refusal-halt resume through the default gate call credits plan review across a repair-class plan delta."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"
REVIEWS = [{"reviewer": "staff-eng", "verdict": "APPROVED"}]
COVERAGE = {"verdict": "OK", "mechanicalFindings": [], "judgmentFindings": []}

_FM = (
    "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-01\n"
    "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
    "capabilities: []\nanchor_premises: []\n"
    f"sizing_object: {REL}\n"
    f"prime_exit_criterion:\n  statement: the op reports per class\n  derived_from: {REL}\n"
    "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n"
)
_ROW = (
    "- id: {id}\n  title: Ship it\n  body: Do the named work and pin it with a test.\n"
    "  change_kind: code-edit\n  surface: {write}\n  writes: [{write}]\n"
    "  reads_at_head:\n{reads}  register_rows: [{reg}]\n  queue_scope: project\n  disposition: open\n"
)


def _plan_text(*, write="coordinator_core/c1.py", reads=("a.py",), reg='"R1"', extra_rows=""):
    reads_yaml = "".join(f"    - {r}\n" for r in reads)
    return _FM + _ROW.format(id="C1", write=write, reads=reads_yaml, reg=reg) + extra_rows + "```\n"


def _test_row():
    return _ROW.format(id="C2", write="coordinator_core/tests/test_c2.py", reads="    - a.py\n", reg='"R1"')


def _put_plan(repo, text):
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _refuse(repo, original, *, with_gate=True):
    """A refusal halt of `original`, with a kept plan_gate built by kept_work.plan_gate."""
    _put(repo, tshirt="S", route="spec-dispatch")
    _put_plan(repo, original)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "coordinator_core").mkdir(exist_ok=True)
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted="refusal", sizing_rel=REL, plan_rel=PLAN_REL)
    halted = {
        "halted": "refusal",
        "plan": PLAN_REL,
        "blitz": {"pulled": [{"planPath": PLAN_REL, "reviews": REVIEWS, "coverage": COVERAGE}]},
    }
    gate = kept_work.plan_gate(halted, repo) if with_gate else None
    if with_gate:
        assert gate is not None and gate["plan_scope"] is not None
        kept_work.write(record, {}, gate)
    return gate


def _reply(repo):
    return ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")["resume_phases"]


@pytest.fixture(autouse=True)
def _fixture(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _ready(monkeypatch):
    """Readiness stubbed to `ready`; the case under test is the delta classification."""
    from coordinator_core.ops import plan_readiness_verdict

    monkeypatch.setattr(plan_readiness_verdict, "_handler", lambda params, repo_root=None: {"verdict": "ready"})


def _credited(phases):
    assert {"plan", "plan_review", "plan_gate_rulings"} <= set(phases["reuse"])
    assert "execute" in phases["rerun"]
    assert phases["verdicts"]["plan_review"]["reviews"] == REVIEWS
    assert phases["verdicts"]["plan_gate_rulings"]["readiness"] == "ready"
    assert phases["verdicts"]["plan_gate_rulings"]["coverage"] == COVERAGE


def _rerun(phases):
    assert {"plan", "plan_review", "plan_gate_rulings"} <= set(phases["rerun"])
    assert not {"plan", "plan_review", "plan_gate_rulings"} & set(phases["reuse"])


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda: _plan_text(reads=("a.py", "b.py")), id="added-reads-path"),
        pytest.param(lambda: _plan_text(reg='"R1", "R2"'), id="added-register-row"),
        pytest.param(lambda: _plan_text(extra_rows=_test_row()), id="added-test-only-row"),
    ],
)
def test_repair_class_delta_reuses_plan_review_and_rulings(repo, monkeypatch, edit):  # noqa: F811
    _refuse(repo, _plan_text())
    _put_plan(repo, edit())
    _ready(monkeypatch)
    _credited(_reply(repo))


def test_unchanged_bytes_reuse_plan_review_and_rulings(repo, monkeypatch):  # noqa: F811
    _refuse(repo, _plan_text())
    _ready(monkeypatch)
    _credited(_reply(repo))


def test_repair_delta_with_real_readiness_credits_the_prior_verdicts(repo):  # noqa: F811
    """Readiness is not stubbed: it runs over the carried reviews against the current bytes."""
    from coordinator_core.ops.plan_readiness_verdict import _handler as readiness

    _refuse(repo, _plan_text())
    _put_plan(repo, _plan_text(reads=("a.py", "b.py")))
    verdict = readiness({"plan": PLAN_REL, "reviews": REVIEWS, "coverage": COVERAGE}, repo_root=repo / ".git")
    assert verdict["verdict"] == "ready", repr(verdict)[-1500:]
    _credited(_reply(repo))


def test_repair_delta_with_unready_plan_reruns(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops import plan_readiness_verdict

    _refuse(repo, _plan_text())
    _put_plan(repo, _plan_text(reads=("a.py", "b.py")))
    monkeypatch.setattr(plan_readiness_verdict, "_handler", lambda params, repo_root=None: {"verdict": "judgment"})
    _rerun(_reply(repo))


def test_scope_bearing_delta_reruns_review_and_rulings(repo, monkeypatch):  # noqa: F811
    _refuse(repo, _plan_text())
    _put_plan(repo, _plan_text(write="coordinator_core/other.py"))
    _ready(monkeypatch)
    _rerun(_reply(repo))


def test_legacy_record_without_plan_scope_keeps_the_exact_sha_rule(repo, monkeypatch):  # noqa: F811
    gate = _refuse(repo, _plan_text())
    legacy = {k: v for k, v in gate.items() if k != "plan_scope"}
    kept_work.write(repo / RECORD_DIR_ROOT / RUN, {}, legacy)
    _put_plan(repo, _plan_text(reads=("a.py", "b.py")))
    _ready(monkeypatch)
    _rerun(_reply(repo))
    _put_plan(repo, _plan_text())
    _credited(_reply(repo))


def test_refusal_without_a_kept_plan_gate_reruns_everything(repo, monkeypatch):  # noqa: F811
    _refuse(repo, _plan_text(), with_gate=False)
    _ready(monkeypatch)
    phases = _reply(repo)
    assert phases["reuse"] == [] and phases["verdicts"] == {}
