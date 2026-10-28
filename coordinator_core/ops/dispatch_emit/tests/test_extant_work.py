"""lookup_extant: keyed reads only, exact slug/ask-hash hits, near band, open-handoff filter, no spawns."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import extant_work as ew

INTENT = "stop re-authoring existing plans by looking up extant routing artifacts before authoring"
STEM = "2026-10-25-ab12cd-1-my-slug"
SIZING_REL = f"state/sizings/{STEM}.yaml"


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _plan(status: str, title: str = "Unrelated") -> str:
    return f'---\ntitle: "{title}"\nstatus: {status}\n---\n# body\n'


def test_exact_slug_plan_carries_status(tmp_path):
    _w(tmp_path, "docs/plans/my-slug.md", _plan("approved"))
    res = ew.lookup_extant(str(tmp_path), "my-slug", "", "zzz")
    (hit,) = res.exact
    assert (hit.kind, hit.match, hit.key, hit.plan_status) == ("plan", "exact", "slug", "approved")
    assert hit.path == "docs/plans/my-slug.md"


def test_stem_keyed_plan_is_found_with_sizing_rel(tmp_path):
    _w(tmp_path, SIZING_REL, f"name: my-slug\nintent: '{INTENT}'\n")
    _w(tmp_path, f"docs/plans/{STEM}.md", _plan("executing"))
    res = ew.lookup_extant(str(tmp_path), "my-slug", "", "zzz", sizing_rel=SIZING_REL)
    assert [(h.kind, h.path, h.plan_status) for h in res.exact] == [
        ("plan", f"docs/plans/{STEM}.md", "executing")
    ]


def test_sizing_plan_edge_is_exact_and_sizing_is_not_its_own_hit(tmp_path):
    _w(tmp_path, SIZING_REL,
       f"name: other\nintent: '{INTENT}'\nplan: \"docs/plans/2026-10-25-dated-name.md\"\n")
    _w(tmp_path, "docs/plans/2026-10-25-dated-name.md", _plan("approved"))
    res = ew.lookup_extant(str(tmp_path), "nomatch", ew.compute_ask_hash(INTENT), INTENT,
                           sizing_rel=SIZING_REL)
    assert [(h.kind, h.path) for h in res.exact] == [("plan", "docs/plans/2026-10-25-dated-name.md")]
    assert all(h.path != SIZING_REL for h in res.exact + res.related)


def test_unkeyed_dated_plan_is_not_found(tmp_path):
    _w(tmp_path, "docs/plans/2026-10-25-my-slug.md", _plan("approved"))
    assert ew.lookup_extant(str(tmp_path), "my-slug", "", "zzz").exact == ()


def test_ask_hash_matches_keyed_sizing_intent(tmp_path):
    _w(tmp_path, "state/sizings/a.yaml", f"name: other\nintent: '{INTENT}'\n")
    res = ew.lookup_extant(str(tmp_path), "a", ew.compute_ask_hash(INTENT), "")
    (hit,) = res.exact
    assert (hit.kind, hit.key) == ("sizing", "slug")
    _w(tmp_path, "state/sizings/b.yaml", f"name: other\nintent: '{INTENT}'\n")
    res = ew.lookup_extant(str(tmp_path), "b", "0" * 64, "")
    assert res.exact[0].key == "slug"
    res = ew.lookup_extant(str(tmp_path), "nomatch", ew.compute_ask_hash(INTENT), "")
    assert res.exact == ()


def test_unreached_sizing_is_not_near_and_slug_named_one_is_exact(tmp_path):
    _w(tmp_path, "state/sizings/near.yaml",
       "name: near\nintent: looking up extant routing artifacts before authoring plans again\n")
    _w(tmp_path, "state/sizings/far.yaml", "name: far\nintent: rotate the database credentials nightly\n")
    near = ew.lookup_extant(str(tmp_path), "near-x", "", INTENT, run_ids=())
    assert near.near == ()
    # A near hit needs the record to have been reached: the slug names it, then text decides.
    res = ew.lookup_extant(str(tmp_path), "far", "", INTENT)
    assert res.exact and res.exact[0].path == "state/sizings/far.yaml"


def test_near_text_hit_on_a_reached_stem_keyed_plan(tmp_path):
    _w(tmp_path, f"docs/plans/{STEM}.md",
       _plan("draft", "looking up extant routing artifacts before authoring plans again"))
    res = ew.lookup_extant(str(tmp_path), "other", "", INTENT, sizing_rel=SIZING_REL)
    assert res.exact == ()
    assert [h.path for h in res.near] == [f"docs/plans/{STEM}.md"]
    assert res.near[0].match == "near" and res.near[0].score >= ew.NEAR_MIN
    _w(tmp_path, f"docs/plans/{STEM}.md", _plan("draft", "rotate the database credentials nightly"))
    far = ew.lookup_extant(str(tmp_path), "other", "", INTENT, sizing_rel=SIZING_REL)
    assert far.exact == () and far.near == ()


def test_open_baton_handoff_is_an_exact_hit(tmp_path):
    head = "---\ntitle: x\nstatus: {}\nsummary: {}\n---\n"
    _w(tmp_path, SIZING_REL, f"name: my-slug\nintent: nothing\nbaton: \"state/handoffs/h.md\"\n")
    _w(tmp_path, "state/handoffs/h.md", head.format("open", INTENT))
    res = ew.lookup_extant(str(tmp_path), "other", "", INTENT, sizing_rel=SIZING_REL)
    assert [h.path for h in res.exact] == ["state/handoffs/h.md"]


def test_closed_baton_handoff_is_skipped(tmp_path):
    head = "---\ntitle: x\nstatus: {}\nsummary: {}\n---\n"
    _w(tmp_path, SIZING_REL, "name: my-slug\nintent: nothing\nbaton: \"state/handoffs/h.md\"\n")
    _w(tmp_path, "state/handoffs/h.md", head.format("done", INTENT))
    res = ew.lookup_extant(str(tmp_path), "other", "", INTENT, sizing_rel=SIZING_REL)
    assert res.exact == () and res.related == ()


def test_folded_multiline_intent_is_read(tmp_path):
    _w(tmp_path, "state/sizings/f.yaml",
       "name: f\nintent: 'rotate\n  looking up extant routing artifacts before authoring existing plans'\nroute: plan\n")
    res = ew.lookup_extant(str(tmp_path), "f", "", INTENT)
    assert [h.path for h in res.exact] == ["state/sizings/f.yaml"]


def test_open_handoff_statuses_match_schema_enum():
    schema = Path(__file__).resolve().parents[3] / "frontmatter" / "schemas" / "handoff.schema.json"
    enum = json.loads(schema.read_text(encoding="utf-8"))["properties"]["status"]["enum"]
    assert ew.OPEN_HANDOFF_STATUSES == frozenset(enum)


def test_ledger_run_named_by_caller_exact_by_ask_hash(tmp_path):
    _w(tmp_path, "state/warp/run-a/relayed-ask.md", INTENT)
    _w(tmp_path, "state/warp/run-b/relayed-ask.md", INTENT)
    res = ew.lookup_extant(str(tmp_path), "nomatch", ew.compute_ask_hash(INTENT), "", run_ids=("run-a",))
    assert [(h.kind, h.path, h.key) for h in res.exact] == [
        ("ledger", "state/warp/run-a/relayed-ask.md", "ask_hash")
    ]


def test_run_id_escaping_the_warp_dir_is_ignored(tmp_path):
    _w(tmp_path, "state/relayed-ask.md", INTENT)
    res = ew.lookup_extant(str(tmp_path), "x", ew.compute_ask_hash(INTENT), "", run_ids=("..",))
    assert res.exact == ()


def test_wiki_hit_only_for_a_page_the_sizing_names(tmp_path):
    page = "docs/wiki/machine-load-norm.md"
    _w(tmp_path, page, "# Looking up extant routing artifacts before authoring plans\n")
    _w(tmp_path, "docs/wiki/guard-messaging.md", "# Looking up extant routing artifacts before authoring plans\n")
    _w(tmp_path, SIZING_REL, f"name: my-slug\nintent: x\nnotes: see {page} for the rule\n")
    res = ew.lookup_extant(str(tmp_path), "nomatch", "", INTENT, sizing_rel=SIZING_REL)
    assert res.exact == () and res.near == ()
    assert [(h.kind, h.path) for h in res.related] == [("wiki", page)]


def test_missing_corpus_is_empty(tmp_path):
    assert ew.lookup_extant(str(tmp_path), "s", "h", "i") == ew.ExtantLookup()
    assert ew.lookup_extant(str(tmp_path), "s", "h", "i", sizing_rel=SIZING_REL) == ew.ExtantLookup()


def test_accepted_statuses_exclude_reviewed_and_draft():
    assert ew.ACCEPTED_PLAN_STATUSES == {"approved", "executing", "landed", "implemented"}


def test_no_subprocess(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _w(tmp_path, "docs/plans/s.md", _plan("draft"))
    assert ew.lookup_extant(str(tmp_path), "s", "", INTENT).exact


def test_answers_with_directory_listing_forbidden(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tree walk")

    _w(tmp_path, SIZING_REL, f"name: my-slug\nintent: '{INTENT}'\nplan: \"docs/plans/p.md\"\n")
    _w(tmp_path, "docs/plans/p.md", _plan("approved"))
    with monkeypatch.context() as m:
        m.setattr(os, "scandir", boom)
        m.setattr(os, "listdir", boom)
        res = ew.lookup_extant(str(tmp_path), "my-slug", ew.compute_ask_hash(INTENT), INTENT,
                               sizing_rel=SIZING_REL)
    assert [(h.kind, h.plan_status) for h in res.exact] == [("plan", "approved")]


def test_lookup_cost_is_flat_in_corpus_size(tmp_path):
    for i in range(300):
        _w(tmp_path, f"docs/plans/2026-01-01-filler-{i}.md", _plan("draft"))
        _w(tmp_path, f"state/sizings/2026-01-01-filler-{i}.yaml", f"name: f{i}\nintent: filler {i}\n")
    _w(tmp_path, SIZING_REL, f"name: my-slug\nintent: '{INTENT}'\nplan: \"docs/plans/p.md\"\n")
    _w(tmp_path, "docs/plans/p.md", _plan("approved"))
    t0 = time.process_time()
    res = ew.lookup_extant(str(tmp_path), "my-slug", ew.compute_ask_hash(INTENT), INTENT,
                           sizing_rel=SIZING_REL)
    assert res.exact
    assert time.process_time() - t0 < 0.05
