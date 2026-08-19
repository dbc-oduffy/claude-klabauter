"""Contract test for `coordinator_core.session.session_facts` — DR-319's return-shape
contract (docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md).

This is what discharges AC3, AC6, and AC8
(docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md § C2): a TEST that the
posture holds, not a document describing it (the Staff Engineer F9/F10).

This is also the authoritative home for DR-323 § (c)'s per-fact AC9 contract (fl-core-02
C6): `_FACT_VALUE_KEY_DECLARATION` below plus `TestPerFactRequiredFieldDeclaration`, which
calls each served facade function and asserts its returned record against that
declaration. `coordinator_core/fact_contract_gate/tests/` asserts only
`producer_inventory`'s unchanged repo-wide union — it does not duplicate this.

Git failure is simulated by monkeypatching the producer seam
(`branch_resolution._git_run`), not by requiring a broken repo — a broken git repo is a
flaky, environment-dependent way to exercise a code path that is really "the subprocess
call returned nonzero," which is trivially and deterministically faked.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import branch_resolution
from coordinator_core.session import claimed_plan, session_facts

_SID = "sess-c2-contract-test-001"
_SID_PICKUP = "sess-c2-pickup-kind-test-001"
_SID_GOVERNING_PLAN = "sess-c7b-governing-plan-test-001"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _session_shape_path(common_dir: Path, sid: str) -> Path:
    return common_dir / "coordinator-sessions" / sid / "session-shape.json"


def _fake_result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)



def _fake_commits(count: int) -> list[dict]:
    """`session.commits` primitive rows — only the numstat fields the derived
    producers read. Stubbed at the primitive rather than at `_git_run` because
    `session_commit_count_attributed` derives from the primitive (its C5
    migration), so a `_git_run` stub no longer reaches it."""
    return [{"sha": f"sha{i:03d}", "added": 1, "deleted": 0} for i in range(count)]

def test_computed_record_is_distinguishable_from_a_degraded_record(tmp_path, monkeypatch):
    """The whole point of the posture: a degraded read and a genuinely-zero read must
    not collapse into the same value at the call site."""
    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: _fake_commits(0),
    )
    zero_record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: None,
    )
    degraded_record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    assert zero_record["degraded"] is False
    assert zero_record["value"] == 0
    assert degraded_record["degraded"] is True
    assert "value" not in degraded_record

    # The distinguishing property under test: these are not the same record, and a
    # caller cannot mistake one for the other by reading `.get("value")` alone.
    assert zero_record != degraded_record
    assert degraded_record.get("value") is None


def test_computed_record_carries_every_required_key_never_a_fabricated_default(tmp_path, monkeypatch):
    """A silently-missing field fails loudly: assert every DR-319 key is actually
    present, not merely that `.get()` returns something plausible."""
    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: _fake_commits(2),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    for key in ("degraded", "value", "source", "collision"):
        assert key in record, f"computed record missing required key {key!r}"
    assert record["degraded"] is False
    assert record["value"] == 2
    assert isinstance(record["source"], str) and record["source"]
    # collision is ALWAYS present on a computed record (R-11) — None here because this
    # fact has no peer-mutable surface, never coerced to False.
    assert record["collision"] is None


def test_collision_key_is_present_unconditionally_on_a_computed_record(tmp_path, monkeypatch):
    """`"collision" in record` must hold unconditionally on a computed record — an
    omitted key declares nothing, which is the absent-vs-clean conflation R-11
    forbids."""
    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: _fake_commits(0),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert "collision" in record
    assert record["collision"] is None
    # Never coerced to False (R-11 negative-spec) — a bare truthiness/`is False` check
    # would wrongly treat "no collision mode exists" as "checked, clean."
    assert record["collision"] is not False


def test_degraded_record_carries_evidence_and_source_never_a_value_key(tmp_path, monkeypatch):
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(1, stderr="git log failed"),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    assert record["degraded"] is True
    assert "value" not in record
    assert isinstance(record["evidence"], str) and record["evidence"]
    assert isinstance(record["source"], str) and record["source"]
    assert set(record) == {"degraded", "evidence", "source"}


def test_served_fact_carries_no_verdict_field(tmp_path, monkeypatch):
    """AC8, the detect/decide split: neither shape may carry a verdict, recommendation,
    disposition, or action key — this facade emits evidence and collision state, never
    a decision replacing an EM judgment."""
    forbidden = {"verdict", "recommendation", "disposition", "action"}

    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: _fake_commits(1),
    )
    computed = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert forbidden.isdisjoint(computed), f"computed record leaked a verdict key: {set(computed) & forbidden}"

    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: None,
    )
    degraded = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert degraded["degraded"] is True, "fixture no longer produces a degraded record"
    assert forbidden.isdisjoint(degraded), f"degraded record leaked a verdict key: {set(degraded) & forbidden}"


def test_computed_shape_is_exactly_the_dr319_key_set(tmp_path, monkeypatch):
    """No shape other than DR-319's two is legal, regardless of internal consistency."""
    monkeypatch.setattr(
        branch_resolution,
        "_cached_session_commits",
        lambda root, sid: _fake_commits(1),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert set(record) == {"degraded", "value", "source", "collision"}


# ---------------------------------------------------------------------------
# session_pickup_kind (Fact 1) — DR-323's lift of
# quick_wrap_assemble._read_pickup_kind, with the posture conversion and the AC6
# mixed handoff-plus-memo case as the load-bearing new behaviour.
# ---------------------------------------------------------------------------


def test_pickup_kind_reads_classification_from_artifact_frontmatter(tmp_path: Path):
    common = tmp_path / ".git"
    _write(
        _session_shape_path(common, _SID_PICKUP),
        json.dumps(
            {
                "session_id": _SID_PICKUP,
                "pickup": {"happened": True, "handoff": "b.md", "deliverable_id": "dlv-1"},
            }
        ),
    )
    _write(tmp_path / "state" / "handoffs" / "b.md", "---\nkind: spinoff\nstatus: claimed\n---\n")

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert record["degraded"] is False
    value = record["value"]
    assert value["classification"] == "spinoff"
    assert value["artifact_path"] == "state/handoffs/b.md"
    assert value["consumed_predecessor"] is True
    assert value["deliverable_id"] == "dlv-1"


def test_pickup_kind_is_none_for_a_session_that_picked_nothing_up(tmp_path: Path):
    """A session-shape.json that exists but records no pickup is a COMPUTED `none`,
    never degraded — the exact case AC3's conversion must not sweep in."""
    common = tmp_path / ".git"
    _write(_session_shape_path(common, _SID_PICKUP), json.dumps({"session_id": _SID_PICKUP}))

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert record["degraded"] is False
    assert record["value"]["classification"] == "none"
    assert record["value"]["consumed_predecessor"] is False


def test_pickup_kind_is_computed_none_when_session_shape_json_was_never_written(tmp_path: Path):
    """No session-shape.json at all (the file's own path absent, not merely unreadable)
    is the ordinary early-session case — computed, not degraded."""
    common = tmp_path / ".git"

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert record["degraded"] is False
    assert record["value"]["classification"] == "none"


def test_pickup_kind_degrades_when_session_shape_json_exists_but_cannot_be_parsed(tmp_path: Path):
    """AC3 — the posture conversion under test. `_read_session_shape` swallows a
    JSONDecodeError internally and reports source="absent", indistinguishable (to a
    naive caller) from a session that never wrote the file at all. This facade
    recovers that distinction: the file is present but unparsable, so this must be
    `degraded: True` with evidence naming the producer/call/path — never a fabricated
    `classification: "none"`."""
    common = tmp_path / ".git"
    _write(_session_shape_path(common, _SID_PICKUP), "{not valid json")

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert record["degraded"] is True
    assert "value" not in record
    assert "_read_session_shape" in record["evidence"]
    assert str(_session_shape_path(common, _SID_PICKUP)) in record["evidence"]
    assert record["source"] == session_facts._SOURCE_SESSION_PICKUP_KIND


def test_pickup_kind_degraded_and_computed_none_are_not_the_same_record(tmp_path: Path):
    """The whole point of AC3: a degraded read and a genuinely-empty computed read
    must not collapse into the same value at the call site."""
    common = tmp_path / ".git"
    computed = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    _write(_session_shape_path(common, _SID_PICKUP), "{not valid json")
    degraded = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert computed != degraded
    assert computed["degraded"] is False
    assert degraded["degraded"] is True
    assert degraded.get("value") is None


def test_pickup_kind_ac6_mixed_handoff_and_memo_carries_both_axes_with_stated_precedence(
    tmp_path: Path,
):
    """AC6, corrected reading: a session that both consumed a handoff AND actioned
    memos is not data loss. `classification` stays single-valued and picks the
    handoff; `actioned_memos` still travels in `value` regardless."""
    common = tmp_path / ".git"
    _write(
        _session_shape_path(common, _SID_PICKUP),
        json.dumps(
            {
                "session_id": _SID_PICKUP,
                "pickup": {"happened": True, "handoff": "b.md"},
                "actioned_memos": [{"basename": "m.md", "decision": "accepted"}],
            }
        ),
    )
    _write(tmp_path / "state" / "handoffs" / "b.md", "---\nkind: handoff\n---\n")

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    value = record["value"]
    # Precedence: classification picks the handoff over the memos.
    assert value["classification"] == "handoff"
    # Not lost: the memo axis still travels in `value` alongside the handoff pick.
    assert value["actioned_memos"] == [{"basename": "m.md", "decision": "accepted"}]
    assert value["consumed_predecessor"] is True


def test_pickup_kind_classifies_an_actioned_memo_session_as_memo_when_no_handoff_happened(
    tmp_path: Path,
):
    common = tmp_path / ".git"
    _write(
        _session_shape_path(common, _SID_PICKUP),
        json.dumps(
            {
                "session_id": _SID_PICKUP,
                "pickup": {"happened": False},
                "actioned_memos": [{"basename": "m.md", "decision": "accepted"}],
            }
        ),
    )

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert record["value"]["classification"] == "memo"
    assert record["value"]["consumed_predecessor"] is True


def test_pickup_kind_collision_is_always_none_no_peer_mutable_surface(tmp_path: Path):
    """DR-323 § (b): Fact 1 is session-scoped, single-writer — no collision mode
    exists, so `collision` is `None`, never omitted, never coerced to `False`."""
    common = tmp_path / ".git"
    _write(_session_shape_path(common, _SID_PICKUP), json.dumps({"session_id": _SID_PICKUP}))

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert "collision" in record
    assert record["collision"] is None
    assert record["collision"] is not False


def test_pickup_kind_computed_shape_is_exactly_the_dr319_key_set(tmp_path: Path):
    common = tmp_path / ".git"
    _write(_session_shape_path(common, _SID_PICKUP), json.dumps({"session_id": _SID_PICKUP}))

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert set(record) == {"degraded", "value", "source", "collision"}


def test_pickup_kind_degraded_shape_is_exactly_the_dr319_key_set(tmp_path: Path):
    common = tmp_path / ".git"
    _write(_session_shape_path(common, _SID_PICKUP), "{not valid json")

    record = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)

    assert set(record) == {"degraded", "evidence", "source"}


