"""handoff_id resolver for frontmatter.remint: derive, refuse referenced / colliding / underivable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import resolve_repo_name
from coordinator_core.ops.emit.sections.handoffs import _derive_handoff_id
from coordinator_core.ops.frontmatter_remint import handoff_id
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved


def _write(root: Path, rel: str, handoff_id_value: str | None, extra: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    hid = f"handoff_id: {handoff_id_value}\n" if handoff_id_value is not None else ""
    p.write_text(f"---\ntitle: x\n{hid}{extra}---\nbody\n", encoding="utf-8")
    return p


def _expected(root: Path, name: str) -> str:
    return _derive_handoff_id(resolve_repo_name(root), name)[0]


def test_resolves_wire_derived_id(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/2026-10-14-gap-fill-host-asks.md", "hnd-gap-fill-host-asks")
    res = handoff_id.resolve(p, {"handoff_id": "hnd-gap-fill-host-asks"}, tmp_path)
    assert res == Resolved({"handoff_id": _expected(tmp_path, p.name)})


def test_absent_id_resolves(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    assert isinstance(handoff_id.resolve(p, {}, tmp_path), Resolved)


@pytest.mark.parametrize(
    "key", ["predecessor_id", "origin_handoff_id", "predecessor", "forked_from"]
)
def test_refuses_when_old_id_referenced(tmp_path: Path, key: str) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    _write(tmp_path, "state/handoffs/other.md", "hnd-other-abcdef", f"{key}: hnd-a-thing\n")
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"


def test_refuses_when_referenced_from_archive_list_field(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    _write(
        tmp_path,
        "archive/handoffs/2026-10/o.md",
        "hnd-o-abcdef",
        "additional_predecessors:\n  - hnd-a-thing\n",
    )
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"


@pytest.mark.parametrize(
    "rel, body",
    [
        ("state/artifact-conformance/ledger.json", '{"handoff": "hnd-a-thing"}\n'),
        ("state/audits/triage/ledger-p1.yaml", "rows:\n  - id: hnd-a-thing\n"),
    ],
)
def test_refuses_and_names_state_ledger_citer(tmp_path: Path, rel: str, body: str) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    c = tmp_path / rel
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text(body, encoding="utf-8")
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"
    assert rel in res.detail


def test_self_is_not_a_citer(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    assert isinstance(handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path), Resolved)


def test_citer_scan_is_one_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    (tmp_path / "state" / "x.json").write_text('"hnd-a-thing"', encoding="utf-8")
    calls: list[list[str]] = []
    real = handoff_id.subprocess.run

    def spy(cmd, *a, **k):
        calls.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(handoff_id.subprocess, "run", spy)
    handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert len([c for c in calls if "grep" in c]) == 1


def test_run_artifacts_under_state_warp_are_not_citers(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    w = tmp_path / "state" / "warp" / "run-1" / "manifest.json"
    w.parent.mkdir(parents=True)
    w.write_text('"hnd-a-thing"', encoding="utf-8")
    assert isinstance(handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path), Resolved)


def test_run_artifacts_under_state_plan_blitz_are_not_citers(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    w = tmp_path / "state" / "plan-blitz" / "r" / "x.json"
    w.parent.mkdir(parents=True)
    w.write_text('"hnd-a-thing"', encoding="utf-8")
    assert isinstance(handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path), Resolved)


def test_git_error_refuses_rather_than_reporting_no_citers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")

    class _Proc:
        returncode = 128
        stdout = b""

    real = handoff_id.subprocess.run
    monkeypatch.setattr(
        handoff_id.subprocess,
        "run",
        lambda cmd, *a, **k: _Proc() if "grep" in cmd else real(cmd, *a, **k),
    )
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"


@pytest.mark.parametrize("exc", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 2)])
def test_spawn_failure_refuses_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")

    real = handoff_id.subprocess.run

    def boom(cmd, *a, **k):
        if "grep" in cmd:
            raise exc
        return real(cmd, *a, **k)

    monkeypatch.setattr(handoff_id.subprocess, "run", boom)
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"


def test_unreferenced_old_id_is_not_refused(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    _write(tmp_path, "state/handoffs/other.md", "hnd-other-abcdef", "predecessor_id: hnd-zzz-123456\n")
    assert isinstance(handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path), Resolved)


def test_refuses_collision_with_other_holder(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    held = _expected(tmp_path, p.name)
    _write(tmp_path, "archive/handoffs/2026-10/zzz.md", held)
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-collision"


def test_already_holding_derived_id_is_not_a_collision(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    mine = _expected(tmp_path, p.name)
    p.write_text(f"---\ntitle: x\nhandoff_id: {mine}\n---\nb\n", encoding="utf-8")
    assert handoff_id.resolve(p, {"handoff_id": mine}, tmp_path) == Resolved({"handoff_id": mine})


def test_refuses_underivable_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    monkeypatch.setattr(
        handoff_id, "_derive_handoff_id", lambda repo, path: ("hnd-placeholder-replace-with-x-abcdef", "derived")
    )
    res = handoff_id.resolve(p, {}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-underivable"


def test_refuses_underivable_when_slug_is_not_alnum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    monkeypatch.setattr(handoff_id, "_derive_handoff_id", lambda repo, path: ("hnd-Bad_Slug-abcdef", "derived"))
    res = handoff_id.resolve(p, {}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-underivable"


def test_repo_label_is_the_emitting_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    seen: list[str] = []

    def spy(repo: str, path: str):
        seen.append(repo)
        return _derive_handoff_id(repo, path)

    monkeypatch.setattr(handoff_id, "_derive_handoff_id", spy)
    handoff_id.resolve(p, {}, tmp_path)
    assert seen == [resolve_repo_name(tmp_path)]


def test_a_many_path_call_scans_citers_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One citer scan per call, never per path: 14 per-path scans were ~2-3s of process time."""
    from coordinator_core.ops import frontmatter_remint

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    rels = [f"state/handoffs/2026-10-1{i}-thing-{i}.md" for i in range(4)]
    for i, rel in enumerate(rels):
        _write(tmp_path, rel, f"hnd-thing-{i}")
    (tmp_path / "state" / "ledger.json").write_text('{"x": "hnd-thing-2"}\n', encoding="utf-8")
    real = subprocess.run
    calls: list[list[str]] = []

    def counting(argv, *a, **k):
        if argv[:1] == ["git"] and "grep" in argv:
            calls.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(handoff_id.subprocess, "run", counting)
    out = frontmatter_remint._handler({"paths": rels, "fields": ["handoff_id"]}, repo_root=tmp_path)
    assert len(calls) == 1
    outcomes = {r["path"]: r["fields"][0] for r in out["results"]}
    assert outcomes[rels[2]]["reason"] == "handoff-id-referenced"
    assert "state/ledger.json" in outcomes[rels[2]]["detail"]
