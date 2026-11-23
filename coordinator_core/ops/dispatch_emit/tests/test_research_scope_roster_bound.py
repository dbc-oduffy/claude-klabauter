"""A notebooklm research emit states its notebooks roster to the scope stage as a hard constraint."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import (  # noqa: F401
    _run,
    _write_sizing,
    repo,
)
import pytest

_TWO = (
    "--research-class", "corpus", "--research-appetite", "medium", "--research-source", "notebooklm",
    "--research-notebooklm-need", "youtube", "--research-target", "notebooklm=A", "--research-target", "notebooklm=B",
)


def _emit(repo, capsys, *flags):
    rel = _write_sizing(repo, *flags)
    capsys.readouterr()
    code, reply = _run(capsys, ["--from-sizing", rel])
    assert reply and reply["ok"], reply
    script = Path(reply["path"]).read_text(encoding="utf-8")
    return rel, script, reply


def _brief_text(repo, script):
    paths = re.findall(r"[\w./-]*brief\.md", script)
    assert paths, "no members brief named in the emitted script"
    return (repo / paths[0]).read_text(encoding="utf-8")


def test_two_notebook_roster_is_stated_in_the_members_brief(repo, capsys: pytest.CaptureFixture[str]) -> None:
    _, script, _ = _emit(repo, capsys, *_TWO)
    brief = _brief_text(repo, script)
    assert "## Notebooks roster" in brief
    assert "2 entries: `A`, `B`" in brief
    assert "`worker_count` = 2" in brief and "`notebooks` listing exactly" in brief


def test_halt_guard_trips_on_a_one_scope_reply_and_passes_a_two_scope_reply(repo, capsys: pytest.CaptureFixture[str]) -> None:
    _, script, _ = _emit(repo, capsys, *_TWO)
    assert "disagrees with the 2-entry notebooks roster (" in script
    lines = script.splitlines()
    at = next(i for i, ln in enumerate(lines) if "disagrees with the" in ln)
    condition = next(ln for ln in reversed(lines[:at]) if "!== 2" in ln)
    assert "['worker_count']" in condition and "['notebooks']" in condition
    assert condition.count("!== 2") == 2, condition
    assert "!== 1" not in condition


def test_no_notebooklm_leaves_the_brief_unchanged(tmp_path: Path) -> None:
    (tmp_path / "scratch/run-1").mkdir(parents=True)
    ask = tmp_path / "scratch/run-1/ask.md"
    ask.write_text("ask\n", encoding="utf-8")
    assert re_.bind_notebooks_roster(tmp_path, "scratch/run-1", "scratch/run-1/ask.md", ()) == "scratch/run-1/ask.md"
    assert ask.read_text(encoding="utf-8") == "ask\n"


def test_roster_precedence_given_then_targets_then_question_defaults() -> None:
    targets = [{"source": "notebooklm", "ref": "A"}, {"source": "web", "ref": "x"}, {"source": "notebooklm", "ref": "B"}]
    assert re_.notebooks_roster(targets, ["Z"], ["q"]) == ("Z",)
    assert re_.notebooks_roster(targets, [], ["q"]) == ("A", "B")
    assert re_.notebooks_roster([], [], ["q1", "q2"]) == ("A", "B")


def test_a_sizing_brief_is_wrapped_never_edited(tmp_path: Path) -> None:
    sizing = tmp_path / "state/sizings/s.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("x: 1\n", encoding="utf-8")
    rel = re_.bind_notebooks_roster(tmp_path, "scratch/run-1", "state/sizings/s.yaml", ("A", "B"))
    assert rel == "scratch/run-1/brief.md"
    assert sizing.read_text(encoding="utf-8") == "x: 1\n"
    assert "`state/sizings/s.yaml`" in (tmp_path / rel).read_text(encoding="utf-8")


def test_uuid_notebook_targets_map_one_to_one_to_letters_and_bind_existing_ids(repo, capsys) -> None:
    uuids = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    flags = ["--research-class", "corpus", "--research-source", "notebooklm"]
    for u in uuids:
        flags += ["--research-target", f"notebooklm={u}"]
    _, script, reply = _emit(repo, capsys, *flags)
    nlm = next(s for s in reply["segments"] if s["pipeline"] == "notebooklm")
    assert nlm["lists"] == {"notebooks": ["A", "B", "C"]}
    brief = _brief_text(repo, script)
    assert "3 entries: `A`, `B`, `C`" in brief
    assert "### Existing notebook IDs" in brief
    assert f"Notebook `A` -> existing notebook ID `{uuids[0]}`" in brief
    assert f"Notebook `B` -> existing notebook ID `{uuids[1]}`" in brief
    assert f"Notebook `C` -> existing notebook ID `{uuids[2]}`" in brief
    assert "Source strategy: existing_notebook" in brief
    assert "do NOT call `notebook_get`" in brief
    assert "notebook_get" in brief and "notebook_query" in brief and "do NOT call `notebook_create`" in brief
    # All notebooks are bound existing IDs: scout must not halt on empty usable_sources.
    assert "stage scout returned no usable_sources" not in script


def test_mixed_fresh_and_existing_notebooks_keep_scout_usable_sources_halt(repo, capsys) -> None:
    flags = [
        "--research-class", "corpus", "--research-source", "notebooklm",
        "--research-target", "notebooklm=A",
        "--research-target", "notebooklm=22222222-2222-4222-8222-222222222222",
    ]
    _, script, reply = _emit(repo, capsys, *flags)
    nlm = next(s for s in reply["segments"] if s["pipeline"] == "notebooklm")
    assert nlm["lists"] == {"notebooks": ["A", "B"]}
    assert "stage scout returned no usable_sources" in script


def test_notebook_slot_map_preserves_explicit_letters_and_pairs_given_list_with_uuids() -> None:
    mixed = [{"source": "notebooklm", "ref": "A"}, {"source": "notebooklm", "ref": "uuid-2"}]
    assert re_.notebook_slot_map(mixed) == (("A", "B"), (("B", "uuid-2"),))
    uuids = [{"source": "notebooklm", "ref": "uuid-1"}, {"source": "notebooklm", "ref": "uuid-2"}]
    assert re_.notebook_slot_map(uuids, given=["X", "Y"]) == (("X", "Y"), (("X", "uuid-1"), ("Y", "uuid-2")))
