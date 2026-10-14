"""engine_owner_rows: each ownership signal, the skips, and the single resolver call."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import engine_owned_rows
from coordinator_core.ops.dispatch_emit.engine_owned_rows import engine_owner_rows

ROOT = "engine-root-stub"


@pytest.fixture
def calls(monkeypatch):
    seen: list[int] = []

    def fake():
        seen.append(1)
        return ROOT, "resolved-engine"

    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", fake)
    return seen


@pytest.fixture
def cwd(tmp_path):
    return tmp_path


def ids(rows):
    return [r["id"] for r in rows]


def test_owner_repo_signal_cleared_gate_still_engine(cwd, calls):
    row = {"id": "A", "external_gate": [{"owner_repo": "Project-Claude-Klabauter", "cleared": True}]}
    rows = engine_owner_rows([row], [], "", cwd)
    assert rows == [
        {"id": "A", "owner_repo": "claude-klabauter", "owner_root": ROOT,
         "root_class": "resolved-engine", "engine": True}
    ]


def test_reads_ungated_owner_repo_signal(cwd, calls):
    row = {"id": "A", "external_reads_ungated": [{"owner_repo": "claude-klabauter"}]}
    assert ids(engine_owner_rows([row], [], "", cwd)) == ["A"]


def test_other_owner_repo_is_not_engine(cwd, calls):
    row = {"id": "A", "external_gate": [{"owner_repo": "coordinator-content-repo"}]}
    assert engine_owner_rows([row], [], "", cwd) == []
    assert calls == []


@pytest.mark.parametrize("field", ["writes", "writes_under", "surface"])
def test_path_signal(cwd, calls, field):
    value = "coordinator_core/x.py" if field == "surface" else ["coordinator_core/x.py"]
    assert ids(engine_owner_rows([{"id": "A", field: value}], [], "", cwd)) == ["A"]


def test_evidence_sidecar_signal(cwd, calls):
    evidence = "rows:\n  A:\n  - text: delivered in coordinator_core/ops/x.py\n  B:\n  - text: pkg/y.py\n"
    rows = engine_owner_rows([{"id": "A"}, {"id": "B"}], [], evidence, cwd)
    assert ids(rows) == ["A"]


def test_unparseable_evidence_is_ignored(cwd, calls):
    assert engine_owner_rows([], [], "rows: [unclosed", cwd) == []


@pytest.mark.parametrize("token", ["coordinator_core", "claude-klabauter", "klabauter"])
def test_register_source_text_signal(cwd, calls, token):
    reg = [{"id": "R", "source_text": f"lives in {token.upper()}", "met_by": None}]
    assert ids(engine_owner_rows([], reg, "", cwd)) == ["R"]


def test_register_met_by_signal(cwd, calls):
    reg = [{"id": "R", "source_text": "x", "met_by": "coordinator_core@abc"}]
    assert ids(engine_owner_rows([], reg, "", cwd)) == ["R"]


def test_cwd_owned_row_excluded_and_resolver_not_called(cwd, calls):
    reg = [{"id": "T1", "source_text": "pkg/a.py :: f", "met_by": None}]
    assert engine_owner_rows([{"id": "T1", "writes": ["pkg/a.py"]}], reg, "", cwd) == []
    assert calls == []


def test_resolver_called_once_for_many_rows_and_ids_deduped(cwd, calls):
    plan = [{"id": "A", "writes": ["coordinator_core/a.py"]}, {"id": "B", "surface": "coordinator_core/b.py"}]
    reg = [{"id": "A", "source_text": "klabauter", "met_by": None}]
    assert ids(engine_owner_rows(plan, reg, "", cwd)) == ["A", "B"]
    assert len(calls) == 1


def test_claude_klabauter_cwd_skips_classification(cwd, calls):
    (cwd / "coordinator_core").mkdir()
    (cwd / "pyproject.toml").write_text("[project]\n", encoding="utf-8", newline="\n")
    assert engine_owner_rows([{"id": "A", "writes": ["coordinator_core/a.py"]}], [], "", cwd) == []
    assert calls == []


def test_coordinator_core_dir_without_pyproject_is_not_an_engine_cwd(cwd, calls):
    (cwd / "coordinator_core").mkdir()
    assert ids(engine_owner_rows([{"id": "A", "writes": ["coordinator_core/a.py"]}], [], "", cwd)) == ["A"]


def test_unresolved_root_renders_empty(cwd, monkeypatch):
    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", lambda: ("", "unresolved"))
    rows = engine_owner_rows([{"id": "A", "writes": ["coordinator_core/a.py"]}], [], "", cwd)
    assert rows[0]["owner_root"] == ""
    assert rows[0]["root_class"] == "unresolved"