def test_pickup_kind_carries_no_verdict_field_on_either_shape(tmp_path: Path):
    forbidden = {"verdict", "recommendation", "disposition", "action"}
    common = tmp_path / ".git"

    _write(_session_shape_path(common, _SID_PICKUP), json.dumps({"session_id": _SID_PICKUP}))
    computed = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)
    assert forbidden.isdisjoint(computed)
    assert forbidden.isdisjoint(computed["value"])

    _write(_session_shape_path(common, _SID_PICKUP), "{not valid json")
    degraded = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)
    assert forbidden.isdisjoint(degraded)


def test_pickup_kind_source_string_resolves_to_a_symbol_that_exists(tmp_path: Path):
    """AC4 / AC9: the backing-source string is not a stale or copy-pasted label — it
    names a producer symbol that actually exists in this repo."""
    module_path, _, symbol = session_facts._SOURCE_SESSION_PICKUP_KIND.partition("::")
    assert module_path == "coordinator_core/ops/ceremony/branch_resolution.py"
    assert hasattr(branch_resolution, symbol)


# ---------------------------------------------------------------------------
# session_diff_brightline (Fact 3) — DR-323's lift of
# quick_wrap_assemble._read_diff / _novel_loc_split. The named fail-open under test:
# the old call site folded a degraded commit-count read into `commit_count = 0`,
# indistinguishable from a genuinely zero-commit session.
# ---------------------------------------------------------------------------

_SID_DIFF = "sess-c3-diff-brightline-test-001"


def _fake_split(**overrides) -> dict:
    base = {
        "degraded": False,
        "gross_loc": 15,
        "doc_only_loc": 3,
        "relocated_files": 0,
        "novel_loc": 12,
        "novel_commit_count": 1,
    }
    base.update(overrides)
    return base


def test_diff_brightline_degrades_when_commit_count_record_is_degraded(tmp_path, monkeypatch):
    """The fail-open this chunk retires: a degraded `session_commit_count_attributed`
    read must propagate as a degraded FACT, never fold into `commit_count: 0` inside an
    otherwise-computed record."""
    monkeypatch.setattr(
        session_facts,
        "session_commit_count_attributed",
        lambda worktree_root, sid: {"degraded": True, "evidence": "git log failed: boom"},
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)

    assert record["degraded"] is True
    assert record["evidence"] == "git log failed: boom"
    assert record["source"] == session_facts._SOURCE_SESSION_DIFF_BRIGHTLINE
    assert "value" not in record
    assert set(record) == {"degraded", "evidence", "source"}


def test_diff_brightline_degrades_when_novel_loc_split_git_call_fails(tmp_path, monkeypatch):
    """`_novel_loc_split`'s own `git log --numstat` call is a git-backed sub-read this
    chunk owns — its failure must degrade the fact too, not silently zero the split."""
    monkeypatch.setattr(
        session_facts,
        "session_commit_count_attributed",
        lambda worktree_root, sid: {"degraded": False, "value": 0},
    )
    monkeypatch.setattr(
        session_facts,
        "_novel_loc_split",
        lambda worktree_root, sid: {"degraded": True, "evidence": "numstat call failed: boom"},
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)

    assert record["degraded"] is True
    assert record["evidence"] == "numstat call failed: boom"
    assert record["source"] == session_facts._SOURCE_SESSION_DIFF_BRIGHTLINE
    assert "value" not in record
    assert set(record) == {"degraded", "evidence", "source"}


def test_novel_loc_split_degrades_when_the_underlying_git_log_fails(tmp_path, monkeypatch):
    """Direct coverage of the private helper's own posture: `branch_resolution._git_run`
    (never `quick_wrap_assemble._git_out`, which swallows failure into `""`)."""
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="fatal: not a git repository"),
    )
    result = session_facts._novel_loc_split(tmp_path, _SID_DIFF)
    assert result["degraded"] is True
    assert "128" in result["evidence"] or "not a git repository" in result["evidence"]


