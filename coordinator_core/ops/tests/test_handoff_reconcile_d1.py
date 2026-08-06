"""
coordinator_core.ops.tests.test_handoff_reconcile_d1

D1 severed-observer gate tests: the conservation assertion added to
handoff.reconcile_open (docs/plans/2026-07-26-push-side-write-discipline.md
§ D1) — every candidate present in run N's surfaced[] must, by run N+1, be
either terminal (no longer in the open set) or carrying a recorded
reconcile_disposition/reconcile_disposition_reason pair. See the D1 section
of coordinator_core/ops/handoff_reconcile.py's module docstring for the full
rationale (why not a threshold, why not a policy key, why this exact
fail-loud channel).

Kept in a separate file from test_handoff_reconcile.py (D1's write-scope is
additive to that op, not a rework of its existing AC5/AC8/AC9 coverage, and a
separate file avoids touching a large shared test file on a tree with many
concurrent sessions).

Import guard: coordinator_core.ops.handoff_reconcile MUST be imported at
module load time to fire @register_op("handoff.reconcile_open") — mirrors
test_handoff_reconcile.py's own guard.

Coverage:
  (1) unit: _check_conservation — still-open+no-disposition is a violation,
      terminal (absent from open_by_id) passes, recorded disposition passes,
      empty previous-surfaced set is trivially clean.
  (2) unit: _has_recorded_disposition — BOTH fields required; either alone
      does not count.
  (3) unit: _load_surfaced_history / _save_surfaced_history round-trip,
      including tolerance of a missing/malformed history file.
  (4) integration via _handler across two real passes: a candidate surfaced
      in run 1 and untouched by run 2 fires (exit_code 2,
      conservation_violations non-empty); the same candidate with a recorded
      disposition written between runs clears; the same candidate shipped
      (moved to terminal) between runs clears.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

import coordinator_core.ops.handoff_reconcile  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_reconcile import (
    _DISPOSITION_FIELD,
    _DISPOSITION_REASON_FIELD,
    _check_conservation,
    _handler,
    _has_recorded_disposition,
    _history_path,
    _load_surfaced_history,
    _save_surfaced_history,
)

_OP_NAME = "handoff.reconcile_open"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile @register_op did not fire"
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (1) _check_conservation
# ---------------------------------------------------------------------------


def test_still_open_no_disposition_is_a_violation():
    previous = {"h1": "state/handoffs/h1.md"}
    open_by_id = {"h1": {"id": "h1"}}
    violations = _check_conservation(previous, open_by_id)
    assert len(violations) == 1
    assert violations[0]["handoff_id"] == "h1"
    assert violations[0]["handoff_path"] == "state/handoffs/h1.md"
    assert "conservation assertion violated" in violations[0]["reason"]


def test_terminal_candidate_passes():
    """h1 is absent from open_by_id — no longer open, i.e. terminal since run N."""
    previous = {"h1": "state/handoffs/h1.md"}
    open_by_id: dict = {}
    assert _check_conservation(previous, open_by_id) == []


def test_recorded_disposition_passes():
    previous = {"h1": "state/handoffs/h1.md"}
    open_by_id = {
        "h1": {
            "id": "h1",
            _DISPOSITION_FIELD: "acknowledged",
            _DISPOSITION_REASON_FIELD: "waiting on an external dependency",
        }
    }
    assert _check_conservation(previous, open_by_id) == []


def test_empty_previous_surfaced_is_trivially_clean():
    assert _check_conservation({}, {"h1": {"id": "h1"}}) == []


def test_multiple_previous_candidates_each_evaluated_independently():
    previous = {"h1": "state/handoffs/h1.md", "h2": "state/handoffs/h2.md"}
    open_by_id = {
        "h1": {"id": "h1"},  # still open, no disposition -> violation
        # h2 absent -> terminal, passes
    }
    violations = _check_conservation(previous, open_by_id)
    assert [v["handoff_id"] for v in violations] == ["h1"]


# ---------------------------------------------------------------------------
# (2) _has_recorded_disposition
# ---------------------------------------------------------------------------


def test_disposition_without_reason_is_not_recorded():
    meta = {_DISPOSITION_FIELD: "acknowledged"}
    assert _has_recorded_disposition(meta) is False


def test_reason_without_disposition_is_not_recorded():
    meta = {_DISPOSITION_REASON_FIELD: "because"}
    assert _has_recorded_disposition(meta) is False


def test_blank_strings_do_not_count():
    meta = {_DISPOSITION_FIELD: "   ", _DISPOSITION_REASON_FIELD: "   "}
    assert _has_recorded_disposition(meta) is False


def test_both_fields_non_empty_counts():
    meta = {_DISPOSITION_FIELD: "acknowledged", _DISPOSITION_REASON_FIELD: "tracked elsewhere"}
    assert _has_recorded_disposition(meta) is True


# ---------------------------------------------------------------------------
# (3) history persistence round-trip
# ---------------------------------------------------------------------------


def test_history_round_trip(handoff_repo):
    common_dir = handoff_repo.common_dir
    path = _history_path(common_dir)
    assert _load_surfaced_history(path) == ({}, None)

    _run(_save_surfaced_history(path, {"h1": "state/handoffs/h1.md"}, common_dir))
    assert _load_surfaced_history(path) == ({"h1": "state/handoffs/h1.md"}, None)

    # Location: <common_dir>/coordinator-sessions/reconcile-history/surfaced-history.json
    assert path.name == "surfaced-history.json"
    assert path.parent.name == "reconcile-history"
    assert path.parent.parent.name == "coordinator-sessions"
    assert path.parent.parent.parent == common_dir


def test_history_load_tolerates_missing_file(tmp_path):
    # Review: code-reviewer (Finding 2) — genuinely-absent is NOT an error
    # (first-ever-run case), distinct from present-but-corrupt below.
    assert _load_surfaced_history(tmp_path / "nonexistent" / "surfaced-history.json") == ({}, None)


def test_history_load_fails_loud_on_malformed_json(tmp_path):
    # Review: code-reviewer (Finding 2) — a PRESENT but corrupt file must
    # surface a non-None load_error, not silently degrade to a clean {}.
    path = _history_path(tmp_path / "gitdir")
    path.parent.mkdir(parents=True)
    path.write_text("not json{{{", encoding="utf-8")
    surfaced, load_error = _load_surfaced_history(path)
    assert surfaced == {}
    assert load_error is not None
    assert "JSON" in load_error


def test_history_load_fails_loud_on_wrong_shape(tmp_path):
    # Review: code-reviewer (Finding 2) — malformed shape (surfaced key not
    # an object) is the same "present but corrupt" class as bad JSON.
    path = _history_path(tmp_path / "gitdir")
    path.parent.mkdir(parents=True)
    path.write_text('{"surfaced": "not-a-dict"}', encoding="utf-8")
    surfaced, load_error = _load_surfaced_history(path)
    assert surfaced == {}
    assert load_error is not None


# ---------------------------------------------------------------------------
# (4) integration — two real _handler passes
# ---------------------------------------------------------------------------


def _seed_ambiguous(handoff_repo, name: str, scope_file: str):
    """An open handoff with no candidate commit at all — C2 verdict=no-match,
    always lands in surfaced[] (mirrors test_handoff_reconcile.py's
    _build_ac5_fixture ambiguous #1)."""
    handoff_repo.seed_handoff(
        name,
        "open",
        deployment_state="open",
        extra=f'title: "unmatched ambiguous work"\nscope:\n  - {scope_file}',
    )


def test_unactioned_candidate_fires_on_second_pass(handoff_repo):
    _seed_ambiguous(handoff_repo, "2026-01-01-d1-unactioned.md", "nonexistent-a.py")

    result1 = _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))
    assert result1["exit_code"] == 0
    assert result1["conservation_violations"] == []
    assert any(e["handoff_id"] == "2026-01-01-d1-unactioned" for e in result1["surfaced"])

    # Second pass — nothing on disk changed since run 1.
    result2 = _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))
    assert result2["exit_code"] == 2
    assert len(result2["conservation_violations"]) == 1
    assert result2["conservation_violations"][0]["handoff_id"] == "2026-01-01-d1-unactioned"


