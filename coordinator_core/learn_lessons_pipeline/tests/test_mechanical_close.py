"""Tests for learn_lessons_pipeline.mechanical_close: contract plus the deterministic rules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from coordinator_core import ipc
from coordinator_core.distill.prompt_tier import estimate_tokens
from coordinator_core.learn_lessons_pipeline import mechanical_close as mc
from coordinator_core.session import record_homes

_PAGE = "docs" + "/wiki/page.md"
_NONE = "docs" + "/wiki/none.md"
BODY = "A published fix does not reach a resident warm server until that server churns."


def _home(tmp_path) -> Path:
    home = Path(record_homes.home_dir(str(tmp_path), "lessons"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def _row(home: Path, name: str, *, title="T", body=BODY, scope="project", created="2026-10-01", extra="") -> Path:
    p = home / f"{name}.yaml"
    p.write_text(
        f'created: "{created}"\ntitle: "{title}"\nbody: |\n  {body}\nstatus: open\nscope: {scope}\n'
        f'from_repo: r\n{extra}',
        encoding="utf-8",
    )
    return p


def _archived(tmp_path, name="old", month="2026-09", **kw) -> Path:
    adir = tmp_path / "archive" / "lessons" / month
    adir.mkdir(parents=True, exist_ok=True)
    return _row(adir, name, **kw)


def _by_id(verdicts):
    return {v.row_id: v for v in verdicts}


def test_verdict_shape():
    v = mc.MechanicalVerdict("r1", "retag", "", "scope local -> project", "state/lessons/r1.yaml", "project")
    assert v.new_scope == "project" and v.row_path == "state/lessons/r1.yaml"
    assert mc.MechanicalVerdict("r2", "discard", "x", "y", "p").new_scope is None


def test_scan_queue_one_pass(tmp_path):
    home = _home(tmp_path)
    (home / "b.yaml").write_text("bb" * 10, encoding="utf-8")
    (home / "a.yaml").write_text("a", encoding="utf-8")
    (home / "note.txt").write_text("ignored", encoding="utf-8")
    rows = mc.scan_queue(tmp_path)
    assert [r["row_id"] for r in rows] == ["a", "b"]
    assert rows[0]["row_path"] == "state/lessons/a.yaml"
    assert [r["size_bytes"] for r in rows] == [1, 20]


def test_scan_queue_missing_home(tmp_path):
    assert mc.scan_queue(tmp_path) == []


def test_empty_queue_has_no_candidates(tmp_path):
    assert mc.close_candidates(object(), tmp_path) == []


def test_apply_empty_reports_equal_tokens(tmp_path):
    home = _home(tmp_path)
    (home / "a.yaml").write_text("x" * 400, encoding="utf-8")
    report = mc.apply_verdicts([], tmp_path)
    assert report["tokens_before"] == report["tokens_after"] == estimate_tokens(400)
    assert report["closed"] == {"dedupe": 0, "discard": 0, "wiki-append": 0, "retag": 0}
    assert report["archived"] == [] and report["rewritten"] == []


def test_duplicate_of_open_row_names_oldest_survivor(tmp_path):
    home = _home(tmp_path)
    _row(home, "b-newer", created="2026-10-05", title="Warm Server")
    _row(home, "a-oldest", created="2026-10-01", title="warm   server")
    _row(home, "c-newest", created="2026-10-09", title="WARM SERVER")
    v = _by_id(mc.close_candidates(object(), tmp_path))
    assert "a-oldest" not in v
    assert v["b-newer"].verdict == v["c-newest"].verdict == "dedupe"
    assert v["b-newer"].covered_by == "state/lessons/a-oldest.yaml"
    assert "a-oldest" in v["b-newer"].reason


def test_duplicate_of_archived_lesson_closes_every_open_copy(tmp_path):
    home = _home(tmp_path)
    _archived(tmp_path, "old")
    _row(home, "x")
    v = _by_id(mc.close_candidates(object(), tmp_path))
    assert v["x"].verdict == "dedupe"
    assert v["x"].covered_by == "archive/lessons/2026-09/old.yaml"


def test_empty_body_and_memory_stub_discard(tmp_path):
    home = _home(tmp_path)
    (home / "empty.yaml").write_text('created: "2026-10-01"\ntitle: t\nbody: ""\nstatus: open\nscope: project\n', encoding="utf-8")
    (home / "short.yaml").write_text('created: "2026-10-01"\ntitle: t\nbody: "no frontmatter"\nstatus: open\nscope: project\n', encoding="utf-8")
    _row(
        home, "mem", title="MEMORY", body="- [Publish anytime](publish-anytime.md) -- PM standing OK",
        extra="tags:\n  - rerouted-from-memory\n",
    )
    v = _by_id(mc.close_candidates(object(), tmp_path))
    assert {k: x.verdict for k, x in v.items()} == {"empty": "discard", "short": "discard", "mem": "discard"}
    assert "empty body" in v["empty"].reason and "memory-ingest stub" in v["mem"].reason


def test_empty_body_with_other_content_is_kept(tmp_path):
    home = _home(tmp_path)
    (home / "lesson-form.yaml").write_text(
        'created: "2026-10-01"\ntitle: t\nstatus: open\nscope: project\nlesson: >-\n  A real lesson stored outside body.\n',
        encoding="utf-8",
    )
    assert mc.close_candidates(object(), tmp_path) == []


def test_retag_table_and_unmapped_scope(tmp_path):
    home = _home(tmp_path)
    for i, scope in enumerate(['local', '"local"', "'claude-klabauter'", "fleet", "bespoke", "universal", "project"]):
        _row(home, f"r{i}", scope=scope, body=f"{BODY} variant {i}", created=f"2026-10-0{i + 1}")
    v = _by_id(mc.close_candidates(object(), tmp_path))
    assert {k: (x.verdict, x.new_scope) for k, x in v.items()} == {
        "r0": ("retag", "project"),
        "r1": ("retag", "project"),
        "r2": ("retag", "project"),
        "r3": ("retag", "universal"),
    }


def test_unique_substantive_row_yields_no_verdict(tmp_path):
    home = _home(tmp_path)
    _row(home, "only")
    _row(home, "other", body="A different substantive lesson about guards and hooks.")
    assert mc.close_candidates(object(), tmp_path) == []


def test_closed_status_rows_are_not_candidates(tmp_path):
    home = _home(tmp_path)
    _row(home, "a")
    p = _row(home, "b")
    p.write_text(p.read_text(encoding="utf-8").replace("status: open", "status: closed"), encoding="utf-8")
    assert mc.close_candidates(object(), tmp_path) == []


def test_manifest_restricts_reported_rows(tmp_path):
    home = _home(tmp_path)
    _row(home, "a", created="2026-10-01")
    _row(home, "b", created="2026-10-02")
    manifest = SimpleNamespace(entries=[SimpleNamespace(path=str(home / "a.yaml"))])
    assert mc.close_candidates(manifest, tmp_path) == []
    manifest = SimpleNamespace(entries=[SimpleNamespace(path="state/lessons/b.yaml")])
    assert [v.row_id for v in mc.close_candidates(manifest, tmp_path)] == ["b"]


def test_apply_archives_stamps_retags_and_second_run_is_noop(tmp_path):
    home = _home(tmp_path)
    _row(home, "a-keep", created="2026-10-01")
    _row(home, "b-dup", created="2026-10-02")
    (home / "c-empty.yaml").write_text('created: "2026-10-01"\ntitle: t\nbody: ""\nstatus: open\nscope: project\n', encoding="utf-8")
    _row(home, "d-local", scope='"local"', body="Another distinct lesson about retagging scope values.")

    verdicts = mc.close_candidates(object(), tmp_path)
    report = mc.apply_verdicts(verdicts, tmp_path)

    assert report["closed"] == {"dedupe": 1, "discard": 1, "wiki-append": 0, "retag": 1}
    assert report["rewritten"] == ["state/lessons/d-local.yaml"]
    assert sorted(report["archived_from"]) == ["state/lessons/b-dup.yaml", "state/lessons/c-empty.yaml"]
    assert report["tokens_after"] < report["tokens_before"]
    assert sorted(p.name for p in home.iterdir()) == ["a-keep.yaml", "d-local.yaml"]

    month_dirs = list((tmp_path / "archive" / "lessons").iterdir())
    assert len(month_dirs) == 1
    dup = yaml.safe_load((month_dirs[0] / "b-dup.yaml").read_text(encoding="utf-8"))
    assert dup["disposition"] == "dedupe"
    assert dup["covered_by"] == "state/lessons/a-keep.yaml"
    assert "a-keep" in dup["closed_reason"] and dup["title"] == "T" and dup["body"].strip() == BODY
    discarded = yaml.safe_load((month_dirs[0] / "c-empty.yaml").read_text(encoding="utf-8"))
    assert discarded["disposition"] == "discard" and discarded["covered_by"] == ""

    text = (home / "d-local.yaml").read_text(encoding="utf-8")
    assert 'scope: "project"' in text and text.count("\n") == 7
    assert yaml.safe_load(text)["scope"] == "project"

    assert mc.close_candidates(object(), tmp_path) == []
    again = mc.apply_verdicts(verdicts, tmp_path)
    assert again["closed"] == {"dedupe": 0, "discard": 0, "wiki-append": 0, "retag": 0}
    assert again["archived"] == [] and again["rewritten"] == []
    assert again["tokens_before"] == again["tokens_after"]


def test_apply_replaces_existing_paper_trail_keys(tmp_path):
    home = _home(tmp_path)
    _archived(tmp_path, "old")
    _row(home, "x", extra='covered_by: "stale"\n')
    mc.apply_verdicts(mc.close_candidates(object(), tmp_path), tmp_path)
    (moved,) = (tmp_path / "archive" / "lessons").glob("*/x.yaml")
    doc = yaml.safe_load(moved.read_text(encoding="utf-8"))
    assert doc["covered_by"] == "archive/lessons/2026-09/old.yaml"
    assert moved.read_text(encoding="utf-8").count("covered_by:") == 1


def test_apply_archive_name_collision_never_overwrites(tmp_path):
    home = _home(tmp_path)
    _row(home, "a", created="2026-10-01")
    _row(home, "b", created="2026-10-02")
    month = mc.datetime.now(mc.timezone.utc).strftime("%Y-%m")
    taken = tmp_path / "archive" / "lessons" / month / "b.yaml"
    taken.parent.mkdir(parents=True)
    taken.write_text("keep: me\n", encoding="utf-8")
    report = mc.apply_verdicts(mc.close_candidates(object(), tmp_path), tmp_path)
    assert taken.read_text(encoding="utf-8") == "keep: me\n"
    assert report["archived"] == [f"archive/lessons/{month}/b-2.yaml"]


def test_apply_refuses_a_path_outside_the_lessons_home(tmp_path):
    _home(tmp_path)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text("title: x\n", encoding="utf-8")
    v = mc.MechanicalVerdict("elsewhere", "discard", "", "r", "elsewhere.yaml")
    report = mc.apply_verdicts([v], tmp_path)
    assert report["skipped"] == ["elsewhere.yaml"] and outside.exists()


def test_apply_dedupe_with_missing_survivor_is_skipped(tmp_path):
    home = _home(tmp_path)
    _row(home, "x")
    v = mc.MechanicalVerdict("x", "dedupe", "state/lessons/gone.yaml", "r", "state/lessons/x.yaml")
    report = mc.apply_verdicts([v], tmp_path)
    assert report["skipped"] == ["state/lessons/x.yaml"] and (home / "x.yaml").exists()


def test_apply_preserves_crlf(tmp_path):
    home = _home(tmp_path)
    p = home / "crlf.yaml"
    p.write_bytes(b'created: "2026-10-01"\r\ntitle: t\r\nbody: "' + BODY.encode() + b'"\r\nstatus: open\r\nscope: local # note\r\n')
    mc.apply_verdicts(mc.close_candidates(object(), tmp_path), tmp_path)
    assert p.read_bytes().endswith(b"scope: project # note\r\n")
    assert b"\n" not in p.read_bytes().replace(b"\r\n", b"")


def _wiki(tmp_path, indexed=True) -> Path:
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / "page.md"
    page.write_text("# Page\n\nExisting text.\n", encoding="utf-8")
    (wiki / "_index.md").write_text("- [page](page.md)\n" if indexed else "- nothing\n", encoding="utf-8")
    return page


def test_wiki_append_needs_a_named_existing_index_reachable_page(tmp_path):
    home = _home(tmp_path)
    _wiki(tmp_path)
    _row(home, "named", body="Named wiki body alpha.", extra=f"target_wiki: {_PAGE}\n")
    _row(home, "missing", body="Missing wiki body beta.", extra=f"target_wiki: {_NONE}\n")
    _row(home, "outside", body="Outside wiki body gamma.", extra="target_wiki: ../outside.md\n")
    _row(home, "bare", body="Bare wiki body delta.", extra="target_wiki: docs/wiki/\n")
    verdicts, open_rows = mc.classify_with_residue(tmp_path)
    assert [(v.row_id, v.verdict, v.covered_by) for v in verdicts] == [("named", "wiki-append", _PAGE)]
    assert len(open_rows) == 4


def test_wiki_append_target_not_linked_by_an_index_stays_residue(tmp_path):
    home = _home(tmp_path)
    _wiki(tmp_path, indexed=False)
    _row(home, "named", body="Named wiki body alpha.", extra=f"target_wiki: {_PAGE}\n")
    assert mc.close_candidates(None, tmp_path) == []


def test_apply_wiki_append_appends_once_and_archives(tmp_path):
    home = _home(tmp_path)
    page = _wiki(tmp_path)
    _row(home, "named", title="Heading", body="Named wiki body alpha.", extra=f"target_wiki: {_PAGE}\n")
    verdicts = mc.close_candidates(None, tmp_path)
    report = mc.apply_verdicts(verdicts, tmp_path)
    assert report["closed"]["wiki-append"] == 1 and report["appended"] == [_PAGE]
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# Page\n\nExisting text.\n") and "## Heading\n\nNamed wiki body alpha.\n" in text
    assert not (home / "named.yaml").exists() and report["archived"]
    again = mc.apply_verdicts(verdicts, tmp_path)
    assert again["skipped"] and page.read_text(encoding="utf-8") == text


def test_apply_wiki_append_to_a_non_wiki_path_is_skipped(tmp_path):
    home = _home(tmp_path)
    _row(home, "named", body="Named wiki body alpha.")
    other = home / "other.yaml"
    other.write_text("body: x\n", encoding="utf-8")
    v = mc.MechanicalVerdict("named", "wiki-append", "state/lessons/other.yaml", "r", "state/lessons/named.yaml")
    report = mc.apply_verdicts([v], tmp_path)
    assert report["skipped"] == ["state/lessons/named.yaml"] and (home / "named.yaml").exists()
    assert other.read_text(encoding="utf-8") == "body: x\n"


def test_discard_when_named_wiki_already_holds_the_body_else_wiki_append(tmp_path):
    home = _home(tmp_path)
    page = _wiki(tmp_path)
    page.write_text("# Page\n\nExisting   TEXT already recorded in this page.\n", encoding="utf-8")
    _row(home, "covered", body="existing text already recorded in this page.",extra=f"target_wiki: {_PAGE}\n")
    _row(home, "fresh", body="Brand new wiki body epsilon.", extra=f"target_wiki: {_PAGE}\n")
    got = {v.row_id: (v.verdict, v.covered_by) for v in mc.close_candidates(None, tmp_path)}
    assert got == {"covered": ("discard", _PAGE), "fresh": ("wiki-append", _PAGE)}


def test_universal_scope_retags_only_with_a_domain_tag(tmp_path):
    home = _home(tmp_path)
    _row(home, "ue", body="Unreal specific lesson body one.", scope="universal", extra="tags: [ue, other]\n")
    _row(home, "plain", body="Generic lesson body two here.", scope="universal", extra="tags: [process]\n")
    _row(home, "untagged", body="Generic lesson body three.", scope="universal")
    got = {v.row_id: v.new_scope for v in mc.close_candidates(None, tmp_path)}
    assert got == {"ue": "project"}


def test_dedupe_ignores_title(tmp_path):
    home = _home(tmp_path)
    _row(home, "a", title="One", created="2026-10-01")
    _row(home, "b", title="Two", created="2026-10-02")
    assert [(v.row_id, v.covered_by) for v in mc.close_candidates(None, tmp_path)] == [("b", "state/lessons/a.yaml")]


def _call(name, params, root):
    import coordinator_core.learn_lessons_pipeline.ops  # noqa: F401  registers both names

    return asyncio.run(ipc._REGISTRY[name](params, root))


def _mixed_queue(tmp_path):
    home = _home(tmp_path)
    _wiki(tmp_path)
    _row(home, "a-keep", title="Keeper", body="A unique judgment lesson about nothing else here.", created="2026-10-01")
    _row(home, "b-dup", title="Keeper", body="A unique judgment lesson about nothing else here.", created="2026-10-02")
    _row(home, "c-empty", body="")
    _row(home, "d-local", body="Local scope lesson with its own distinct body text.", scope="local")
    _row(home, "e-wiki", body="Wiki lesson with its own distinct body text.", extra=f"target_wiki: {_PAGE}\n")


def test_both_op_names_dry_run_return_the_same_report_with_residue_ids(tmp_path):
    _mixed_queue(tmp_path)
    reports = [
        _call(n, {"repo": str(tmp_path), "dry_run": True}, None)["report"]
        for n in ("learn_lessons_pipeline.apply_mechanical", "learn_lessons_pipeline.mechanical_close")
    ]
    assert reports[0] == reports[1]
    assert reports[0]["residue_ids"] == ["a-keep"]
    assert reports[0]["closed"] == {"dedupe": 1, "discard": 1, "wiki-append": 1, "retag": 1}


def test_op_write_reports_residue_ids_and_defaults_to_repo_root(tmp_path):
    _mixed_queue(tmp_path)
    report = _call("learn_lessons_pipeline.apply_mechanical", {}, tmp_path)["report"]
    assert report["residue_ids"] == ["a-keep"] and report["appended"] == [_PAGE]
    assert _call("learn_lessons_pipeline.mechanical_close", {"dry_run": True}, tmp_path)["report"]["verdicts"] == []


def test_unknown_repo_is_refused_naming_the_param(tmp_path):
    for name in ("learn_lessons_pipeline.apply_mechanical", "learn_lessons_pipeline.mechanical_close"):
        for dry in (True, False):
            with pytest.raises(ipc.CallerFacingValidationError, match="param `repo`"):
                _call(name, {"repo": str(tmp_path / "nope"), "dry_run": dry}, tmp_path)


def test_archive_digests_memoized_and_invalidated_on_new_row(tmp_path, monkeypatch):
    from coordinator_core.learn_lessons_pipeline import mechanical_close as mc

    month = tmp_path / "archive" / "lessons" / "2026-11"
    month.mkdir(parents=True)
    (month / "a.yaml").write_text("body: first lesson body text here\n", encoding="utf-8")
    calls = []
    real = mc._load_doc
    monkeypatch.setattr(mc, "_load_doc", lambda p: calls.append(p) or real(p))
    mc._ARCHIVE_DIGEST_CACHE.clear()
    first = mc._archive_digests(tmp_path)
    assert mc._archive_digests(tmp_path) == first and len(calls) == 1
    (month / "b.yaml").write_text("body: second lesson body text here\n", encoding="utf-8")
    assert len(mc._archive_digests(tmp_path)) == 2
