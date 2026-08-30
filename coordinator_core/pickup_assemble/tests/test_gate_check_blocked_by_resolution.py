"""
coordinator_core.pickup_assemble.tests.test_gate_check_blocked_by_resolution

Purpose: chunk C2 (plan 2026-08-30-the-gate-brief-reads-a-list-where-the-
record-wrote-one) — `reconcile.handoff_corpus._build_blocker_index` and
`pickup_assemble.compute_gate_blocker_evidence` resolve EVERY id in an
`awaiting_gate` handoff's `blocked_by` list to a record (deployment_state,
holder, holder reachability), instead of handing the EM a bare id list to
hand-walk the corpus for.

Pinned per the dispatch brief:
  - a `stub_id: "sat-06"` (quoted, the corpus-majority shape — measured 625
    of 653 id lines) is reachable by the UNQUOTED lookup key `sat-06`
    (`unquote_yaml_scalar` MUST run on the extracted value before it is used
    as an index key — an unquoted key means 96% of lookups resolve as
    `unresolvable`, the exact dangling-ref symptom C3 argues cannot happen);
  - a record whose frontmatter block exceeds the 4096-byte truncated-read
    floor still resolves, via `_frontmatter_head_bytes`'s full-read
    fallback — the 4096-byte constant is self-correcting, not trusted
    blindly;
  - the four-way answer class (`resolved` / `unresolvable` / `ambiguous` /
    `scan_incomplete`) each get their own coverage.

Neither `_build_blocker_index` nor `compute_gate_blocker_evidence` shells
out to git — both operate purely on frontmatter already on disk — so this
file needs no `spawns_process` real-git fixture, unlike its `brief()`-level
sibling `test_brief_awaiting_gate_typed_fields.py`.

Spec backlink: docs/plans/2026-08-30-the-gate-brief-reads-a-list-where-the-
record-wrote-one.md § chunk C2.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_gate_check_blocked_by_resolution.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
from coordinator_core.reconcile.handoff_corpus import (
    _BLOCKER_INDEX_HEAD_BYTES,
    _build_blocker_index,
)

pytestmark = [pytest.mark.cadence]


def _write_handoff(path: Path, fm_body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm_body}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def test_quoted_stub_id_reachable_by_unquoted_lookup_key(tmp_path: Path):
    repo = tmp_path
    path = _write_handoff(
        repo / "state" / "handoffs" / "sat-06.md",
        'title: "Blocker"\n'
        "created: 2026-01-01\n"
        "status: open\n"
        'stub_id: "sat-06"\n'
        "deployment_state: shipped\n",
    )

    index, scan_errors = _build_blocker_index(repo)

    assert scan_errors == []
    assert index.get("sat-06") == [path]

    evidence = pa.compute_gate_blocker_evidence(repo, ["sat-06"])
    assert len(evidence) == 1
    assert evidence[0]["status"] == "resolved"
    assert evidence[0]["resolved"] is True
    assert evidence[0]["deployment_state"] == "shipped"


def test_oversized_frontmatter_still_resolves_via_full_read_fallback(tmp_path: Path):
    repo = tmp_path
    # Padding pushes the `stub_id:` line well past the 4096-byte truncated
    # read — the closing `---` fence is not found within that head, which
    # must trigger `_frontmatter_head_bytes`'s full-file fallback rather
    # than silently dropping the id.
    padding = "\n".join(f"note_{i}: filler text to pad the frontmatter block" for i in range(200))
    assert len(padding.encode("utf-8")) > _BLOCKER_INDEX_HEAD_BYTES
    path = _write_handoff(
        repo / "state" / "handoffs" / "oversized.md",
        f"title: \"Oversized\"\ncreated: 2026-01-01\nstatus: open\n{padding}\n"
        'stub_id: "oversized-01"\n'
        "deployment_state: shipped\n",
    )

    index, scan_errors = _build_blocker_index(repo)

    assert scan_errors == []
    assert index.get("oversized-01") == [path]

    evidence = pa.compute_gate_blocker_evidence(repo, ["oversized-01"])
    assert evidence[0]["status"] == "resolved"
    assert evidence[0]["deployment_state"] == "shipped"


def test_unresolvable_id_names_nothing_in_the_corpus(tmp_path: Path):
    repo = tmp_path
    (repo / "state" / "handoffs").mkdir(parents=True)

    evidence = pa.compute_gate_blocker_evidence(repo, ["ghost-id-does-not-exist"])

    assert len(evidence) == 1
    assert evidence[0]["status"] == "unresolvable"
    assert evidence[0]["resolved"] is False
    assert evidence[0]["deployment_state"] is None


def test_ambiguous_when_more_than_one_head_survives_the_collapse(tmp_path: Path):
    repo = tmp_path
    # Two independent (non-chained) records sharing the same stub_id is a
    # genuine cross-family collision — `collapse_to_chain_heads` has nothing
    # to collapse (neither supersedes the other), so both survive as heads.
    _write_handoff(
        repo / "state" / "handoffs" / "dup-a.md",
        'title: "Dup A"\ncreated: 2026-01-01\nstatus: open\n'
        'stub_id: "dup-01"\ndeployment_state: open\n',
    )
    _write_handoff(
        repo / "state" / "handoffs" / "dup-b.md",
        'title: "Dup B"\ncreated: 2026-01-01\nstatus: open\n'
        'stub_id: "dup-01"\ndeployment_state: open\n',
    )

    index, _ = _build_blocker_index(repo)
    assert len(index.get("dup-01", [])) == 2

    evidence = pa.compute_gate_blocker_evidence(repo, ["dup-01"])
    assert evidence[0]["status"] == "ambiguous"
    assert evidence[0]["resolved"] is False


def test_empty_blocked_by_short_circuits_without_a_corpus_walk(tmp_path: Path):
    assert pa.compute_gate_blocker_evidence(tmp_path, None) == []
    assert pa.compute_gate_blocker_evidence(tmp_path, []) == []


def test_handoff_id_form_also_resolves(tmp_path: Path):
    repo = tmp_path
    path = _write_handoff(
        repo / "state" / "handoffs" / "durable.md",
        'title: "Durable"\ncreated: 2026-01-01\nstatus: open\n'
        'handoff_id: "hnd-durable-abc123"\ndeployment_state: in_flight\n'
        "claimed_by: session-xyz\n",
    )

    index, _ = _build_blocker_index(repo)
    assert index.get("hnd-durable-abc123") == [path]

    evidence = pa.compute_gate_blocker_evidence(repo, ["hnd-durable-abc123"])
    assert evidence[0]["status"] == "resolved"
    assert evidence[0]["deployment_state"] == "in_flight"
    assert evidence[0]["holder"] == "session-xyz"


BLOCKER_FM = (
    'title: "Blocker"\n'
    "created: 2026-01-01\n"
    "status: open\n"
    'stub_id: "sat-06"\n'
    "deployment_state: shipped\n"
)


def test_scalar_blocked_by_is_one_id_not_one_id_per_character(tmp_path: Path):
    # A `str` is iterable, so an unguarded loop resolves it per CHARACTER --
    # 'sat-06' becoming six blockers named 's','a','t','-','0','6', and a
    # recommendation naming characters. The schema declares a list but nothing
    # enforces that at read time, so this function is the place that must not
    # trust it. Found by the criterion-only reader at 6f146d4d06.
    repo = tmp_path
    _write_handoff(repo / "state" / "handoffs" / "sat-06.md", BLOCKER_FM)

    evidence = pa.compute_gate_blocker_evidence(repo, "sat-06")

    assert len(evidence) == 1, f"scalar iterated per character: {evidence!r}"
    assert evidence[0]["id"] == "sat-06"
    assert evidence[0]["status"] == "resolved"
    assert evidence[0]["deployment_state"] == "shipped"


def test_scan_incomplete_when_a_corpus_root_is_unreadable(tmp_path: Path, monkeypatch):
    # Directory-level unreadable subtree — `collect_live_handoff_paths`
    # raising `OSError` is the shape `_build_blocker_index` already
    # catches and threads into `scan_errors`. Monkeypatched rather than
    # `os.chmod`-based: `os.chmod` does not remove read access on Windows
    # (this repo runs Windows as a first-class host), which would silently
    # no-op the permission trick and pass for the wrong reason (Review:
    # code-reviewer — Finding 2).
    import coordinator_core.reconcile.handoff_corpus as hc

    repo = tmp_path
    (repo / "state" / "handoffs").mkdir(parents=True)

    def _raise(_root):
        raise OSError("simulated: permission denied")

    monkeypatch.setattr(hc, "collect_live_handoff_paths", _raise)

    index, scan_errors = hc._build_blocker_index(repo)
    assert scan_errors != []
    assert index == {}

    evidence = pa.compute_gate_blocker_evidence(repo, ["some-id"])
    assert evidence[0]["status"] == "scan_incomplete"
    assert evidence[0]["resolved"] is False


def test_scan_incomplete_when_a_single_record_file_is_unreadable(tmp_path: Path, monkeypatch):
    # File-granularity unreadable record — a directory-level walk finds
    # `path`, but reading it fails (locked/permission-denied). This must
    # come back `scan_incomplete`, never `unresolvable` — the exact gap
    # Finding 1 closed (`_frontmatter_head_bytes` now returns `None` on a
    # read failure instead of silently treating the file as carrying no
    # id). Monkeypatched at `_frontmatter_head_bytes` itself rather than
    # `os.chmod`, for the same Windows reason as the test above.
    import coordinator_core.reconcile.handoff_corpus as hc

    repo = tmp_path
    path = _write_handoff(repo / "state" / "handoffs" / "locked.md", BLOCKER_FM)

    real_head_bytes = hc._frontmatter_head_bytes

    def _flaky_head_bytes(p: Path):
        if p == path:
            return None
        return real_head_bytes(p)

    monkeypatch.setattr(hc, "_frontmatter_head_bytes", _flaky_head_bytes)

    index, scan_errors = hc._build_blocker_index(repo)
    assert scan_errors != []
    assert index.get("sat-06") is None  # the id never made it into the index

    evidence = pa.compute_gate_blocker_evidence(repo, ["sat-06"])
    assert evidence[0]["status"] == "scan_incomplete"
    assert evidence[0]["resolved"] is False


def test_scan_incomplete_when_the_indexed_file_fails_at_resolve_time(tmp_path: Path, monkeypatch):
    # The id resolves through the (successfully-built) index — some other
    # unrelated subtree failed to scan — but the specific file this id
    # names has become unreadable by the time `compute_gate_blocker_
    # evidence` re-reads it. `dag._read_meta` swallows the `OSError`
    # itself (`except Exception: return {}`), so this call site re-probes
    # with its own direct read to tell "unreadable" apart from "malformed
    # YAML" (Review: code-reviewer — Finding 1) — this must land
    # `scan_incomplete`, never `unresolvable`.
    import coordinator_core.pickup_assemble as pa_mod

    repo = tmp_path
    path = _write_handoff(repo / "state" / "handoffs" / "sat-06.md", BLOCKER_FM)

    real_read_meta = pa_mod.dag._read_meta

    def _flaky_read_meta(file_path: str):
        if Path(file_path) == path.resolve():
            return {}
        return real_read_meta(file_path)

    monkeypatch.setattr(pa_mod.dag, "_read_meta", _flaky_read_meta)

    real_read_bytes = Path.read_bytes

    def _flaky_read_bytes(self: Path):
        if self == path:
            raise OSError("simulated: permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _flaky_read_bytes)

    evidence = pa.compute_gate_blocker_evidence(repo, ["sat-06"])
    assert evidence[0]["status"] == "scan_incomplete"
    assert evidence[0]["resolved"] is False


def test_unparsed_flow_list_text_is_malformed_not_a_bracketed_id(tmp_path: Path):
    # The Anti-scope case: `roadmap_link_stubs._as_list` would return
    # `['[sat-06]']` here, inventing an id nothing can ever match and reporting
    # it as an ordinary missing blocker. Text that still looks like an unparsed
    # list is malformed input and says so, rather than resolving a bracket.
    repo = tmp_path
    _write_handoff(repo / "state" / "handoffs" / "sat-06.md", BLOCKER_FM)

    evidence = pa.compute_gate_blocker_evidence(repo, "[sat-06]")

    assert len(evidence) == 1
    assert evidence[0]["id"] == "[sat-06]"
    assert evidence[0]["status"] == "unresolvable"
    assert evidence[0]["resolved"] is False
