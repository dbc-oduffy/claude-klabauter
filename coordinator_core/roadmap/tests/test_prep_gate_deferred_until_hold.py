"""A `deferred_until` row is unschedulable to the prep gate, as it is to `read_spine`.

A pin hold withholds a row from every wave, so an unresolvable declaration on it
(a placeholder path) is moot; removing the hold changes the body sha and stales
any stamp taken while it stood.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg

_FM = """census: []
prime_exit_criterion:
  statement: the hold certifies withheld
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_HOLD = "  deferred_until: pin-lands\n"


def _spine(hold: str) -> str:
    return (
        "- id: C1\n  title: t\n  body: Do the thing.\n  change_kind: code-edit\n"
        "  surface: docs/x.md\n"
        "  writes: ['<surface-resolved-in-chunk>']\n"
        "  external_gate:\n"
        "    - owner_repo: example-retrieval-repo\n"
        "      condition: they land the op\n"
        "      requires: landed-work\n"
        f"{hold}"
        "  queue_scope: project\n  disposition: open\n"
    )


def _plan(root: Path, hold: str) -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / "2026-09-07-fixture.md"
    path.write_text(
        "---\ntitle: fixture\nstatus: draft\ncreated: 2026-09-07\n"
        "author: fixture-session\n" + _FM + "---\n\n# Fixture\n\n## Tasks\n\n"
        "```yaml plan-tasks\n" + _spine(hold).strip() + "\n```\n",
        encoding="utf-8",
    )
    return path


def test_a_held_row_with_a_placeholder_path_is_prepped(tmp_path):
    report = pg.gate_plan(tmp_path, _plan(tmp_path, _HOLD))
    assert report["verdict"] == pg.PREPPED, report["message"]


def test_the_identical_row_without_the_hold_is_not_prepped(tmp_path):
    report = pg.gate_plan(tmp_path, _plan(tmp_path, ""))
    assert report["verdict"] == pg.NOT_PREPPED


def test_an_empty_hold_does_not_withhold(tmp_path):
    report = pg.gate_plan(tmp_path, _plan(tmp_path, "  deferred_until: ''\n"))
    assert report["verdict"] == pg.NOT_PREPPED


def test_removing_the_hold_stales_an_earlier_stamp(tmp_path):
    held = _plan(tmp_path, _HOLD).read_text(encoding="utf-8")
    body_sha = pg.read_stamp(held)["body_sha"]
    stamp = (
        "mise_prepped_by: t\nmise_prepped_at: 2026-09-07\n"
        f"mise_prepped_sha: {body_sha}\nmise_prepped_findings: []\n"
    )
    stamped = held.replace("census: []\n", "census: []\n" + stamp, 1)
    assert pg.read_stamp(stamped)["state"] == pg.CERTIFIED
    unheld = stamped.replace(_HOLD, "")
    assert unheld != stamped
    assert pg.read_stamp(unheld)["state"] == pg.STALE