def test_novel_loc_split_computes_the_gross_doc_only_novel_split(tmp_path, monkeypatch):
    """Direct coverage of the parsing logic moved wholesale from
    `quick_wrap_assemble._novel_loc_split` — unchanged behaviour, new posture."""
    numstat_out = (
        "@@QWA-COMMIT@@ abc123\n"
        "10\t2\tcoordinator_core/foo.py\n"
        "3\t0\tdocs/readme.md\n"
    )
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=numstat_out),
    )
    result = session_facts._novel_loc_split(tmp_path, _SID_DIFF)
    assert result == {
        "degraded": False,
        "gross_loc": 15,
        "doc_only_loc": 3,
        "relocated_files": 0,
        "novel_loc": 12,
        "novel_commit_count": 1,
    }


def _stub_diff_brightline_composition(monkeypatch, *, method, commit_count=3, split=None):
    monkeypatch.setattr(
        session_facts,
        "session_commit_count_attributed",
        lambda worktree_root, sid: {"degraded": False, "value": commit_count},
    )
    monkeypatch.setattr(
        session_facts,
        "_novel_loc_split",
        lambda worktree_root, sid: split or _fake_split(),
    )
    monkeypatch.setattr(
        session_facts,
        "analyze_session_scoping",
        lambda worktree_root, common_dir, sid: branch_resolution.ScopingVerdict(
            method=method, foreign_count=0, contiguous=True, candidate_range=""
        ),
    )
    monkeypatch.setattr(
        session_facts,
        "_session_touched_paths",
        lambda worktree_root, sid: ["coordinator_core/foo.py", "docs/readme.md"],
    )


def test_diff_brightline_computed_shape_is_exactly_the_dr319_key_set(tmp_path, monkeypatch):
    _stub_diff_brightline_composition(
        monkeypatch, method=branch_resolution.SCOPING_METHOD_TRAILER
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)

    assert set(record) == {"degraded", "value", "source", "collision"}
    assert record["degraded"] is False
    assert record["source"] == session_facts._SOURCE_SESSION_DIFF_BRIGHTLINE
    assert record["collision"] is None
    assert record["value"] == {
        "scoping_method": branch_resolution.SCOPING_METHOD_TRAILER,
        "trustworthy": True,
        "sha_range": None,
        "commit_count": 3,
        "surface_count": 2,
        "novel_surface_count": 1,
        "touched_paths": ["coordinator_core/foo.py", "docs/readme.md"],
        "gross_loc": 15,
        "doc_only_loc": 3,
        "relocated_files": 0,
        "novel_loc": 12,
        "novel_commit_count": 1,
        "brightline": {
            "novel_loc": branch_resolution._BRIGHTLINE_LOC,
            "novel_commit_count": branch_resolution._BRIGHTLINE_COMMITS,
            "novel_surface_count": branch_resolution._BRIGHTLINE_SURFACES,
        },
        "breached": [],
        "under_brightline": True,
    }


def test_diff_brightline_trustworthy_is_false_only_on_ambiguous_scoping_and_degraded_stays_false(
    tmp_path, monkeypatch
):
    """KEEP `trustworthy` DISTINCT FROM `degraded`: an ambiguous scoping method is a
    data-quality signal about the trailer, not a probe failure. The record is still
    computed, still carries real counts."""
    _stub_diff_brightline_composition(
        monkeypatch, method=branch_resolution.SCOPING_METHOD_AMBIGUOUS
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)

    assert record["degraded"] is False
    assert record["value"]["trustworthy"] is False
    assert record["value"]["scoping_method"] == branch_resolution.SCOPING_METHOD_AMBIGUOUS


def test_diff_brightline_collision_is_always_none_no_peer_mutable_surface(tmp_path, monkeypatch):
    """DR-323 § (b): trailer attribution is per-sid, so no peer mutation can change
    what this fact reports for a given sid — `collision` is `None`, never omitted,
    never coerced to `False`."""
    _stub_diff_brightline_composition(
        monkeypatch, method=branch_resolution.SCOPING_METHOD_TRAILER
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)

    assert "collision" in record
    assert record["collision"] is None
    assert record["collision"] is not False


def test_diff_brightline_breached_reflects_brightline_thresholds(tmp_path, monkeypatch):
    over_threshold_split = _fake_split(
        novel_loc=branch_resolution._BRIGHTLINE_LOC,
        novel_commit_count=branch_resolution._BRIGHTLINE_COMMITS,
    )
    _stub_diff_brightline_composition(
        monkeypatch,
        method=branch_resolution.SCOPING_METHOD_TRAILER,
        split=over_threshold_split,
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)

    assert record["value"]["breached"] == ["novel_commit_count", "novel_loc"]
    assert record["value"]["under_brightline"] is False


def test_diff_brightline_carries_no_verdict_field_on_either_shape(tmp_path, monkeypatch):
    forbidden = {"verdict", "recommendation", "disposition", "action"}

    _stub_diff_brightline_composition(
        monkeypatch, method=branch_resolution.SCOPING_METHOD_TRAILER
    )
    computed = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)
    assert forbidden.isdisjoint(computed)
    assert forbidden.isdisjoint(computed["value"])

    monkeypatch.setattr(
        session_facts,
        "session_commit_count_attributed",
        lambda worktree_root, sid: {"degraded": True, "evidence": "boom"},
    )
    degraded = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)
    assert forbidden.isdisjoint(degraded)


def test_diff_brightline_degraded_shape_is_exactly_the_dr319_key_set(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_facts,
        "session_commit_count_attributed",
        lambda worktree_root, sid: {"degraded": True, "evidence": "boom"},
    )
    record = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)
    assert set(record) == {"degraded", "evidence", "source"}


def test_diff_brightline_source_string_resolves_to_a_symbol_that_exists(tmp_path):
    """AC4 / AC9: the backing-source string names a producer symbol that actually
    exists in this repo — DR-323 § (b)'s table description for Fact 3."""
    module_path, _, symbol = session_facts._SOURCE_SESSION_DIFF_BRIGHTLINE.partition("::")
    assert module_path == "coordinator_core/ops/ceremony/branch_resolution.py"
    assert hasattr(branch_resolution, symbol)


# ---------------------------------------------------------------------------
# session_terminal_sizings (Fact 4) — DR-323's lift of
# quick_wrap_assemble._read_terminal_sizings / _dirty_paths. THE COLLISION
# REFERENCE IMPLEMENTATION: collision is the OR across per-record `dirty` flags,
# and per-record granularity must survive inside `value["terminal"]`.
# ---------------------------------------------------------------------------


def _write_sizing(sizings_dir: Path, name: str, status: str) -> Path:
    return _write(
        sizings_dir / name,
        f"---\nstatus: {status}\n---\nbody\n",
    )


def test_terminal_sizings_is_computed_empty_when_sizings_dir_is_absent(tmp_path: Path):
    """An absent `state/sizings/` directory is a COMPUTED empty scan, never degraded —
    the AC3 split this chunk draws between 'nothing to scan' and 'scan could not run'."""
    record = session_facts.session_terminal_sizings(tmp_path)

    assert record["degraded"] is False
    assert record["value"] == {"scanned": 0, "terminal": [], "non_terminal_count": 0}
    assert record["collision"] is False
    assert record["source"] == session_facts._SOURCE_SESSION_TERMINAL_SIZINGS


def test_terminal_sizings_degrades_when_the_glob_raises_oserror(tmp_path: Path, monkeypatch):
    """A missing directory and a mid-glob `OSError` currently collapse into the same
    empty result upstream — this chunk splits them. An `OSError` is degraded."""
    sizings_dir = tmp_path / "state" / "sizings"
    sizings_dir.mkdir(parents=True)

    def _raise(self, pattern):
        raise OSError("boom")

    monkeypatch.setattr(Path, "glob", _raise)
    record = session_facts.session_terminal_sizings(tmp_path)

    assert record["degraded"] is True
    assert "value" not in record
    assert "boom" in record["evidence"]
    assert record["source"] == session_facts._SOURCE_SESSION_TERMINAL_SIZINGS
    assert set(record) == {"degraded", "evidence", "source"}


