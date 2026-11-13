"""Tests for coordinator_core.symbol_knowledge.workstate_index (fixture repos under tmp_path)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.symbol_knowledge import workstate_index as ws

SCIP = "scip-python python fixture 0.1 `pkg.mod`/fn()."


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/mod.py", "LIMIT = 3\n\n\ndef fn():\n    return 1\n\n\nclass Box:\n    def put(self):\n        pass\n")
    _write(
        root,
        "docs/decisions/DR-1-fn.md",
        "---\nid: DR-1\nstatus: accepted\n---\n# Decision\n\n## Why fn\n\nWe keep `pkg.mod.fn` and `pkg.mod.Box.put` and pkg/mod.py:4-5 as is.\n"
        "Also `docs/plans/x.md` and pkg/missing.py are cited, plus `unrelated.thing`.\n",
    )
    _write(root, "docs/plans/2026-11/p.md", "# Plan\n\nTouches pkg/mod.py and `pkg.mod.LIMIT`.\n")
    _write(root, "state/lessons/l1.yaml", "id: l1\nbody: |\n  The helper `Box.put` in pkg/mod.py misled us.\n")
    _write(root, "archive/daily-summaries/2026-11-13.md", "# Day\n\nEdited pkg/mod.py today.\n")
    _write(root, "README.md", "pkg/mod.py is not a workstate doc\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _anchors(root: Path) -> "list[dict]":
    text = (root / ws.ANCHORS_REL).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _index(root: Path) -> "dict[str, list]":
    return json.loads((root / ws.INDEX_REL).read_text(encoding="utf-8"))["refs"]


def _resolver(calls: "list"):
    def resolve(path: str, qual: str):
        calls.append((path, qual))
        return SCIP if qual == "fn" else None

    return resolve


def test_index_maps_files_and_symbols_to_plans_decisions_lessons_and_logs(repo: Path) -> None:
    out = ws.workstate_index(repo, resolver=_resolver([]))
    refs = _index(repo)
    kinds = {d[1] for d in refs["pkg/mod.py"]}
    assert kinds == {"plan", "dr", "lesson", "log"}
    assert ["docs/decisions/DR-1-fn.md", "dr"] in refs["pkg/mod.py::fn"]
    assert ["state/lessons/l1.yaml", "lesson"] in refs["pkg/mod.py::Box.put"]
    assert ["docs/decisions/DR-1-fn.md", "dr"] in refs["pkg/mod.py::Box.put"]
    assert ["docs/plans/2026-11/p.md", "plan"] in refs["pkg/mod.py::LIMIT"]
    assert out["docs"] == 4
    assert not any("README" in d[0] for ds in refs.values() for d in ds)
    assert "pkg/missing.py" not in refs and "unrelated.thing" not in refs
    assert not any(k.startswith("docs/") for k in refs)


def test_anchor_lines_carry_the_edge_batch_shape(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]), now="2026-11-13T00:00:00Z")
    lines = _anchors(repo)
    fn_dr = next(
        a for a in lines if a["record"]["record_path"].startswith("docs/decisions/") and a["anchor"]["symbol_key"] == SCIP
    )
    assert fn_dr["kind"] == "workstate_anchor" and fn_dr["v"] == 1 and fn_dr["repo"] == "fixture"
    assert fn_dr["anchor"] == {"file_path": "pkg/mod.py", "symbol_key": SCIP, "line_start": 4, "line_end": None}
    assert fn_dr["record"] == {
        "record_kind": "decision",
        "record_path": "docs/decisions/DR-1-fn.md",
        "record_id": "DR-1",
        "section_anchor": "why-fn",
    }
    assert fn_dr["relation"] == "decides" and fn_dr["confidence"] == "explicit"
    assert fn_dr["producer"] == ws.PRODUCER and fn_dr["observed_at"] == "2026-11-13T00:00:00Z"
    assert len(fn_dr["commit_sha"]) == 40
    span = next(a for a in lines if a["anchor"]["line_start"] == 4 and a["anchor"]["line_end"] == 5)
    assert span["anchor"]["symbol_key"] is None and span["anchor"]["file_path"] == "pkg/mod.py"
    assert all("\\" not in json.dumps(a["anchor"]) for a in lines)
    assert not any(a["record"]["record_path"].startswith("archive/") for a in lines)


def test_bare_name_citation_is_inferred_and_only_from_a_cited_file(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    lesson = [a for a in _anchors(repo) if a["record"]["record_kind"] == "lesson"]
    inferred = [a for a in lesson if a["confidence"] == "inferred"]
    assert [(a["anchor"]["file_path"], a["relation"], a["anchor"]["line_start"]) for a in inferred] == [
        ("pkg/mod.py", "mentions", 9)
    ]
    assert not any(a["relation"] == "cites" and a["confidence"] == "inferred" for a in lesson)


def test_symbol_key_is_null_when_the_resolver_has_no_answer_or_is_absent(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    by_qual_line = {a["anchor"]["line_start"]: a["anchor"]["symbol_key"] for a in _anchors(repo)}
    assert by_qual_line[9] is None and by_qual_line[4] == SCIP

    def down(path: str, qual: str):
        raise RuntimeError("example-retrieval-repo down")

    out = ws.workstate_index(repo, resolver=down)
    assert out["scip_resolved"] == 0 or out["scip_calls"] == 0
    (repo / ws.CACHE_REL).unlink()
    out = ws.workstate_index(repo, resolver=down)
    assert out["scip_resolved"] == 0
    assert all(a["anchor"]["symbol_key"] is None for a in _anchors(repo))


def test_warm_rerun_rereads_nothing_resolves_nothing_and_rewrites_nothing(repo: Path) -> None:
    calls: list = []
    first = ws.workstate_index(repo, resolver=_resolver(calls))
    assert first["docs_reread"] == 4 and first["wrote"] is True and calls
    before = {p: (repo / p).read_bytes() for p in (ws.INDEX_REL, ws.ANCHORS_REL)}
    calls.clear()
    second = ws.workstate_index(repo, resolver=_resolver(calls))
    assert second["docs_reread"] == 0 and calls == [] and second["wrote"] is False
    assert {p: (repo / p).read_bytes() for p in before} == before


def test_a_changed_doc_is_the_only_one_reread(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    _write(repo, "docs/plans/2026-11/p.md", "# Plan\n\nNow touches pkg/mod.py:9 only.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit plan")
    out = ws.workstate_index(repo, resolver=_resolver([]))
    assert out["docs_reread"] == 1
    assert "pkg/mod.py::LIMIT" not in _index(repo)


def test_a_deleted_doc_drops_out_of_the_index(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    (repo / "state/lessons/l1.yaml").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop lesson")
    ws.workstate_index(repo, resolver=_resolver([]))
    assert not any(d[1] == "lesson" for ds in _index(repo).values() for d in ds)


def test_outputs_are_sorted(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    refs = json.loads((repo / ws.INDEX_REL).read_text(encoding="utf-8"))["refs"]
    assert list(refs) == sorted(refs)
    assert all(v == sorted(v) for v in refs.values())
    lines = _anchors(repo)
    order = [(a["anchor"]["file_path"], a["anchor"]["line_start"] or 0, a["record"]["record_path"]) for a in lines]
    assert order == sorted(order)


def test_decision_anchored_to_a_symbol_whose_body_moved_is_flagged(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    lines = _anchors(repo)
    flagged = ws.stale_decision_anchors(lines, {SCIP})
    assert {a["record"]["record_path"] for a in flagged} == {"docs/decisions/DR-1-fn.md"}
    assert all(a["relation"] == "decides" and a["anchor"]["symbol_key"] == SCIP for a in flagged)


def test_an_unmoved_symbol_or_a_non_decision_record_is_not_flagged(repo: Path) -> None:
    ws.workstate_index(repo, resolver=_resolver([]))
    lines = _anchors(repo)
    assert ws.stale_decision_anchors(lines, {"other"}) == []
    assert ws.stale_decision_anchors(lines, set()) == []
    lesson_only = [a for a in lines if a["record"]["record_kind"] == "lesson"]
    assert ws.stale_decision_anchors(lesson_only, {SCIP}) == []


def test_extract_citations_ignores_fenced_blocks_for_symbols_and_tracks_sections() -> None:
    text = "# A\n\n## Sec One\n\nsee `a.b_c()` and x/y.py:3\n\n```\n`ignored.name` z/w.py\n```\n"
    refs = ws.extract_citations(text)
    assert ["s", "a.b_c", "sec-one"] in refs
    assert ["p", "x/y.py", "sec-one", 3, None] in refs
    assert not any(r[1] == "ignored.name" for r in refs)
