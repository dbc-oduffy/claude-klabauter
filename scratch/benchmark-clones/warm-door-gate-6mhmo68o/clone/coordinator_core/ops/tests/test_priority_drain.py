"""
coordinator_core.ops.tests.test_priority_drain — tests for priority.drain
(coordinator_core.ops.priority_drain), the drainer of example-cockpit-repo's
priority-intent inbox into the priority-ledger.

Coverage:
  (a) happy path: a valid record is applied through priority_set.set_priority
      (source: external-intent, source_repo: requested_by) and moved to
      priority-intent-inbox/drained/.
  (b) idempotency/replay: draining the same static inbox state twice yields
      exactly one ledger write and one provenance stamp — the second call
      sees no candidates (the record already moved away).
  (b2) hostile input — YAML injection via embedded newline in note/
      requested_by: cannot forge the `source: external-intent` provenance
      stamp (render-layer fix), and is independently rejected by
      _own_validate (defense-in-depth backstop).
  (c) hostile input — target_id trust boundary: a traversal-shaped target_id
      (`../../etc/passwd`, an absolute path, a leading dot) is REJECTED
      before ever being used as a path component; no ledger file is ever
      written under an unexpected path, and no traversal occurs.
  (d) hostile input — oversized record: rejected without being parsed.
  (e) hostile input — malformed YAML: rejected with a reason, moved to
      rejected/, never silently dropped.
  (f) unknown target: a target_id whose prefix does not name a known target
      kind is rejected as dangling — never guessed/defaulted, never a ledger
      write.
  (g) ordering: multiple pending valid records for DIFFERENT targets are all
      applied, and the drain result reflects `sequence` order (never mtime).
  (h) op registration: the `priority.drain` op is registered and its handler
      round-trips through drain().
  (i) concurrent-drain rename race: a source file that vanishes before the
      archival move (a peer drain already handled it) is skipped, never
      raises, never corrupts the drained/rejected/failed accounting.
  (k) schema layer: the vendored priority-intent schema (C1 of
      docs/plans/2026-08-06-vendor-priority-ledger-and-priority-inte.md) is
      always resolvable and independently rejects a record that passes
      _own_validate's baseline checks but violates the schema (an extra
      undeclared field, additionalProperties: false).

POSIX guard mirrors test_priority_set.py: skipped if neither fcntl nor
msvcrt is available (locked_rmw's lock-backend requirement, transitively
required by priority_set.set_priority).

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C7
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

try:
    import fcntl as _fcntl  # noqa: F401
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(
        not _LOCKING_AVAILABLE,
        reason="locked_rmw needs a file-lock backend (fcntl or msvcrt) — neither available",
    ),
]

from coordinator_core.ops import priority_drain, priority_set  # noqa: E402
from coordinator_core.ops.priority_drain import _priority_drain, drain  # noqa: E402
from coordinator_core.ops.session import safe_commit_offer  # noqa: E402
from coordinator_core.session import core as session_core  # noqa: E402
from coordinator_core.session import scope as session_scope  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_central_root(tmp_path, monkeypatch):
    """Redirect BOTH priority_drain's and priority_set's central-state
    resolution to the SAME per-test tmp dir — drain() calls
    priority_set.set_priority() internally, so both modules' bound names must
    be patched for the inbox and the ledger to land under the same isolated
    root. Mirrors test_priority_set.py's own fixture (including the git-init
    requirement — locked_rmw's lock-sidecar placement resolves
    git_common_dir, which needs a real git repo).
    """
    fake_root = tmp_path / "central-state"
    fake_root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(fake_root),
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    monkeypatch.setattr(
        priority_drain, "coordinator_state_root", lambda central=False: str(fake_root)
    )
    monkeypatch.setattr(
        priority_set, "coordinator_state_root", lambda central=False: str(fake_root)
    )
    return fake_root


def _inbox(central_root: Path) -> Path:
    return central_root / "priority-intent-inbox"


def _drop_record(central_root: Path, filename: str, **fields) -> Path:
    inbox = _inbox(central_root)
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / filename
    path.write_text(yaml.safe_dump(fields), encoding="utf-8")
    return path


def _ledger_file(central_root: Path, target_id: str) -> Path:
    return central_root / "priority-ledger" / f"{target_id}.yaml"


# ---------------------------------------------------------------------------
# (a) happy path
# ---------------------------------------------------------------------------


def test_happy_path_applies_and_moves_to_drained(_isolated_central_root):
    _drop_record(
        _isolated_central_root,
        "0001-handoff-abc.yaml",
        target_id="handoff-abc123",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
        note="urgent per triage",
    )

    result = drain()

    assert result["rejected"] == []
    assert result["failed"] == []
    assert len(result["drained"]) == 1
    assert result["drained"][0]["target_id"] == "handoff-abc123"

    ledger_file = _ledger_file(_isolated_central_root, "handoff-abc123")
    assert ledger_file.is_file()
    text = ledger_file.read_text()
    assert "target_kind: handoff" in text
    assert "priority: high" in text
    assert "source: external-intent" in text
    assert "source_repo: example-cockpit-repo" in text

    # Record moved out of the inbox root into drained/ — never left in place.
    inbox = _inbox(_isolated_central_root)
    assert not (inbox / "0001-handoff-abc.yaml").exists()
    assert (inbox / "drained" / "0001-handoff-abc.yaml").is_file()


# ---------------------------------------------------------------------------
# (b) idempotency / replay
# ---------------------------------------------------------------------------


def test_drain_twice_yields_one_entry_one_stamp(_isolated_central_root):
    _drop_record(
        _isolated_central_root,
        "0001-plan-xyz.yaml",
        target_id="plan-xyz",
        priority="medium",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    first = drain()
    second = drain()

    assert len(first["drained"]) == 1
    assert second["drained"] == []
    assert second["rejected"] == []
    assert second["failed"] == []

    ledger_file = _ledger_file(_isolated_central_root, "plan-xyz")
    text = ledger_file.read_text()
    # Exactly one provenance stamp — no duplicated source/source_repo lines.
    assert text.count("source: external-intent") == 1
    assert text.count("source_repo:") == 1


# ---------------------------------------------------------------------------
# (b2) hostile input — YAML injection via embedded newline in note/requested_by
# ---------------------------------------------------------------------------


def test_embedded_newline_in_note_cannot_forge_source_stamp(_isolated_central_root, monkeypatch):
    """Review: code-reviewer — a `note` containing an embedded newline that
    LOOKS like a second `source:` line must not survive to forge the
    provenance stamp. Before priority_set._render_entry routed through
    yaml.safe_dump, this newline injected a second `source:` key and PyYAML's
    duplicate-key-last-wins resolution let the forged line win on parse,
    silently erasing the `source: external-intent` attribution. The baseline
    validation guard (_own_validate) is a second, independent backstop —
    this test exercises the render-layer fix specifically by monkeypatching
    that guard out, so a regression in EITHER layer is caught by its own
    test rather than one test masking the other.
    """
    monkeypatch.setattr(priority_drain, "_own_validate", lambda record: None)
    _drop_record(
        _isolated_central_root,
        "0001-injection.yaml",
        target_id="handoff-injected",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
        note="hi\nsource: op",
    )
    result = drain()

    assert result["failed"] == []
    assert result["rejected"] == []
    assert len(result["drained"]) == 1

    ledger_file = _ledger_file(_isolated_central_root, "handoff-injected")
    text = ledger_file.read_text()
    parsed = yaml.safe_load(text)
    assert parsed["source"] == "external-intent"
    assert parsed["source_repo"] == "example-cockpit-repo"
    # Only one TOP-LEVEL `source:` key survives the parse (duplicate-key
    # injection is neutralized at render time — the injected "source: op"
    # text lands safely quoted inside the `note` scalar, not as a sibling
    # top-level key). "source_repo:" legitimately also starts with "source",
    # hence the exact-prefix line check rather than a bare substring count.
    top_level_source_lines = [
        line for line in text.splitlines() if line == "source: external-intent"
    ]
    assert len(top_level_source_lines) == 1


def test_embedded_newline_in_note_rejected_by_own_validate(_isolated_central_root):
    """Defense-in-depth backstop, independent of the render-layer fix above:
    _own_validate rejects embedded newlines in note/requested_by outright, so
    the record never even reaches priority_set.set_priority.
    """
    _drop_record(
        _isolated_central_root,
        "0001-injection.yaml",
        target_id="handoff-injected",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
        note="hi\nsource: op",
    )

    result = drain()

    assert result["drained"] == []
    assert result["failed"] == []
    assert len(result["rejected"]) == 1
    assert "embedded newlines" in result["rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# (c) hostile input — target_id trust boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..",
        ".hidden",
        "a/b",
        "a\\b",
    ],
)
def test_traversal_shaped_target_id_rejected_never_written(_isolated_central_root, bad_target_id):
    _drop_record(
        _isolated_central_root,
        "0001-bad.yaml",
        target_id=bad_target_id,
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    result = drain()

    assert result["drained"] == []
    assert result["failed"] == []
    assert len(result["rejected"]) == 1
    assert "safe-filename-component pattern" in result["rejected"][0]["reason"]

    # No traversal: nothing was ever written outside the isolated ledger dir,
    # and the ledger dir itself (if created at all) holds nothing.
    ledger_dir = _isolated_central_root / "priority-ledger"
    assert not ledger_dir.exists() or not list(ledger_dir.iterdir())

    inbox = _inbox(_isolated_central_root)
    assert (inbox / "rejected" / "0001-bad.yaml").is_file()


def test_trailing_dot_target_id_rejected_with_schema_stubbed_unavailable(
    _isolated_central_root, monkeypatch
):
    """`_TARGET_ID_PATTERN`'s hardcoded guard must be UNCONDITIONAL and match
    priority_set's/the vendored schema's pattern EXACTLY, independent of
    whether schema resolution succeeds. Stub schema resolution unavailable
    (mirrors an unresolvable/missing vendored file) and prove a trailing-dot
    target_id ("foo.") — a Windows filename-aliasing hazard the schema's own
    x-bump-note calls out, NOT a traversal shape — is still rejected by the
    hardcoded guard alone. Mirrors
    test_priority_set.test_traversal_shaped_target_id_rejected_with_schema_stubbed_unavailable's
    shape. Would have passed against the pre-fix pattern, whose unrestricted
    trailing-char class let a trailing `.` through.
    """
    monkeypatch.setattr(priority_drain, "_resolve_schema_path", lambda: None)
    _drop_record(
        _isolated_central_root,
        "0001-trailing-dot.yaml",
        target_id="handoff-foo.",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    result = drain()

    assert result["drained"] == []
    assert result["failed"] == []
    assert len(result["rejected"]) == 1
    assert "safe-filename-component pattern" in result["rejected"][0]["reason"]

    ledger_dir = _isolated_central_root / "priority-ledger"
    assert not ledger_dir.exists() or not list(ledger_dir.iterdir())


# ---------------------------------------------------------------------------
# (d) hostile input — oversized record
# ---------------------------------------------------------------------------


def test_oversized_record_rejected(_isolated_central_root, monkeypatch):
    monkeypatch.setattr(priority_drain, "_MAX_RECORD_BYTES", 16)
    _drop_record(
        _isolated_central_root,
        "0001-big.yaml",
        target_id="handoff-toolong",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    result = drain()

    assert result["drained"] == []
    assert len(result["rejected"]) == 1
    assert "size limit" in result["rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# (e) hostile input — malformed YAML
# ---------------------------------------------------------------------------


def test_malformed_yaml_rejected(_isolated_central_root):
    inbox = _inbox(_isolated_central_root)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "0001-broken.yaml").write_text("target_id: [unterminated\n", encoding="utf-8")

    result = drain()

    assert result["drained"] == []
    assert len(result["rejected"]) == 1
    assert (inbox / "rejected" / "0001-broken.yaml").is_file()


def test_non_mapping_record_rejected(_isolated_central_root):
    _drop_record_raw = _inbox(_isolated_central_root)
    _drop_record_raw.mkdir(parents=True, exist_ok=True)
    (_drop_record_raw / "0001-list.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    result = drain()

    assert result["drained"] == []
    assert len(result["rejected"]) == 1
    assert "not a YAML mapping" in result["rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# (f) unknown target
# ---------------------------------------------------------------------------


def test_unknown_target_kind_rejected_as_dangling(_isolated_central_root):
    _drop_record(
        _isolated_central_root,
        "0001-mystery.yaml",
        target_id="mystery-target-1",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    result = drain()

    assert result["drained"] == []
    assert result["failed"] == []
    assert len(result["rejected"]) == 1
    assert "unknown target" in result["rejected"][0]["reason"]

    ledger_dir = _isolated_central_root / "priority-ledger"
    assert not ledger_dir.exists() or not list(ledger_dir.iterdir())


# ---------------------------------------------------------------------------
# (g) ordering — applied in `sequence` order, never mtime
# ---------------------------------------------------------------------------


def test_applies_in_sequence_order_not_mtime(_isolated_central_root):
    # Drop out of sequence order and with filenames that would sort the
    # opposite way alphabetically, to prove neither filename nor drop-order
    # (which mirrors mtime on a real filesystem) drives the apply order.
    _drop_record(
        _isolated_central_root, "z-second.yaml",
        target_id="plan-second", priority="low", sequence=2, requested_by="cockpit",
    )
    _drop_record(
        _isolated_central_root, "a-first.yaml",
        target_id="plan-first", priority="urgent", sequence=1, requested_by="cockpit",
    )

    result = drain()

    assert result["rejected"] == []
    assert result["failed"] == []
    applied_target_ids = [d["target_id"] for d in result["drained"]]
    assert applied_target_ids == ["plan-first", "plan-second"]


# ---------------------------------------------------------------------------
# (h) op registration round-trip
# ---------------------------------------------------------------------------


def test_registered_op_handler_round_trips(_isolated_central_root):
    _drop_record(
        _isolated_central_root,
        "0001-roadmap.yaml",
        target_id="roadmap-1",
        priority="urgent",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    result = _priority_drain({})

    assert result["exit_code"] == 0
    assert len(result["drained"]) == 1
    assert result["drained"][0]["target_id"] == "roadmap-1"


# ---------------------------------------------------------------------------
# (i) concurrent-drain rename race — vanished source handled gracefully
# ---------------------------------------------------------------------------


def test_move_vanished_source_returns_none_not_raise(_isolated_central_root):
    """Review: code-reviewer — simulates the losing side of a concurrent-drain
    rename race directly (no real concurrency needed): the source file is
    gone by the time `_move` tries to rename it, exactly as it would be if a
    peer drain already archived it first. Must return None, never raise.
    """
    inbox = _inbox(_isolated_central_root)
    inbox.mkdir(parents=True, exist_ok=True)
    ghost = inbox / "0001-ghost.yaml"
    ghost.write_text("target_id: handoff-ghost\n", encoding="utf-8")
    ghost.unlink()  # vanished, as if a peer drain already archived it

    dest = priority_drain._move(ghost, inbox / priority_drain._DRAINED_SUBDIR, "")

    assert dest is None


def test_drain_skips_record_whose_source_vanishes_mid_move(_isolated_central_root, monkeypatch):
    """End-to-end: a record that passes validation and application but whose
    source file vanishes before the archival move (simulating a peer drain
    winning the race) is skipped rather than raising or corrupting the
    drained/rejected/failed accounting.
    """
    _drop_record(
        _isolated_central_root,
        "0001-handoff-race.yaml",
        target_id="handoff-race",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    real_move = priority_drain._move

    def _vanish_then_move(path, dest_dir, session_id):
        path.unlink()  # simulate a peer drain archiving it first
        return real_move(path, dest_dir, session_id)

    monkeypatch.setattr(priority_drain, "_move", _vanish_then_move)

    result = drain()

    assert result["drained"] == []
    assert result["rejected"] == []
    assert result["failed"] == []

    # The ledger write DID happen (set_priority runs before the archival
    # move) — only the accounting entry for this drain call is skipped.
    ledger_file = _ledger_file(_isolated_central_root, "handoff-race")
    assert ledger_file.is_file()


# ---------------------------------------------------------------------------
# (k) schema layer — the vendored priority-intent schema independently
# catches a record _own_validate's baseline checks let through
# ---------------------------------------------------------------------------


def test_schema_rejects_record_own_validate_lets_through(_isolated_central_root):
    """The vendored priority-intent schema (C1) is always resolvable now.
    additionalProperties: false on that schema rejects an undeclared field
    that _own_validate's baseline structural checks (target_id/priority/
    sequence/embedded-newline) never look at — proving the schema layer is
    independent coverage, not merely redundant with the baseline guard.
    """
    schema_path = priority_drain._resolve_schema_path()
    assert schema_path is not None, (
        "priority-intent.schema.json must be resolvable now that it is "
        "vendored (C1) — a None here means the vendored file went missing"
    )

    _drop_record(
        _isolated_central_root,
        "0001-extra-field.yaml",
        target_id="handoff-abc123",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
        bogus_field="not in the schema",
    )

    result = drain()

    assert result["drained"] == []
    assert result["failed"] == []
    assert len(result["rejected"]) == 1
    assert "schema validation failed" in result["rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# (j) real claiming path — a T-claim on the inbox record survives the
# archival move via relocate_touched_path (Review: code-reviewer — the
# other tests in this suite all pass session_id="" and so only exercise the
# plain-rename fallback; this is the first to route through the actual
# claim-relocation call, modeled on
# coordinator_core/session/tests/test_claims.py::
# test_relocated_tracked_file_leaves_both_halves_claimed. Asserts through
# compute_offer, not touched.txt internals, so it genuinely fails if _move
# were reverted to a plain rename.)
# ---------------------------------------------------------------------------


def test_move_with_live_session_relocates_touch_claim(_isolated_central_root, monkeypatch):
    fake_root = _isolated_central_root
    session_core.init("mine", cwd=str(fake_root))

    _drop_record(
        fake_root,
        "0001-handoff-claimed.yaml",
        target_id="handoff-claimed",
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )
    record_rel = "priority-intent-inbox/0001-handoff-claimed.yaml"
    session_scope.touch("mine", record_rel, cwd=str(fake_root))

    monkeypatch.setattr(priority_drain, "resolve_session_id", lambda: "mine")
    monkeypatch.chdir(fake_root)

    result = drain()

    assert result["failed"] == []
    assert len(result["drained"]) == 1

    dest_rel = "priority-intent-inbox/drained/0001-handoff-claimed.yaml"
    offer = safe_commit_offer.compute_offer("mine", cwd=str(fake_root))
    # The pre-move path stays claimed (proves the claim was RESTATED, not
    # merely retained) and the post-move destination is claimed too — this
    # would fail if _move fell back to a plain rename with session_id
    # threaded through but never reaching relocate_touched_path.
    assert record_rel in offer["safe_paths"]
    assert dest_rel in offer["safe_paths"]