def test_terminal_sizings_degrades_when_dirty_paths_git_status_fails(tmp_path: Path, monkeypatch):
    """`_dirty_paths`'s own `git status` call is a sub-read this chunk owns — its
    failure must degrade the fact, not silently report every record clean."""
    sizings_dir = tmp_path / "state" / "sizings"
    _write_sizing(sizings_dir, "a.yaml", "shipped")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="fatal: not a git repository"),
    )

    record = session_facts.session_terminal_sizings(tmp_path)

    assert record["degraded"] is True
    assert "git status" in record["evidence"]
    assert set(record) == {"degraded", "evidence", "source"}


def test_dirty_paths_degrades_when_git_status_fails(tmp_path: Path, monkeypatch):
    """Direct coverage of the private helper's own posture: `branch_resolution._git_run`
    (never `quick_wrap_assemble._git_out`, which swallows failure into `""`
    indistinguishable from a genuinely clean tree)."""
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(1, stderr="boom"),
    )
    result = session_facts._dirty_paths(tmp_path)
    assert result["degraded"] is True
    assert "boom" in result["evidence"]


def test_dirty_paths_parses_porcelain_status_and_rename_arrow(tmp_path: Path, monkeypatch):
    """Direct coverage of the parsing logic moved wholesale from
    `quick_wrap_assemble._dirty_paths` — unchanged behaviour, new posture."""
    porcelain = ' M state/sizings/a.yaml\nR  old.yaml -> state/sizings/b.yaml\n'
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=porcelain),
    )
    result = session_facts._dirty_paths(tmp_path)
    assert result == {
        "degraded": False,
        "paths": {"state/sizings/a.yaml", "state/sizings/b.yaml"},
    }


def test_terminal_sizings_reports_dirty_and_clean_records_with_per_record_granularity(
    tmp_path: Path, monkeypatch
):
    """The load-bearing assertion: `collision` is the record-level OR, but each
    `terminal` entry keeps its OWN `dirty` bool — the EM's skip-vs-sweep call is per
    record and must not be flattened into the aggregate."""
    sizings_dir = tmp_path / "state" / "sizings"
    _write_sizing(sizings_dir, "clean.yaml", "shipped")
    _write_sizing(sizings_dir, "dirty.yaml", "declined")
    _write_sizing(sizings_dir, "draft.yaml", "sized")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(
            0, stdout=" M state/sizings/dirty.yaml\n"
        ),
    )

    record = session_facts.session_terminal_sizings(tmp_path)

    assert record["degraded"] is False
    assert record["value"]["scanned"] == 3
    assert record["value"]["non_terminal_count"] == 1
    by_path = {e["path"]: e for e in record["value"]["terminal"]}
    assert by_path["state/sizings/clean.yaml"]["dirty"] is False
    assert by_path["state/sizings/clean.yaml"]["reason"] is None
    assert by_path["state/sizings/dirty.yaml"]["dirty"] is True
    assert by_path["state/sizings/dirty.yaml"]["reason"] is not None
    # Record-level collision is the OR across the two — True because ONE record is
    # dirty, even though the other is clean; the aggregate does not erase the split.
    assert record["collision"] is True


def test_terminal_sizings_collision_is_false_when_no_terminal_record_is_dirty(
    tmp_path: Path, monkeypatch
):
    sizings_dir = tmp_path / "state" / "sizings"
    _write_sizing(sizings_dir, "clean.yaml", "shipped")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )

    record = session_facts.session_terminal_sizings(tmp_path)

    assert record["collision"] is False
    assert record["value"]["terminal"][0]["dirty"] is False


def test_terminal_sizings_drops_movable_key(tmp_path: Path, monkeypatch):
    """DR-323 § (b): `movable` is dropped — a pure restatement of `dirty` and an
    EM disposition key, DR-319's Negative-spec on AC12. Not re-scrutinized here."""
    sizings_dir = tmp_path / "state" / "sizings"
    _write_sizing(sizings_dir, "a.yaml", "shipped")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )

    record = session_facts.session_terminal_sizings(tmp_path)

    entry = record["value"]["terminal"][0]
    assert set(entry) == {"path", "status", "dirty", "reason"}
    assert "movable" not in entry


def test_terminal_sizings_computed_shape_is_exactly_the_dr319_key_set(
    tmp_path: Path, monkeypatch
):
    sizings_dir = tmp_path / "state" / "sizings"
    _write_sizing(sizings_dir, "a.yaml", "shipped")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )

    record = session_facts.session_terminal_sizings(tmp_path)
    assert set(record) == {"degraded", "value", "source", "collision"}


def test_terminal_sizings_carries_no_verdict_field_on_either_shape(
    tmp_path: Path, monkeypatch
):
    forbidden = {"verdict", "recommendation", "disposition", "action"}
    sizings_dir = tmp_path / "state" / "sizings"
    _write_sizing(sizings_dir, "a.yaml", "shipped")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )

    computed = session_facts.session_terminal_sizings(tmp_path)
    assert forbidden.isdisjoint(computed)
    assert forbidden.isdisjoint(computed["value"])
    for entry in computed["value"]["terminal"]:
        assert forbidden.isdisjoint(entry)
        assert "movable" not in entry

    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(1, stderr="boom"),
    )
    degraded = session_facts.session_terminal_sizings(tmp_path)
    assert forbidden.isdisjoint(degraded)


def test_terminal_sizings_source_string_resolves_to_a_symbol_that_exists(tmp_path: Path):
    """AC4 / AC9: unlike Facts 1/3 (which point at `branch_resolution`), Fact 4's
    scan logic is moved wholesale into this module, so its source string
    self-references `session_facts` — a later chunk (C6) asserts this resolves."""
    module_path, _, symbol = session_facts._SOURCE_SESSION_TERMINAL_SIZINGS.partition("::")
    assert module_path == "coordinator_core/session/session_facts.py"
    assert hasattr(session_facts, symbol)


# ---------------------------------------------------------------------------
# session_fold_sidecars (Fact 5) — DR-323's lift of
# quick_wrap_assemble._read_fold_sidecars. AC3 is the load-bearing behaviour:
# a root that raises OSError must degrade the fact, never silently continue
# into a `present: False` indistinguishable from a clean-and-empty scan.
# ---------------------------------------------------------------------------


def _write_sidecar(root: Path, name: str) -> Path:
    return _write(root / name, "{}")


def test_fold_sidecars_is_computed_empty_when_neither_root_exists(tmp_path: Path):
    """Neither `state/execution-records/` nor `state/fold-execution-records/`
    existing is a COMPUTED empty scan, never degraded — same 'nothing to scan'
    posture `session_terminal_sizings` carries for an absent `state/sizings/`."""
    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["degraded"] is False
    assert record["value"] == {"present": False, "paths": [], "count": 0}
    assert record["collision"] is False
    assert record["source"] == session_facts._SOURCE_SESSION_FOLD_SIDECARS


def test_fold_sidecars_finds_json_under_either_root(tmp_path: Path, monkeypatch):
    exec_root = tmp_path / "state" / "execution-records"
    fold_root = tmp_path / "state" / "fold-execution-records"
    _write_sidecar(exec_root, "a.json")
    _write_sidecar(fold_root, "b.json")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )

    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["degraded"] is False
    assert record["value"]["present"] is True
    assert record["value"]["count"] == 2
    assert set(record["value"]["paths"]) == {
        "state/execution-records/a.json",
        "state/fold-execution-records/b.json",
    }


