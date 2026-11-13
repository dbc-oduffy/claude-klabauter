"""Tests for "ceremony.update_docs_worklist": registry, classification, cache, the
git-spawn ceiling and the DR-344 500ms process-time bar at default params.

The git runner is replaced by a counting double, so nothing here spawns a process.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.update_docs_worklist as uw
from coordinator_core.git.run import GitResult
from coordinator_core.ipc import _REGISTRY

_OP = "ceremony.update_docs_worklist"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_NOW = _dt.datetime.now(tz=_dt.timezone.utc)


class _Git:
    """Counting `run_git` double: `diff` answers `changed`, `rev-parse` answers `head`."""

    def __init__(self, changed=(), head=_SHA_B, diff_ok=True):
        self.calls: list[list[str]] = []
        self.changed = list(changed)
        self.head = head
        self.diff_ok = diff_ok

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "rev-parse":
            return GitResult(0, self.head + "\n", "", False)
        if not self.diff_ok:
            return GitResult(128, "", "bad object", False)
        return GitResult(0, "".join(p + "\0" for p in self.changed), "", False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "tasks").mkdir()
    (root / "state" / "cross-repo" / "archive").mkdir(parents=True)
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE", str(tmp_path / "cache"))
    return root


def _write(path: Path, text: str, *, age_days: float = 0.0) -> Path:
    path.write_text(text, encoding="utf-8")
    if age_days:
        ts = (_NOW - _dt.timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
    return path


def _plan(repo: Path, name: str, status: str, *, age_days=60.0, supersedes: str = "") -> str:
    sup = f"supersedes: {supersedes}\n" if supersedes else ""
    _write(repo / "docs" / "plans" / name, f"---\nstatus: {status}\n{sup}---\nbody\n", age_days=age_days)
    return f"docs/plans/{name}"


def _run(repo: Path, git: _Git, monkeypatch, params=None) -> dict:
    monkeypatch.setattr(uw, "run_git", git)
    return uw._ceremony_update_docs_worklist(params if params is not None else {}, repo_root=repo / ".git")


def test_op_registered():
    assert _OP in _REGISTRY


def test_repo_root_none_raises():
    with pytest.raises(ValueError):
        uw._ceremony_update_docs_worklist({}, repo_root=None)


def test_prune_classification_and_lineage_hold(repo, monkeypatch):
    old = _plan(repo, "old-superseded.md", "superseded")
    _plan(repo, "fresh-superseded.md", "superseded", age_days=1)
    _plan(repo, "live.md", "in_progress")
    held = _plan(repo, "held-abandoned.md", "abandoned")
    _plan(repo, "successor.md", "in_progress", supersedes="docs/plans/held-abandoned.md")
    _write(repo / "tasks" / "a.md", "---\nstatus: superseded\n---\n")
    _write(repo / "tasks" / "b.md", "---\nstatus: open\n---\n")
    (repo / "tasks" / "c.txt").write_text("---\nstatus: superseded\n---\n")
    picked = (_NOW - _dt.timedelta(days=200)).isoformat()
    _write(repo / "state" / "cross-repo" / "archive" / "old.md", f"---\nstatus: actioned\npicked_up_at: {picked}\n---\n")
    _write(repo / "state" / "cross-repo" / "archive" / "new.md", f"---\nstatus: actioned\npicked_up_at: {_NOW.isoformat()}\n---\n")

    out = _run(repo, _Git(), monkeypatch)

    prune = out["decided_by_op"]["prune"]
    assert prune["plans"] == [old]
    assert prune["tasks"] == ["tasks/a.md"]
    assert prune["crossrepo_archive"] == ["state/cross-repo/archive/old.md"]
    assert out["decided_by_op"]["lineage_blocked"] == 1
    assert held not in prune["plans"]
    kinds = {w["kind"]: w for w in out["agent_worklist"]}
    assert kinds["lineage_held_disposed_plan"]["paths"] == [held]


def test_drift_becomes_agent_worklist_and_directory_staleness(repo, monkeypatch):
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))

    stale = _run(repo, _Git(changed=["coordinator_core/x.py", "coordinator/bin/y.py"]), monkeypatch)
    assert stale["ack_head"] == _SHA_A
    assert stale["decided_by_op"]["directory_md_stale"] is True
    drift = [w for w in stale["agent_worklist"] if w["kind"] == "doc_drift_review"]
    assert drift[0]["paths"] == ["coordinator_core/x.py"]

    fresh = _run(repo, _Git(changed=["coordinator_core/x.py", "coordinator_core/DIRECTORY.md"]), monkeypatch)
    assert fresh["decided_by_op"]["directory_md_stale"] is False


def test_default_call_one_spawn_cold_seeds_baseline_without_drift(repo, monkeypatch):
    git = _Git()
    out = _run(repo, git, monkeypatch)
    assert [c[0] for c in git.calls] == ["rev-parse"]
    assert out["ack_head"] == _SHA_B
    assert out["changed_since_ack"]["total"] == 0
    assert out["agent_worklist"] == []


def test_unreachable_ack_reseeds_and_stops_respawning(repo, monkeypatch):
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))

    first = _Git(diff_ok=False)
    out = _run(repo, first, monkeypatch)
    assert out["ack_head"] == _SHA_B and out["changed_since_ack"]["total"] == 0

    second = _Git()
    _run(repo, second, monkeypatch)
    assert [c[0] for c in second.calls] == ["diff"]


def test_ack_advances_baseline_only_when_asked(repo, monkeypatch):
    _run(repo, _Git(head=_SHA_A), monkeypatch)
    default = _Git(head=_SHA_B)
    assert _run(repo, default, monkeypatch)["ack_head"] == _SHA_A
    assert [c[0] for c in default.calls] == ["diff"]

    acked = _Git(head=_SHA_B)
    assert _run(repo, acked, monkeypatch, {"ack": True})["ack_head"] == _SHA_B
    assert len(acked.calls) == 2
    assert _run(repo, _Git(head=_SHA_B), monkeypatch)["ack_head"] == _SHA_B


def test_warm_cache_rereads_nothing_and_rereads_changed_file(repo, monkeypatch):
    for i in range(5):
        _plan(repo, f"p{i}.md", "superseded")
    cold = _run(repo, _Git(), monkeypatch)
    assert cold["cache"]["state"] == "cold" and cold["cache"]["reread"] == 5

    warm = _run(repo, _Git(), monkeypatch)
    assert warm["cache"]["state"] == "warm" and warm["cache"]["reread"] == 0

    _plan(repo, "p0.md", "in_progress")
    edited = _run(repo, _Git(), monkeypatch)
    assert edited["cache"]["reread"] == 1
    assert edited["decided_by_op"]["prune_totals"]["plans"] == 4


def test_reread_budget_leaves_unscanned_files_unprunable(repo, monkeypatch):
    for i in range(5):
        _plan(repo, f"p{i}.md", "superseded")
    monkeypatch.setattr(uw, "REREAD_BUDGET_SECS", -1.0)
    out = _run(repo, _Git(), monkeypatch)
    assert out["cache"]["complete"] is False
    assert out["cache"]["reread"] == 0
    assert out["decided_by_op"]["prune_totals"]["plans"] == 0


def test_crlf_and_preamble_frontmatter_use_shared_parser(tmp_path):
    crlf = _write(tmp_path / "a.md", "---\r\nstatus: superseded\r\nsupersedes: docs/plans/x.md\r\n---\r\nbody\r\n")
    assert uw._frontmatter_fields(str(crlf)) == ("superseded", "docs/plans/x.md", "")
    plain = _write(tmp_path / "b.md", '---\nstatus: "actioned"\npicked_up_at: 2026-01-01T00:00:00Z\n---\n')
    assert uw._frontmatter_fields(str(plain)) == ("actioned", "", "2026-01-01T00:00:00Z")
    none = _write(tmp_path / "c.md", "no frontmatter\n")
    assert uw._frontmatter_fields(str(none)) == ("", "", "")


def test_output_is_bounded(repo, monkeypatch):
    long_names = ["coordinator_core/" + "d" * 480 + f"/m{i}.py" for i in range(400)]
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))
    out = _run(repo, _Git(changed=long_names), monkeypatch)
    assert len(json.dumps(out, separators=(",", ":"))) <= uw.OUTPUT_BYTE_CAP
    assert out["changed_since_ack"]["total"] == 400
    assert out["changed_since_ack"]["truncated"] is True


def test_default_result_projects_under_the_long_prompt_tier(repo, monkeypatch):
    from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS, estimate_tokens

    assert estimate_tokens(uw.OUTPUT_BYTE_CAP) <= LONG_PROMPT_TIER_TOKENS
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))
    out = _run(repo, _Git(changed=[f"coordinator_core/m{i}.py" for i in range(300)]), monkeypatch)
    assert estimate_tokens(len(json.dumps(out, separators=(",", ":")).encode())) <= LONG_PROMPT_TIER_TOKENS
    assert out["agent_worklist"][0]["total"] == 300


def test_worklist_row_paths_are_cut_to_the_tier_and_a_fitting_row_is_untouched(monkeypatch):
    import coordinator_core.distill.prompt_tier as pt

    monkeypatch.setattr(pt, "LONG_PROMPT_TIER_TOKENS", 10)
    paths = ["p" * 17 for _ in range(10)]
    over = {"kind": "doc_drift_review", "paths": list(paths)}
    under = {"kind": "lineage_held_disposed_plan", "paths": paths[:1]}
    uw._bound_phase_paths([over, under])
    assert over["paths_truncated"] is True
    assert 0 < len(over["paths"]) < len(paths)
    assert "paths_truncated" not in under and under["paths"] == paths[:1]


def test_cache_root_falls_back_to_repo_local_without_the_key(repo, monkeypatch):
    monkeypatch.setattr(uw, "registry_get", lambda key: None)
    assert uw._cache_path(repo) == repo / ".coordinator-local" / "cache" / uw.CACHE_SUBDIR / uw.CACHE_FILE


def test_default_params_hold_the_500ms_bar_with_at_most_one_git_spawn(repo, monkeypatch):
    """DR-344: {} returns agent_worklist and decided_by_op under 500ms process time, <=1 git spawn."""
    for i in range(1200):
        _plan(repo, f"p{i}.md", "superseded" if i % 3 == 0 else "in_progress")
    for i in range(800):
        _write(repo / "tasks" / f"t{i}.md", "---\nstatus: open\n---\n")
    picked = (_NOW - _dt.timedelta(days=200)).isoformat()
    for i in range(1000):
        _write(repo / "state" / "cross-repo" / "archive" / f"m{i}.md", f"---\nstatus: actioned\npicked_up_at: {picked}\n---\n")

    for label in ("cold", "warm"):
        git = _Git()
        monkeypatch.setattr(uw, "run_git", git)
        start = time.process_time()
        out = uw._ceremony_update_docs_worklist({}, repo_root=repo / ".git")
        elapsed_ms = (time.process_time() - start) * 1000
        assert "agent_worklist" in out and "decided_by_op" in out
        assert len(git.calls) <= 1, label
        assert elapsed_ms < 500, f"{label}: {elapsed_ms:.0f}ms"
    assert out["decided_by_op"]["prune_totals"]["plans"] == 400
    assert out["cache"]["reread"] == 0


_PHASE_IDS = [
    "0", "1-2b", "3", "4", "5", "6", "7", "8", "8b", "9", "9b", "10", "10b", "11", "11a", "11b", "11g",
    "11c", "11d", "12", "11f", "11h", "11h2", "11i", "11j", "11k", "11m", "11n", "11o", "11p", "13", "14", "15",
]


def _handoff(repo: Path, rel: str, text: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _archived(repo: Path, n: int) -> list[str]:
    return [
        _handoff(repo, f"archive/handoffs/2026-01/2026-01-{i + 1:02d}_000000_h{i}.md", f"---\nid: h{i}\n---\nbody\n")
        for i in range(n)
    ]


def _counts(out: dict) -> dict:
    return out["decided_by_op"]["archived_handoffs"]


def _seed_ack(cache_dir_env: str) -> None:
    cache_dir = Path(cache_dir_env) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))


def test_phase_ids_are_the_33_row_table():
    assert list(uw.UPDATE_DOCS_PHASE_IDS) == _PHASE_IDS
    assert len(set(_PHASE_IDS)) == 33


def test_no_change_tree_has_no_agent_verdict_and_a_verdict_per_phase(repo, monkeypatch):
    verdicts = _run(repo, _Git(), monkeypatch)["phase_verdicts"]
    assert list(verdicts) == _PHASE_IDS
    assert uw.AGENT not in verdicts.values()
    assert verdicts["6"] == "noop" and verdicts["1-2b"] == "noop"
    assert {verdicts[p] for p in ("11a", "12", "13")} == {"noop"}
    assert {verdicts[p] for p in ("9b", "10", "11c", "11d", "11i", "11j")} == {"op"}


def test_drift_and_lineage_items_make_only_their_phases_agent(repo, monkeypatch):
    _seed_ack(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"])
    _plan(repo, "held-abandoned.md", "abandoned")
    _plan(repo, "successor.md", "in_progress", supersedes="docs/plans/held-abandoned.md")

    verdicts = _run(repo, _Git(changed=["coordinator_core/x.py"]), monkeypatch)["phase_verdicts"]

    assert {p for p, v in verdicts.items() if v == "agent"} == {"1-2b", "3", "7", "8"}
    assert verdicts["11a"] == "op" and verdicts["13"] == "op"


def test_non_source_drift_is_op_not_agent(repo, monkeypatch):
    _seed_ack(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"])
    verdicts = _run(repo, _Git(changed=["coordinator/bin/y.py"]), monkeypatch)["phase_verdicts"]
    assert uw.AGENT not in verdicts.values() and verdicts["3"] == "op"


def test_phase_6_follows_the_lessons_outbox(repo, monkeypatch):
    assert _run(repo, _Git(), monkeypatch)["phase_verdicts"]["6"] == "noop"
    (repo / "state" / "lessons-outbox").mkdir(parents=True)
    (repo / "state" / "lessons-outbox" / "x.yaml").write_text("a: 1\n")
    assert _run(repo, _Git(), monkeypatch)["phase_verdicts"]["6"] == "op"


def test_archived_handoffs_prune_all_but_newest_ten_and_counts_sum_to_total(repo, monkeypatch):
    names = _archived(repo, 14)
    out = _run(repo, _Git(), monkeypatch)
    assert _counts(out) == {"total": 14, "prune": 4, "keep": 10, "indeterminate": 0}
    assert out["decided_by_op"]["prune"]["archived_handoffs"] == names[:4]
    assert out["decided_by_op"]["prune_totals"]["archived_handoffs"] == 4


def test_archived_handoff_guards_keep_what_a_dependent_or_roadmap_or_referrer_needs(repo, monkeypatch):
    names = _archived(repo, 15)
    for i, kind in ((0, "roadmap-baton"), (1, "spinoff-roadmap")):
        _handoff(repo, names[i], f"---\nid: h{i}\nkind: {kind}\n---\n")
    _handoff(repo, "state/handoffs/live.md", f"---\nid: live\npredecessor: {Path(names[2]).name}\nblocked_by: [h3]\n---\n")
    _handoff(repo, names[4], "---\nstatus: x\n---\n")

    out = _run(repo, _Git(), monkeypatch)

    assert out["decided_by_op"]["prune"]["archived_handoffs"] == []
    assert _counts(out) == {"total": 15, "prune": 0, "keep": 14, "indeterminate": 1}


def test_archived_handoffs_never_prune_when_the_corpus_pass_or_reread_budget_is_spent(repo, monkeypatch):
    _archived(repo, 14)
    monkeypatch.setattr(uw, "BLOCKED_PASS_START_SECS", -1.0)
    out = _run(repo, _Git(), monkeypatch)
    assert out["decided_by_op"]["prune"]["archived_handoffs"] == []
    assert _counts(out) == {"total": 14, "prune": 0, "keep": 10, "indeterminate": 4}

    monkeypatch.setattr(uw, "REREAD_BUDGET_SECS", -1.0)
    spent = _run(repo, _Git(), monkeypatch)
    assert spent["decided_by_op"]["prune"]["archived_handoffs"] == []


def test_archived_handoff_verdict_is_cached_until_the_corpus_changes(repo, monkeypatch):
    _archived(repo, 14)
    _run(repo, _Git(), monkeypatch)
    warm = _run(repo, _Git(), monkeypatch)
    assert _counts(warm)["prune"] == 4
    _handoff(repo, "state/handoffs/live.md", "---\nid: live\nblocked_by: [h0]\n---\n")
    changed = _run(repo, _Git(), monkeypatch)
    assert _counts(changed)["prune"] == 3 and _counts(changed)["keep"] == 11


_MANIFEST = (
    "terms:\n"
    "  - term: chroma\n"
    "    removed: 2026-08-18\n"
    "    note: replaced by the embedded store\n"
)


def _retired_fixture(repo: Path) -> None:
    (repo / "docs" / "wiki").mkdir(parents=True, exist_ok=True)
    _write(repo / "docs" / "retired-terms.yaml", _MANIFEST)
    _write(repo / "docs" / "wiki" / "host-vs-addons.md", "# Host\n\nVectors live in Chroma.\nclean line\n\nThe chroma collection is per-addon.\n")
    _write(repo / "docs" / "wiki" / "addon-protocol.md", "# Protocol\n\n- upsert into the CHROMA store\n- chromatic aberration is unrelated\n")


def test_retired_term_hits_reach_the_default_worklist_with_file_line_and_record(repo, monkeypatch):
    _retired_fixture(repo)
    git = _Git()

    out = _run(repo, git, monkeypatch)

    item = next(w for w in out["agent_worklist"] if w["kind"] == uw.RETIRED_TERM_KIND)
    assert item["total"] == 3
    assert item["paths"] == [
        "docs/wiki/addon-protocol.md:3 chroma (removed 2026-08-18): replaced by the embedded store",
        "docs/wiki/host-vs-addons.md:3 chroma (removed 2026-08-18): replaced by the embedded store",
        "docs/wiki/host-vs-addons.md:6 chroma (removed 2026-08-18): replaced by the embedded store",
    ]
    assert {(h["file"], h["line"], h["removed"]) for h in item["hits"]} == {
        ("docs/wiki/addon-protocol.md", 3, "2026-08-18"),
        ("docs/wiki/host-vs-addons.md", 3, "2026-08-18"),
        ("docs/wiki/host-vs-addons.md", 6, "2026-08-18"),
    }
    assert out["phase_verdicts"]["3"] == uw.AGENT
    assert item["complete"] is True
    assert len(git.calls) <= 1


def test_retired_item_reports_incomplete_when_the_scan_budget_cuts_it(repo, monkeypatch):
    _retired_fixture(repo)
    ticks = iter([0.0, 0.0, 99.0, 99.0, 99.0, 99.0])
    monkeypatch.setattr("coordinator_core.updatedocs.retired_terms._clock", lambda: next(ticks, 99.0))

    out = _run(repo, _Git(), monkeypatch)

    item = next(w for w in out["agent_worklist"] if w["kind"] == uw.RETIRED_TERM_KIND)
    assert item["complete"] is False
    assert "time budget" in item["why"]


def test_no_manifest_yields_no_retired_item_and_a_malformed_one_is_reported_not_raised(repo, monkeypatch):
    (repo / "docs" / "wiki").mkdir(parents=True)
    _write(repo / "docs" / "wiki" / "p.md", "chroma\n")
    clean = _run(repo, _Git(), monkeypatch)
    assert not [w for w in clean["agent_worklist"] if w["kind"].startswith("retired_term")]
    assert clean["phase_verdicts"]["3"] != uw.AGENT

    _write(repo / "docs" / "retired-terms.yaml", "terms: nope\n")
    bad = _run(repo, _Git(), monkeypatch)
    item = next(w for w in bad["agent_worklist"] if w["kind"] == uw.RETIRED_MANIFEST_KIND)
    assert "terms" in item["why"]