def test_recorded_disposition_between_runs_clears_violation(handoff_repo):
    _seed_ambiguous(handoff_repo, "2026-01-01-d1-disposed.md", "nonexistent-b.py")

    result1 = _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))
    assert result1["conservation_violations"] == []

    # Operator records a disposition on disk between run 1 and run 2.
    path = Path(handoff_repo.abs_path("2026-01-01-d1-disposed.md"))
    text = path.read_text(encoding="utf-8")
    disposed = text.replace(
        "\n---\n\n# Handoff",
        (
            f'\n{_DISPOSITION_FIELD}: "acknowledged"\n'
            f'{_DISPOSITION_REASON_FIELD}: "known false positive, tracked externally"'
            "\n---\n\n# Handoff"
        ),
        1,
    )
    assert disposed != text, "fixture's closing frontmatter delimiter shape changed"
    path.write_text(disposed, encoding="utf-8")

    # Sanity: the field really is present in the frontmatter block (read_fm_field
    # returns the raw scalar text, quotes included — _read_meta's full YAML parse,
    # which is what the op actually reads through, is exercised by result2 below).
    split = split_frontmatter(disposed)
    assert split is not None
    assert read_fm_field(split.fm_text, _DISPOSITION_FIELD) == '"acknowledged"'

    result2 = _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))
    assert result2["exit_code"] == 0
    assert result2["conservation_violations"] == []


def test_candidate_reaching_terminal_between_runs_clears_violation(handoff_repo):
    _seed_ambiguous(handoff_repo, "2026-01-01-d1-terminal.md", "nonexistent-c.py")

    result1 = _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))
    assert result1["conservation_violations"] == []

    # Simulate the handoff reaching a terminal state between runs (e.g. shipped
    # by hand, or by an unrelated verb) — deployment_state:shipped removes it
    # from _is_open's admitted set, same predicate the op itself uses.
    path = Path(handoff_repo.abs_path("2026-01-01-d1-terminal.md"))
    text = path.read_text(encoding="utf-8")
    terminal_text = text.replace("deployment_state: open", "deployment_state: shipped", 1)
    assert terminal_text != text
    path.write_text(terminal_text, encoding="utf-8")

    result2 = _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))
    assert result2["exit_code"] == 0
    assert result2["conservation_violations"] == []


def test_history_persisted_at_documented_location(handoff_repo):
    _seed_ambiguous(handoff_repo, "2026-01-01-d1-location.md", "nonexistent-d.py")
    _run(_handler({"dry_run": True}, repo_root=handoff_repo.common_dir))

    history_path = _history_path(handoff_repo.common_dir)
    assert history_path.is_file()
    persisted, load_error = _load_surfaced_history(history_path)
    assert load_error is None
    assert "2026-01-01-d1-location" in persisted