def test_fold_sidecars_degrades_when_one_root_raises_oserror_and_names_which(
    tmp_path: Path, monkeypatch
):
    """AC3, the posture conversion under test: the OLD reader swallowed `OSError`
    per-root and continued, so a partially-failed scan reported the same
    `present: False` as a clean scan finding nothing. This must degrade instead,
    with evidence naming WHICH root raised — 'one of two roots was unreadable'
    is a different fact from 'neither was'."""
    exec_root = tmp_path / "state" / "execution-records"
    fold_root = tmp_path / "state" / "fold-execution-records"
    _write_sidecar(fold_root, "clean.json")
    exec_root.mkdir(parents=True)

    real_rglob = Path.rglob

    def _maybe_raise(self, pattern):
        if self == exec_root:
            raise OSError("boom")
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", _maybe_raise)

    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["degraded"] is True
    assert "value" not in record
    assert "state/execution-records" in record["evidence"]
    assert "boom" in record["evidence"]
    # The OTHER root's own name must not appear as a failure — only the one that
    # actually raised is named as having failed.
    assert "state/fold-execution-records raised" not in record["evidence"]
    assert record["source"] == session_facts._SOURCE_SESSION_FOLD_SIDECARS
    assert set(record) == {"degraded", "evidence", "source"}


def test_fold_sidecars_degrades_naming_both_roots_when_both_raise(tmp_path: Path, monkeypatch):
    """Every existing root is attempted before returning — both failures are named
    in the evidence, not just the first one encountered."""
    exec_root = tmp_path / "state" / "execution-records"
    fold_root = tmp_path / "state" / "fold-execution-records"
    exec_root.mkdir(parents=True)
    fold_root.mkdir(parents=True)

    def _raise(self, pattern):
        raise OSError("boom")

    monkeypatch.setattr(Path, "rglob", _raise)

    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["degraded"] is True
    assert "state/execution-records" in record["evidence"]
    assert "state/fold-execution-records" in record["evidence"]


def test_fold_sidecars_degrades_when_dirty_paths_git_status_fails(tmp_path: Path, monkeypatch):
    """`_dirty_paths`'s own `git status` call is a sub-read this fact owns for its
    collision determination — its failure must degrade the fact, not silently
    report every sidecar clean."""
    _write_sidecar(tmp_path / "state" / "execution-records", "a.json")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="fatal: not a git repository"),
    )

    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["degraded"] is True
    assert "git status" in record["evidence"]
    assert set(record) == {"degraded", "evidence", "source"}


def test_fold_sidecars_collision_is_true_when_a_found_sidecar_is_uncommitted(
    tmp_path: Path, monkeypatch
):
    """A peer session mid-write shows up as an uncommitted (dirty) sidecar path —
    `collision` folds that in as `True`."""
    _write_sidecar(tmp_path / "state" / "execution-records", "a.json")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(
            0, stdout=" M state/execution-records/a.json\n"
        ),
    )

    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["degraded"] is False
    assert record["collision"] is True


def test_fold_sidecars_collision_is_false_when_no_found_sidecar_is_dirty(
    tmp_path: Path, monkeypatch
):
    _write_sidecar(tmp_path / "state" / "execution-records", "a.json")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )

    record = session_facts.session_fold_sidecars(tmp_path)

    assert record["collision"] is False


def test_fold_sidecars_computed_shape_is_exactly_the_dr319_key_set(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )
    record = session_facts.session_fold_sidecars(tmp_path)
    assert set(record) == {"degraded", "value", "source", "collision"}


def test_fold_sidecars_degraded_shape_is_exactly_the_dr319_key_set(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="boom"),
    )
    _write_sidecar(tmp_path / "state" / "execution-records", "a.json")

    record = session_facts.session_fold_sidecars(tmp_path)
    assert set(record) == {"degraded", "evidence", "source"}


def test_fold_sidecars_carries_no_verdict_field_on_either_shape(
    tmp_path: Path, monkeypatch
):
    forbidden = {"verdict", "recommendation", "disposition", "action"}
    _write_sidecar(tmp_path / "state" / "execution-records", "a.json")

    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )
    computed = session_facts.session_fold_sidecars(tmp_path)
    assert forbidden.isdisjoint(computed)
    assert forbidden.isdisjoint(computed["value"])

    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="boom"),
    )
    degraded = session_facts.session_fold_sidecars(tmp_path)
    assert forbidden.isdisjoint(degraded)


def test_fold_sidecars_source_string_resolves_to_a_symbol_that_exists(tmp_path: Path):
    """AC4 / AC9: like Fact 4, Fact 5's scan logic is moved wholesale into this
    module, so its source string self-references `session_facts`."""
    module_path, _, symbol = session_facts._SOURCE_SESSION_FOLD_SIDECARS.partition("::")
    assert module_path == "coordinator_core/session/session_facts.py"
    assert hasattr(session_facts, symbol)


def test_scan_fold_sidecar_roots_degrades_when_an_existing_root_raises(
    tmp_path: Path, monkeypatch
):
    """Direct coverage of the private helper's own posture."""
    exec_root = tmp_path / "state" / "execution-records"
    exec_root.mkdir(parents=True)
    real_rglob = Path.rglob

    def _raise(self, pattern, *args, **kwargs):
        if self == exec_root:
            raise OSError("permission denied")
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", _raise)

    result = session_facts._scan_fold_sidecar_roots(tmp_path)

    assert result["degraded"] is True
    assert "permission denied" in result["evidence"]


def test_scan_fold_sidecar_roots_computes_the_listing_when_both_roots_are_readable(
    tmp_path: Path,
):
    _write_sidecar(tmp_path / "state" / "execution-records", "a.json")
    result = session_facts._scan_fold_sidecar_roots(tmp_path)
    assert result == {"degraded": False, "paths": ["state/execution-records/a.json"]}


# ---------------------------------------------------------------------------
# session_governing_plan (fl-core-02 C7b) — lift of
# quick_wrap_assemble._read_governing_plan. The lift only, no vocabulary
# question in it (DR-323 body). The named fail-open under test: the old bare
# `except Exception` around the resolver import/call returned the `absent`
# literal, conflating "no plan claimed" with "the resolver blew up" in the
# dangerous direction.
# ---------------------------------------------------------------------------


def _set_governing_plan_sid(monkeypatch, sid: str = _SID_GOVERNING_PLAN) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _make_governing_plan_sessions_dir(tmp_path: Path, monkeypatch) -> Path:
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        claimed_plan.core, "sessions_dir", lambda cwd=None: str(sessions_dir)
    )
    return sessions_dir


def _write_governing_plan_claim(
    sessions_dir: Path, slug: str, sid: str, claimed_at: str = "2026-08-18T10:00:00+00:00"
) -> None:
    claim_dir = sessions_dir / "plan-claims" / slug
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _write_plan_file(worktree_root: Path, slug: str, status: str, scope_mode: str) -> Path:
    return _write(
        worktree_root / "docs" / "plans" / f"{slug}.md",
        f"---\nstatus: {status}\nscope_mode: {scope_mode}\n---\n\n# {slug}\n",
    )


def test_governing_plan_is_computed_absent_when_no_claim_is_held(tmp_path: Path, monkeypatch):
    """A session holding zero plan claims is an ordinary, computed state - not a
    degraded one. `list_held_plan_claims` reports this as `[]`, never a raise."""
    _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert record["degraded"] is False
    assert record["value"] == {
        "present": False,
        "path": None,
        "status": None,
        "scope_mode": None,
        "slug": None,
    }
    assert record["collision"] is False


