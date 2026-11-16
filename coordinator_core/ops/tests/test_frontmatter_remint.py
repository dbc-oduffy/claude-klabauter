"""frontmatter.remint handler, refusal-reason contract and lazy resolver registry."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from coordinator_core.ops import frontmatter_remint as remint
from coordinator_core.ops.frontmatter_remint import _contract as contract

BAD_ID = "hnd-atlas-g3m1a7"
GOOD_ID = "hnd-atlas-3a7c1e"

_HANDOFF = """---
title: t
created: 2026-01-01
branch: b
status: open
predecessor: none
handoff_id: {hid}
---

body
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


def _write(repo: Path, hid: str, name: str = "h.md") -> Path:
    p = repo / "state" / "handoffs" / name
    p.write_text(_HANDOFF.format(hid=hid), encoding="utf-8")
    return p


def _fake_family(monkeypatch, family: str, **attrs) -> None:
    mod = types.ModuleType(f"fake_remint_{abs(hash(family))}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.setitem(remint._RESOLVERS, family, mod.__name__)


def _run(repo: Path, **params) -> dict:
    return remint._handler(params, repo_root=repo)


def test_contract_reasons_and_result_types() -> None:
    assert contract.REASONS == {
        "resolver-unbuilt", "handoff-id-underivable", "handoff-id-referenced",
        "handoff-id-collision", "no-governing-plan", "plan-unauthorized",
        "review-run-absent", "register-no-stamp", "register-absent", "register-unsettled",
    }
    assert contract.Resolved({"a": 1}).value == {"a": 1}
    with pytest.raises(ValueError):
        contract.Refused("made-up-reason")


def test_unknown_family_refuses_resolver_unbuilt(repo) -> None:
    p = _write(repo, GOOD_ID)
    before = p.read_bytes()
    out = _run(repo, paths=["state/handoffs/h.md"], fields=["bogus"])
    (field,) = out["results"][0]["fields"]
    assert (field["field"], field["outcome"], field["reason"]) == ("bogus", "refused", "resolver-unbuilt")
    assert p.read_bytes() == before


def test_unimportable_resolver_refuses_resolver_unbuilt(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(repo, BAD_ID)
    before = p.read_bytes()
    monkeypatch.setitem(remint._RESOLVERS, "handoff_id", "coordinator_core.ops.frontmatter_remint.no_such_mod")
    out = _run(repo, paths=["state/handoffs/h.md"])
    (field,) = out["results"][0]["fields"]
    assert (field["field"], field["outcome"], field["reason"]) == ("handoff_id", "refused", "resolver-unbuilt")
    assert p.read_bytes() == before


def test_refusal_leaves_file_byte_identical(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(repo, BAD_ID)
    before = p.read_bytes()
    _fake_family(
        monkeypatch, "handoff_id",
        resolve=lambda path, fm, root: contract.Refused("handoff-id-referenced", "cited"),
    )
    out = _run(repo, paths=["state/handoffs/h.md"])
    (field,) = out["results"][0]["fields"]
    assert (field["outcome"], field["reason"]) == ("refused", "handoff-id-referenced")
    assert field["reason"] in contract.REASONS
    assert p.read_bytes() == before


def test_resolved_value_is_written(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(repo, BAD_ID)
    _fake_family(
        monkeypatch, "handoff_id",
        resolve=lambda path, fm, root: contract.Resolved({"handoff_id": GOOD_ID}),
    )
    out = _run(repo, paths=["state/handoffs/h.md"])
    (field,) = out["results"][0]["fields"]
    assert (field["outcome"], field["reason"]) == ("written", None)
    assert f"handoff_id: {GOOD_ID}" in p.read_text(encoding="utf-8")
    again = _run(repo, paths=["state/handoffs/h.md"])
    assert again["results"][0]["fields"] == []


def test_already_valid_field_is_skipped(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(repo, GOOD_ID)
    before = p.read_bytes()
    called = []
    _fake_family(
        monkeypatch, "handoff_id",
        resolve=lambda path, fm, root: called.append(1) or contract.Resolved({"handoff_id": "x"}),
    )
    out = _run(repo, paths=["state/handoffs/h.md"], fields=["handoff_id"])
    (field,) = out["results"][0]["fields"]
    assert (field["field"], field["outcome"]) == ("handoff_id", "already-valid")
    assert called == [] and p.read_bytes() == before


def test_writer_owning_family_is_called_after_resolve(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo, BAD_ID)
    calls = []
    _fake_family(
        monkeypatch, "handoff_id",
        resolve=lambda path, fm, root: calls.append("resolve") or contract.Resolved(),
        write=lambda path, root: calls.append("write") or contract.Refused("review-run-absent", "none"),
    )
    out = _run(repo, paths=["state/handoffs/h.md"])
    (field,) = out["results"][0]["fields"]
    assert calls == ["resolve", "write"]
    assert (field["outcome"], field["reason"]) == ("refused", "review-run-absent")


def test_cited_handoff_remints_with_alias_and_old_id_resolves(repo) -> None:
    from coordinator_core.dag import build_handoff_id_index

    p = _write(repo, BAD_ID)
    (repo / "state" / "ledger.json").write_text(f'"{BAD_ID}"', encoding="utf-8")
    out = _run(repo, paths=["state/handoffs/h.md"])
    (field,) = out["results"][0]["fields"]
    assert (field["field"], field["outcome"], field["reason"]) == ("handoff_id", "written", None)
    text = p.read_text(encoding="utf-8")
    assert f"handoff_id_aliases:\n  - {BAD_ID}\n" in text
    assert "handoff_id: hnd-" in text and f"handoff_id: {BAD_ID}" not in text
    assert build_handoff_id_index([str(p)])[BAD_ID] == str(p)
    assert _run(repo, paths=["state/handoffs/h.md"])["results"][0]["fields"] == []


def test_usage_and_path_errors(repo) -> None:
    assert _run(repo)["exit_code"] == 2
    out = _run(repo, paths=["../escape.md", "state/handoffs/missing.md", "README.md"])
    assert out["exit_code"] == 0
    assert all(r["error"] for r in out["results"])
