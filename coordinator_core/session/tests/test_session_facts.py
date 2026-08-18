"""Contract test for `coordinator_core.session.session_facts` — DR-319's return-shape
contract (docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md).

This is what discharges AC3, AC6, and AC8
(docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md § C2): a TEST that the
posture holds, not a document describing it (the Staff Engineer F9/F10).

Git failure is simulated by monkeypatching the producer seam
(`branch_resolution._git_run`), not by requiring a broken repo — a broken git repo is a
flaky, environment-dependent way to exercise a code path that is really "the subprocess
call returned nonzero," which is trivially and deterministically faked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import branch_resolution
from coordinator_core.session import session_facts

_SID = "sess-c2-contract-test-001"
_SID_PICKUP = "sess-c2-pickup-kind-test-001"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _session_shape_path(common_dir: Path, sid: str) -> Path:
    return common_dir / "coordinator-sessions" / sid / "session-shape.json"


def _fake_result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_computed_record_is_distinguishable_from_a_degraded_record(tmp_path, monkeypatch):
    """The whole point of the posture: a degraded read and a genuinely-zero read must
    not collapse into the same value at the call site."""
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )
    zero_record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="fatal: not a git repository"),
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
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout="abc123 one\ndef456 two\n"),
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
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
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
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout="abc123 one\n"),
    )
    computed = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert forbidden.isdisjoint(computed), f"computed record leaked a verdict key: {set(computed) & forbidden}"

    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(1, stderr="boom"),
    )
    degraded = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert forbidden.isdisjoint(degraded), f"degraded record leaked a verdict key: {set(degraded) & forbidden}"


def test_computed_shape_is_exactly_the_dr319_key_set(tmp_path, monkeypatch):
    """No shape other than DR-319's two is legal, regardless of internal consistency."""
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout="abc123 one\n"),
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