def test_governing_plan_degrades_when_the_resolver_raises(tmp_path: Path, monkeypatch):
    """`list_held_plan_claims`'s OWN contract is never-raises (its module docstring
    negative-spec) - a caught exception here can only be an import/environment
    failure, never an ordinary 'no plan claimed' result, so it must degrade rather
    than fold into the absent branch (AC3, the fail-open this chunk retires)."""

    def _boom(cwd=None):
        raise RuntimeError("simulated import/environment failure")

    monkeypatch.setattr(claimed_plan, "list_held_plan_claims", _boom)

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert record["degraded"] is True
    assert "list_held_plan_claims" in record["evidence"]
    assert "simulated import/environment failure" in record["evidence"]
    assert set(record) == {"degraded", "evidence", "source"}


def test_governing_plan_reads_status_and_scope_mode_from_frontmatter(tmp_path: Path, monkeypatch):
    sessions_dir = _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)
    _write_governing_plan_claim(sessions_dir, "2026-08-18-plan-a", _SID_GOVERNING_PLAN)
    _write_plan_file(tmp_path, "2026-08-18-plan-a", "in_flight", "feature")
    monkeypatch.setattr(session_facts, "_git_run", lambda args, cwd: _fake_result(0, stdout=""))

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert record["degraded"] is False
    assert record["value"]["present"] is True
    assert record["value"]["path"] == "docs/plans/2026-08-18-plan-a.md"
    assert record["value"]["status"] == "in_flight"
    assert record["value"]["scope_mode"] == "feature"
    assert record["value"]["slug"] == "2026-08-18-plan-a"
    assert record["value"]["claims_held"] == 1
    assert record["collision"] is False


@pytest.mark.parametrize(
    "token",
    [
        "feature",
        "spec-dispatch",
        "production-patch",
        "architecture",
        "additive-only",
        "spike",
        "chore",
        "decision",
        "process",
        "spike-then-cutover",
    ],
)
def test_governing_plan_scope_mode_is_a_verbatim_pass_through(
    tmp_path: Path, monkeypatch, token: str
):
    """AC11: the served `scope_mode` is whatever the frontmatter carries, over
    every token the corpus census actually observed - no normalization, no enum,
    no canonical spelling, no default substitution. `atomic`/`fan-out` are
    deliberately absent from this list - DR-323's C7b body: they occur zero times
    in the 463-value census, and the plan_summary.py docstring naming them is
    stale prose on a `str | None`, not an enum this chunk must satisfy."""
    sessions_dir = _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)
    _write_governing_plan_claim(sessions_dir, "2026-08-18-plan-b", _SID_GOVERNING_PLAN)
    _write_plan_file(tmp_path, "2026-08-18-plan-b", "in_flight", token)
    monkeypatch.setattr(session_facts, "_git_run", lambda args, cwd: _fake_result(0, stdout=""))

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    # Exact string equality, not membership in a known set - a normalizing
    # implementation that happened to map every token to itself would still
    # pass a set-membership assertion; equality against the RAW written token
    # is what actually rules normalization out.
    assert record["value"]["scope_mode"] == token


def test_governing_plan_present_stays_true_when_the_plan_file_is_gone(
    tmp_path: Path, monkeypatch
):
    """A claim with no resolvable file on disk is not an absent plan - the claim
    is real even though the file it names is gone (same rule the lifted reader
    already carried)."""
    sessions_dir = _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)
    _write_governing_plan_claim(sessions_dir, "2026-08-18-vanished-plan", _SID_GOVERNING_PLAN)

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert record["degraded"] is False
    assert record["value"]["present"] is True
    assert record["value"]["path"] is None
    assert record["value"]["status"] is None
    assert record["value"]["scope_mode"] is None
    assert record["value"]["slug"] == "2026-08-18-vanished-plan"
    # No plan path resolved on disk means nothing to compare against `git
    # status` for THIS fact's own collision determination - short-circuits to
    # `False` without a `_dirty_paths` read, same shortcut Facts 4/5 take for
    # an empty scan.
    assert record["collision"] is False


def test_governing_plan_collision_is_true_when_the_plan_file_is_dirty(
    tmp_path: Path, monkeypatch
):
    """DR-323 § (b): Fact 2's backing surface (claim store + plan frontmatter) is
    peer-mutable, so `collision` is a real bool - this reuses Facts 4/5's own
    `_dirty_paths` mechanism, asking whether the RESOLVED plan path is itself
    currently uncommitted."""
    sessions_dir = _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)
    _write_governing_plan_claim(sessions_dir, "2026-08-18-plan-c", _SID_GOVERNING_PLAN)
    _write_plan_file(tmp_path, "2026-08-18-plan-c", "in_flight", "feature")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=" M docs/plans/2026-08-18-plan-c.md\n"),
    )

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert record["degraded"] is False
    assert record["collision"] is True


def test_governing_plan_degrades_when_dirty_paths_git_status_fails(
    tmp_path: Path, monkeypatch
):
    """`_dirty_paths`'s own `git status` call is a sub-read this fact owns - its
    failure must degrade the fact, not silently report the plan clean."""
    sessions_dir = _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)
    _write_governing_plan_claim(sessions_dir, "2026-08-18-plan-d", _SID_GOVERNING_PLAN)
    _write_plan_file(tmp_path, "2026-08-18-plan-d", "in_flight", "feature")
    monkeypatch.setattr(
        session_facts,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="fatal: not a git repository"),
    )

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert record["degraded"] is True
    assert "git status" in record["evidence"]
    assert set(record) == {"degraded", "evidence", "source"}


def test_governing_plan_computed_shape_is_exactly_the_dr319_key_set(
    tmp_path: Path, monkeypatch
):
    _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert set(record) == {"degraded", "value", "source", "collision"}


def test_governing_plan_degraded_shape_is_exactly_the_dr319_key_set(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        claimed_plan,
        "list_held_plan_claims",
        lambda cwd=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    record = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)

    assert set(record) == {"degraded", "evidence", "source"}


