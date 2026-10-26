"""lookup_extant: exact slug/ask-hash hits, near band, open-handoff filter, no spawns."""

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import extant_work as ew

INTENT = "stop re-authoring existing plans by looking up extant routing artifacts before authoring"


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _plan(status: str, title: str = "Unrelated") -> str:
    return f'---\ntitle: "{title}"\nstatus: {status}\n---\n# body\n'


def test_exact_slug_plan_carries_status(tmp_path):
    _w(tmp_path, "docs/plans/2026-10-25-my-slug.md", _plan("approved"))
    res = ew.lookup_extant(str(tmp_path), "my-slug", "", "zzz")
    (hit,) = res.exact
    assert (hit.kind, hit.match, hit.key, hit.plan_status) == ("plan", "exact", "slug", "approved")
    assert hit.path == "docs/plans/2026-10-25-my-slug.md"


def test_slug_does_not_match_a_longer_slug_suffix_partially(tmp_path):
    _w(tmp_path, "docs/plans/2026-10-25-other-my-slug2.md", _plan("draft"))
    res = ew.lookup_extant(str(tmp_path), "my-slug", "", "zzz")
    assert res.exact == ()


def test_ask_hash_matches_sizing_intent(tmp_path):
    _w(tmp_path, "state/sizings/a.yaml", f"name: other\nintent: '{INTENT}'\n")
    res = ew.lookup_extant(str(tmp_path), "nomatch", ew.compute_ask_hash(INTENT), "")
    (hit,) = res.exact
    assert (hit.kind, hit.key) == ("sizing", "ask_hash")


def test_near_band_both_sides(tmp_path):
    _w(tmp_path, "state/sizings/near.yaml",
       "name: near\nintent: looking up extant routing artifacts before authoring plans again\n")
    _w(tmp_path, "state/sizings/far.yaml", "name: far\nintent: rotate the database credentials nightly\n")
    res = ew.lookup_extant(str(tmp_path), "nomatch", "", INTENT)
    assert [h.path for h in res.near] == ["state/sizings/near.yaml"]
    assert res.near[0].match == "near" and res.near[0].score >= ew.NEAR_MIN
    assert res.exact == ()


def test_folded_multiline_intent_is_read(tmp_path):
    _w(tmp_path, "state/sizings/f.yaml",
       "name: f\nintent: 'rotate\n  looking up extant routing artifacts before authoring existing plans'\nroute: plan\n")
    res = ew.lookup_extant(str(tmp_path), "nomatch", "", INTENT)
    assert [h.path for h in res.near] == ["state/sizings/f.yaml"]


def test_only_open_handoffs_are_scanned(tmp_path):
    head = "---\ntitle: x\nstatus: {}\nsummary: {}\n---\n"
    _w(tmp_path, "state/handoffs/2026-10-25-open-one.md", head.format("open", INTENT))
    _w(tmp_path, "state/handoffs/2026-10-25-closed-one.md", head.format("done", INTENT))
    res = ew.lookup_extant(str(tmp_path), "nomatch", "", INTENT)
    assert [h.path for h in res.near] == ["state/handoffs/2026-10-25-open-one.md"]


def test_open_handoff_statuses_match_schema_enum():
    schema = Path(__file__).resolve().parents[3] / "frontmatter" / "schemas" / "handoff.schema.json"
    enum = json.loads(schema.read_text(encoding="utf-8"))["properties"]["status"]["enum"]
    assert ew.OPEN_HANDOFF_STATUSES == frozenset(enum)


def test_ledger_run_exact_by_slug_and_ask_hash(tmp_path):
    _w(tmp_path, "state/warp/run-a/relayed-ask.md", INTENT)
    _w(tmp_path, "state/warp/run-a/run.json",
       json.dumps({"sizing_rel": "state/sizings/2026-10-25-ab12-1-my-slug.yaml"}))
    by_slug = ew.lookup_extant(str(tmp_path), "my-slug", "", "")
    assert [(h.kind, h.key) for h in by_slug.exact] == [("ledger", "slug")]
    by_hash = ew.lookup_extant(str(tmp_path), "nomatch", ew.compute_ask_hash(INTENT), "")
    assert [(h.kind, h.key) for h in by_hash.exact] == [("ledger", "ask_hash")]


def test_wiki_hit_is_related_not_near_or_exact(tmp_path):
    _w(tmp_path, "docs/wiki/routing.md", "# Looking up extant routing artifacts before authoring plans\n")
    res = ew.lookup_extant(str(tmp_path), "routing", "", INTENT)
    assert res.exact == () and res.near == ()
    assert [h.kind for h in res.related] == ["wiki"]


def test_missing_corpus_is_empty(tmp_path):
    assert ew.lookup_extant(str(tmp_path), "s", "h", "i") == ew.ExtantLookup()


def test_accepted_statuses_exclude_reviewed_and_draft():
    assert ew.ACCEPTED_PLAN_STATUSES == {"approved", "executing", "landed", "implemented"}


def test_no_subprocess(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _w(tmp_path, "docs/plans/2026-10-25-s.md", _plan("draft"))
    assert ew.lookup_extant(str(tmp_path), "s", "", INTENT).exact
