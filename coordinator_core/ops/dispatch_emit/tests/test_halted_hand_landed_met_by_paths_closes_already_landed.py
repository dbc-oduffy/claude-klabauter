"""A halted run whose declared product was hand-committed closes already_landed, with no new commit, when the
refire's met rows name those paths in met_by; a row naming no declared path, or a path HEAD does not hold
byte for byte, stays refused and marks the refusal refire_futile."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import pre_halt_ancestry as pha
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    B,
    RUN2,
    _composed,
    _git,
    _refire,
    _write,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = reviewed()
REFUSED = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _verdict(*met_by) -> dict:
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": list(met_by)}]}


def _terminal(root, token, resume, verdict) -> dict:
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def _hand_landed(root) -> tuple:
    """Hand-commit the declared paths after the halt, with no owner named, then stage the refire whose
    base (`since_sha`) is that commit. Returns `(resume, refire, hand_sha)`."""
    _write(root, "a.py", "a = 7\n")
    _write(root, B, "b = 7\n")
    _git(root, "add", "a.py", B)
    _git(root, "commit", "-q", "-m", "hand land the withheld paths")
    hand = _git(root, "rev-parse", "HEAD").strip()
    resume = {**_composed(root), "since_sha": hand}
    return resume, _refire(root, edit=False), hand


def test_hand_committed_paths_named_in_met_by_close_already_landed_with_no_new_commit(halted):  # noqa: F811
    resume, refire, hand = _hand_landed(halted)

    out = _terminal(halted, refire["token"], resume, _verdict("a.py:1", f"{B}@abc1234"))

    assert out.get("refused") is None, out
    assert out["committed"] is True and out[ALREADY_LANDED_KEY] is True, out
    assert out["sha"] == hand and _git(halted, "rev-parse", "HEAD").strip() == hand
    again = _terminal(halted, refire["token"], resume, _verdict("a.py:1", f"{B}@abc1234"))
    assert again["committed"] is True and again["sha"] == hand, again


def test_met_by_naming_no_declared_path_stays_refused_and_marks_the_refusal_futile(halted):  # noqa: F811
    resume, refire, hand = _hand_landed(halted)
    out = _terminal(halted, refire["token"], resume, _verdict("docs/elsewhere.md", "the tests pass"))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == REFUSED
    assert out["refire_futile"] is True
    assert not out.get(ALREADY_LANDED_KEY)
    assert _git(halted, "rev-parse", "HEAD").strip() == hand


def test_without_the_credit_the_same_refire_refuses_futile(halted, monkeypatch):  # noqa: F811
    resume, refire, _hand = _hand_landed(halted)
    monkeypatch.setattr(pha, "met_by_paths_at_head_credit", lambda *a, **k: frozenset())

    out = _terminal(halted, refire["token"], resume, _verdict("a.py"))

    assert out["refused"] == "ledger-unmet" and out["refire_futile"] is True, out


def test_a_refusal_with_another_reason_is_not_futile(halted):  # noqa: F811
    resume, refire, _hand = _hand_landed(halted)
    out = _terminal(halted, refire["token"], resume, {"items": [{"id": "R1", "status": "not_met"}]})

    assert out["refused"] == "ledger-unmet", out
    assert "refire_futile" not in out


@pytest.fixture
def small(tmp_path):
    def git(*a):
        return subprocess.run(
            ["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True, **no_console_creationflags()
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    for name in ("p.py", "q.py", "other.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    (tmp_path / "p.py").write_text("x = 2\n", encoding="utf-8")
    git("commit", "-q", "-am", "change p")
    return tmp_path, git


def _met(**by):
    return [{"id": rid, "status": "met", "met_by": [text]} for rid, text in by.items()]


def test_credit_requires_every_named_path_tracked_and_identical(small):
    root, git = small
    (root / "ignored.py").write_text("i = 1\n", encoding="utf-8")
    (root / "new.py").write_text("n = 1\n", encoding="utf-8")
    (root / "q.py").write_text("x = 3\n", encoding="utf-8")
    declared = ["p.py", "q.py", "ignored.py", "new.py", "missing.py"]
    items = _met(A="p.py", B="p.py q.py", C="ignored.py", D="new.py", E="missing.py", F="no path here")

    got = pha.met_by_paths_at_head_credit(root, items, list("ABCDEF"), declared)

    assert got == frozenset({"A"})


def test_credit_fills_detail_only_when_every_id_is_credited(small):
    root, git = small
    declared = ["p.py", "q.py"]
    full: dict = {}
    assert pha.met_by_paths_at_head_credit(root, _met(A="p.py:3", B="./q.py@abc1234"), ["A", "B"], declared, full)
    assert full["landed_sha"] == git("log", "-1", "--format=%H", "--", "p.py", "q.py")
    assert full["landed_paths"] == ["p.py", "q.py"]
    assert set(full[pha.CITED_DETAIL_KEY]) == {"A", "B"} and set(full[pha.PATH_CREDIT_DETAIL_KEY]) == {"A", "B"}
    part: dict = {}
    assert pha.met_by_paths_at_head_credit(root, _met(A="p.py", B="nothing"), ["A", "B"], declared, part) == {"A"}
    assert part == {}


def test_credit_spawns_once_over_the_union_of_rows(small, monkeypatch):
    root, _git_ = small
    calls: list = []
    real = pha.run_git
    monkeypatch.setattr(pha, "run_git", lambda args, **k: calls.append(args) or real(args, **k))

    pha.met_by_paths_at_head_credit(root, _met(A="p.py", B="q.py", C="p.py q.py"), ["A", "B", "C"], ["p.py", "q.py"])

    assert len(calls) == 2


def test_credit_names_nothing_for_a_path_that_only_prefixes_a_longer_name(small):
    root, _git_ = small
    (root / "p.py.bak").write_text("x", encoding="utf-8")
    assert pha.met_by_paths_at_head_credit(root, _met(A="p.py.bak"), ["A"], ["p.py"]) == frozenset()


def _manifest(root):
    import json

    path = root / "manifest.json"
    path.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["p.py", "q.py"]}]}), encoding="utf-8")
    return path


def test_twin_gate_credits_the_hand_landed_rows_and_refuses_the_rest(small):
    root, git = small
    resume = {"run_id": "old", "halt_sha": git("rev-parse", "HEAD"), "blocking": ["R1"], "unlanded": ["C2"]}
    detail: dict = {}
    ok = {"items": _met(R1="p.py:2 and q.py")}
    assert tc._unchanged_since_halt_failures(root, resume, ok, _manifest(root), detail=detail) == []
    assert detail["landed_sha"] == git("log", "-1", "--format=%H", "--", "p.py", "q.py")

    refused = tc._unchanged_since_halt_failures(root, resume, {"items": _met(R1="other.py")}, _manifest(root), detail={})
    assert refused == REFUSED


def test_twin_ledger_gate_closes_already_landed_on_path_credit_and_marks_futile_refusals(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    class _Ledger:
        items = [1]
        all_of_pm_ask = False
        sizing_rel = None

    monkeypatch.setattr(rl, "read_ledger", lambda *a, **k: _Ledger())
    monkeypatch.setattr(rl, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    monkeypatch.setattr(rl, "read_notes", lambda *a, **k: [])
    monkeypatch.setattr(tc.pre_halt_credit, "with_notes", lambda r, n: r)
    sha = "a" * 40

    def credited(root, resume, verdict, manifest, plan_rel, detail):
        detail.update(landed_sha=sha, landed_paths=["p.py"], path_credit={"R1": sha})
        return []

    monkeypatch.setattr(tc, "_unchanged_since_halt_failures", credited)
    closed, _, _ = tc._ledger_gate(tmp_path, tmp_path, {"items": []}, resume_ledger={"blocking": []})
    assert closed["already_landed"] is True and closed["sha"] == sha and closed["paths"] == ["p.py"]

    monkeypatch.setattr(
        tc, "_unchanged_since_halt_failures", lambda *a: [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    )
    refused, _, _ = tc._ledger_gate(tmp_path, tmp_path, {"items": []}, resume_ledger={"blocking": []})
    assert refused["refused"] == "ledger-unmet" and refused["refire_futile"] is True