def test_governing_plan_carries_no_verdict_field_on_either_shape(
    tmp_path: Path, monkeypatch
):
    forbidden = {"verdict", "recommendation", "disposition", "action"}

    _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
    _set_governing_plan_sid(monkeypatch)
    computed = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)
    assert forbidden.isdisjoint(computed)
    assert forbidden.isdisjoint(computed["value"])

    monkeypatch.setattr(
        claimed_plan,
        "list_held_plan_claims",
        lambda cwd=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    degraded = session_facts.session_governing_plan(tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN)
    assert forbidden.isdisjoint(degraded)


def test_governing_plan_source_string_resolves_to_a_symbol_that_exists(tmp_path: Path):
    """AC4 / AC9: the backing-source string is not a stale or copy-pasted label - it
    names a producer symbol that actually exists in this repo."""
    module_path, _, symbol = session_facts._SOURCE_SESSION_GOVERNING_PLAN.partition("::")
    assert module_path == "coordinator_core/session/claimed_plan.py"
    assert hasattr(claimed_plan, symbol)


# ---------------------------------------------------------------------------
# fl-core-02 C6 — DR-323 § (c)'s AC9 mechanism: a per-fact required-field
# declaration (fact name -> required `value` keys) plus a test that calls
# each served facade function and asserts its returned record against it.
# `fact_contract_gate.producer_inventory.emits()` is a global membership test
# with no per-producer attribution (its own module docstring) and cannot
# answer "which fields is THIS fact required to emit" — this declaration is
# the mechanism that can, per C1(c)/DR-323 Negative-spec. Not one branch of
# an either/or with a cost: it is the only candidate.
# ---------------------------------------------------------------------------

#: fact name -> required keys inside a COMPUTED record's `value` payload.
#: A key present only on some branches (`session_governing_plan`'s
#: `claims_held`, present only when `present` is True) is deliberately NOT
#: listed — this declaration asserts what EVERY computed record of that fact
#: carries, not the union of every branch's optional extras. `None` marks a
#: fact whose `value` is not a dict at all (`session_magnitude_attributed`'s
#: is a bare int) — DR-319 § (b): the payload shape is fact-specific.
_FACT_VALUE_KEY_DECLARATION: dict[str, frozenset[str] | None] = {
    "session_magnitude_attributed": None,
    "session_pickup_kind": frozenset(
        {
            "classification",
            "artifact_path",
            "basename",
            "deliverable_id",
            "actioned_memos",
            "consumed_predecessor",
        }
    ),
    "session_governing_plan": frozenset({"present", "path", "status", "scope_mode", "slug"}),
    "session_diff_brightline": frozenset(
        {
            "scoping_method",
            "trustworthy",
            "sha_range",
            "commit_count",
            "surface_count",
            "novel_surface_count",
            "touched_paths",
            "gross_loc",
            "doc_only_loc",
            "relocated_files",
            "novel_loc",
            "novel_commit_count",
            "brightline",
            "breached",
            "under_brightline",
        }
    ),
    "session_terminal_sizings": frozenset({"scanned", "terminal", "non_terminal_count"}),
    "session_fold_sidecars": frozenset({"present", "paths", "count"}),
}

#: AC12's named key set — asserted against directly, never a hand-read, per
#: the C6 brief ("assert against a named key set, not a hand-read").
_FORBIDDEN_VERDICT_KEYS = frozenset({"verdict", "recommendation", "disposition", "action"})


def _resolve_source_symbol(source: str) -> None:
    """AC4/AC9 closure (DR-319 § Consequences' named drift mode): resolve a
    served fact's `source` string to a real symbol by IMPORTING the module
    it names and looking the attribute up — never a regex over the string,
    which would only prove the string is well-formed, not that it points at
    something real. Some served facts self-reference `session_facts` itself
    (Facts 4/5, whose scan logic lives in this module rather than
    `branch_resolution`) — `importlib.import_module` resolves either case
    identically, so the shape difference across source strings does not need
    two code paths here.
    """
    module_path, sep, symbol = source.partition("::")
    assert sep == "::", f"source string {source!r} is not of the form <path>::<symbol>"
    assert module_path.endswith(".py"), f"source string {source!r} has a non-.py module path"
    dotted = module_path[:-3].replace("/", ".")
    module = importlib.import_module(dotted)
    assert hasattr(module, symbol), (
        f"source string {source!r} names symbol {symbol!r} which does not exist on {dotted}"
    )


def _assert_dr319_computed_record(fact_name: str, record: dict) -> None:
    """The per-fact contract assertion `_FACT_VALUE_KEY_DECLARATION` exists to
    make checkable in one place (DR-323 § (c)): `degraded` present and False,
    `collision` present UNCONDITIONALLY (even when `None`-valued — the
    absent-vs-clean conflation R-11 forbids), `value` present, `source`
    present and resolves to a real symbol, no AC12 verdict-shaped key on the
    record OR inside `value`, and `value`'s own required keys (per the
    declaration above) are all present.
    """
    assert record["degraded"] is False
    assert "collision" in record
    assert "value" in record
    assert isinstance(record["source"], str) and record["source"]
    _resolve_source_symbol(record["source"])
    assert _FORBIDDEN_VERDICT_KEYS.isdisjoint(record), (
        f"{fact_name} computed record leaked a verdict key: "
        f"{_FORBIDDEN_VERDICT_KEYS & set(record)}"
    )

    required_value_keys = _FACT_VALUE_KEY_DECLARATION[fact_name]
    if required_value_keys is not None:
        assert isinstance(record["value"], dict)
        missing = required_value_keys - set(record["value"])
        assert not missing, f"{fact_name} computed record missing declared value keys: {missing}"
        assert _FORBIDDEN_VERDICT_KEYS.isdisjoint(record["value"]), (
            f"{fact_name} computed record's value leaked a verdict key"
        )


def _assert_dr319_degraded_record(record: dict) -> None:
    """DR-319 Shape 2: `degraded` present and True, no `value` key, no
    `collision` key at all (degraded means the probe could not run — there is
    no basis to declare a collision state one way or the other), `source`
    present and resolves, no AC12 verdict-shaped key.
    """
    assert record["degraded"] is True
    assert "value" not in record
    assert "collision" not in record
    assert isinstance(record["source"], str) and record["source"]
    _resolve_source_symbol(record["source"])
    assert _FORBIDDEN_VERDICT_KEYS.isdisjoint(record)


class TestPerFactRequiredFieldDeclaration:
    """DR-323 § (c)'s AC9 mechanism, exercised against every served fact:
    `_FACT_VALUE_KEY_DECLARATION` plus a call to the live facade function,
    both for a computed record and (where the fact's own degraded trigger is
    cheap to reach here) a degraded one."""

    def test_session_magnitude_attributed_satisfies_declaration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            branch_resolution, "_cached_session_commits", lambda root, sid: _fake_commits(1)
        )
        computed = session_facts.session_magnitude_attributed(tmp_path, _SID)
        _assert_dr319_computed_record("session_magnitude_attributed", computed)

        monkeypatch.setattr(
            branch_resolution, "_cached_session_commits", lambda root, sid: None
        )
        degraded = session_facts.session_magnitude_attributed(tmp_path, _SID)
        _assert_dr319_degraded_record(degraded)

    def test_session_pickup_kind_satisfies_declaration(self, tmp_path: Path):
        common = tmp_path / ".git"
        _write(_session_shape_path(common, _SID_PICKUP), json.dumps({"session_id": _SID_PICKUP}))
        computed = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)
        _assert_dr319_computed_record("session_pickup_kind", computed)

        _write(_session_shape_path(common, _SID_PICKUP), "{not valid json")
        degraded = session_facts.session_pickup_kind(tmp_path, common, _SID_PICKUP)
        _assert_dr319_degraded_record(degraded)

    def test_session_governing_plan_satisfies_declaration(self, tmp_path: Path, monkeypatch):
        _make_governing_plan_sessions_dir(tmp_path, monkeypatch)
        _set_governing_plan_sid(monkeypatch)
        monkeypatch.setattr(session_facts, "_git_run", lambda args, cwd: _fake_result(0, stdout=""))
        computed = session_facts.session_governing_plan(
            tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN
        )
        _assert_dr319_computed_record("session_governing_plan", computed)

        monkeypatch.setattr(
            claimed_plan,
            "list_held_plan_claims",
            lambda cwd=None: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        degraded = session_facts.session_governing_plan(
            tmp_path, tmp_path / ".git", _SID_GOVERNING_PLAN
        )
        _assert_dr319_degraded_record(degraded)

    def test_session_diff_brightline_satisfies_declaration(self, tmp_path, monkeypatch):
        _stub_diff_brightline_composition(
            monkeypatch, method=branch_resolution.SCOPING_METHOD_TRAILER
        )
        computed = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)
        _assert_dr319_computed_record("session_diff_brightline", computed)

        monkeypatch.setattr(
            session_facts,
            "session_commit_count_attributed",
            lambda worktree_root, sid: {"degraded": True, "evidence": "boom"},
        )
        degraded = session_facts.session_diff_brightline(tmp_path, tmp_path / ".git", _SID_DIFF)
        _assert_dr319_degraded_record(degraded)

    def test_session_terminal_sizings_satisfies_declaration(self, tmp_path: Path, monkeypatch):
        sizings_dir = tmp_path / "state" / "sizings"
        _write_sizing(sizings_dir, "a.yaml", "shipped")
        monkeypatch.setattr(session_facts, "_git_run", lambda args, cwd: _fake_result(0, stdout=""))
        computed = session_facts.session_terminal_sizings(tmp_path)
        _assert_dr319_computed_record("session_terminal_sizings", computed)

        monkeypatch.setattr(
            session_facts, "_git_run", lambda args, cwd: _fake_result(128, stderr="boom")
        )
        degraded = session_facts.session_terminal_sizings(tmp_path)
        _assert_dr319_degraded_record(degraded)

    def test_session_fold_sidecars_satisfies_declaration(self, tmp_path: Path, monkeypatch):
        _write_sidecar(tmp_path / "state" / "execution-records", "a.json")
        monkeypatch.setattr(session_facts, "_git_run", lambda args, cwd: _fake_result(0, stdout=""))
        computed = session_facts.session_fold_sidecars(tmp_path)
        _assert_dr319_computed_record("session_fold_sidecars", computed)

        monkeypatch.setattr(
            session_facts, "_git_run", lambda args, cwd: _fake_result(128, stderr="boom")
        )
        degraded = session_facts.session_fold_sidecars(tmp_path)
        _assert_dr319_degraded_record(degraded)

    def test_declaration_covers_every_served_fact(self):
        """The declaration itself must not silently drop a fact — a future
        sixth served fact with no declaration entry would defeat AC9's
        mechanism silently, the exact failure mode this chunk closes."""
        served = {
            "session_magnitude_attributed",
            "session_pickup_kind",
            "session_governing_plan",
            "session_diff_brightline",
            "session_terminal_sizings",
            "session_fold_sidecars",
        }
        assert set(_FACT_VALUE_KEY_DECLARATION) == served


# ---------------------------------------------------------------------------
# AC11 regression pin — the facade's own frontmatter read must not silently
# lose a declared value (fl-core-02, EM fix on C8's finding).
# ---------------------------------------------------------------------------


_OBSERVED_SCOPE_MODE_TOKENS = (
    "feature",
    "spec-dispatch",
    "production-patch",
    "architecture",
    "additive-only",
    "spike",
    "chore",
    "decision",
    "process",
    "spike-then-cutover",
)


def _plan_with_scope_mode(tmp_path: Path, raw_value: str) -> Path:
    """Write a minimal plan file whose `scope_mode:` line carries `raw_value`
    exactly as authored — quoting, trailing comment and all."""
    path = tmp_path / "plan.md"
    path.write_text(
        f'---\ntitle: "a plan"\nscope_mode: {raw_value}\nstatus: approved\n---\n\nbody\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("token", _OBSERVED_SCOPE_MODE_TOKENS)
def test_scope_mode_reads_every_observed_corpus_token_verbatim(tmp_path: Path, token: str):
    """AC11: the served value is the frontmatter's own string — no enum, no
    canonical spelling, no default substitution — across all ten tokens the
    corpus census observed in 463 declared values."""
    assert session_facts._read_frontmatter_scope_mode(
        _plan_with_scope_mode(tmp_path, token)
    ) == token


def test_scope_mode_survives_a_trailing_comment(tmp_path: Path):
    """The regression this pin exists for.

    C7b re-declared `quick_wrap_assemble`'s `_SCOPE_MODE_RE` verbatim, whose
    trailing `\\s*$` anchor does not tolerate a `# comment` suffix: a plan
    declaring `scope_mode: spec-dispatch  # routed via dispatch` read as None,
    i.e. as having declared no route at all. A declared value reading as absent
    is the fail-open conflation this plan exists to retire, and it would have
    become an AC11 verbatim regression the moment a consumer converged onto the
    facade instead of onto `frontmatter.primitives.read_fm_field_unquoted`.

    Do not "simplify" this back to a local regex — see the negative-spec on
    `session_facts._read_frontmatter_scope_mode`.
    """
    assert session_facts._read_frontmatter_scope_mode(
        _plan_with_scope_mode(tmp_path, "spec-dispatch  # routed via dispatch")
    ) == "spec-dispatch"


def test_scope_mode_strips_one_layer_of_yaml_quoting_only(tmp_path: Path):
    """Quoting is YAML syntax, not part of the value; one layer comes off and
    the token underneath is returned unchanged."""
    assert session_facts._read_frontmatter_scope_mode(
        _plan_with_scope_mode(tmp_path, '"architecture"')
    ) == "architecture"
    assert session_facts._read_frontmatter_scope_mode(
        _plan_with_scope_mode(tmp_path, "'spec-dispatch'")
    ) == "spec-dispatch"


def test_scope_mode_absent_key_reads_as_none_not_as_a_default(tmp_path: Path):
    """An absent key is absent — never substituted with a default, which would
    manufacture a route the plan never declared."""
    path = tmp_path / "plan.md"
    path.write_text(
        '---\ntitle: "a plan"\nstatus: approved\n---\n\nbody\n', encoding="utf-8"
    )
    assert session_facts._read_frontmatter_scope_mode(path) is None


# ---------------------------------------------------------------------------
# Slice-A review finding (P1) — `kind:` and `status:` carried the identical
# trailing-comment defect the `scope_mode:` fix closed, and are fixed with it.
# ---------------------------------------------------------------------------


def test_kind_survives_a_trailing_comment(tmp_path: Path):
    """A `kind:` line carrying a `# comment` must read as its declared value.

    The regression this pin exists for is worse than a None: `session_pickup_kind`
    falls back to a DEFAULT classification when the kind read returns None, so a
    commented `kind:` silently misclassified the session rather than degrading.
    A misread that picks a plausible value is invisible to DR-319's contract test,
    which can only see the record's shape — which is why this is pinned at the
    reader rather than left to the fact-level assertions.
    """
    path = tmp_path / "handoff.md"
    path.write_text(
        '---\ntitle: "a handoff"\nkind: session-handoff  # continuation\n---\n\nbody\n',
        encoding="utf-8",
    )
    assert session_facts._read_frontmatter_kind(path) == "session-handoff"


def test_status_survives_a_trailing_comment(tmp_path: Path):
    """A `status:` line carrying a `# comment` must read as its declared value.

    Same defect class as `kind:` above, with a different consequence: a genuinely
    terminal sizing whose `status:` carried a comment was excluded from the
    terminal scan, so `session_terminal_sizings` reported a clean, smaller scan
    instead of degrading or counting it.
    """
    path = tmp_path / "sizing.yaml"
    path.write_text(
        '---\nstatus: shipped  # landed today\ntitle: "a sizing"\n---\n', encoding="utf-8"
    )
    assert session_facts._read_frontmatter_status(path) == "shipped"


def test_the_three_frontmatter_readers_share_one_parser(tmp_path: Path):
    """Negative-spec pin: all three readers must agree with the canonical parser.

    They were three independently-declared regexes carrying one defect; a future
    "simplification" back to a local regex on any one of them re-opens it on that
    field alone, which is how the `scope_mode` fix left `kind`/`status` behind in
    the first place. Asserting them together is what makes the divergence loud.
    """
    body = (
        '---\nkind: session-handoff  # c\nstatus: shipped  # c\n'
        'scope_mode: spec-dispatch  # c\n---\n\nbody\n'
    )
    path = tmp_path / "all-three.md"
    path.write_text(body, encoding="utf-8")
    assert session_facts._read_frontmatter_kind(path) == "session-handoff"
    assert session_facts._read_frontmatter_status(path) == "shipped"
    assert session_facts._read_frontmatter_scope_mode(path) == "spec-dispatch"
