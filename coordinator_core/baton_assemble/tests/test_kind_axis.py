"""
coordinator_core.baton_assemble.tests.test_kind_axis -- C7 of
docs/plans/2026-08-19-batons-unify-into-one-successor.md ("stamp the role
axis at every baton-authoring path").

Purpose: WITHOUT a writer, the `baton_role` inheritability axis
(`handoff.schema.json :: baton_role`, DoE-ratified `work | record`,
cross-repo/inbox/2026-08-19-doe-claude-em-baton-role-axis-ruling.md) is unset
on every record, and C8/the unification held-set target a field nothing ever
stamps -- the path-shape heuristic they replace would be strictly better
than the replacement. This module pins the two authoring paths
(`coordinator-doc-new --type handoff|spinoff`, called both directly and via
`baton_assemble.apply`'s d1 directive) each stamp `baton_role: work`
unconditionally, that `handoff.normalize` never backfills the field onto an
existing unstamped record (mirroring the `producer` no-backfill discipline),
that the field reads as UNKNOWN/not-inheritable when absent (the predicate
`pickup_assemble._role_axis_is_unknown` new consumers gate on), and that
`/pickup` admitting an operator-NAMED unstamped legacy baton is unaffected --
this field is not a second gate on the operator's own instruction.

Coverage:
  - `_scaffold_handoff` and `_scaffold_spinoff` (loaded off
    `coordinator/bin/coordinator-doc-new.py` by file path -- same idiom
    `coordinator/bin/tests/test_coordinator_doc_new_placeholder_id_mint.py`
    uses, since the entrypoint is an extensionless-shaped `.py` script) each
    emit `baton_role: work` unconditionally, with no flag required.
  - `handoff.normalize` (`_normalize_one_text`, the pure per-file
    normalizer) leaves an ABSENT `baton_role` absent -- never backfills it,
    mirroring the `producer` field's own caller-supplied-only discipline.
  - `pickup_assemble._role_axis_is_unknown` -- the predicate the new
    unification/mise-inheritance consumers gate on -- reads absence (and
    every null-ish string this repo's line-parser can hand back) as unknown,
    and reads `"work"`/`"record"` as known.
  - `pickup_assemble.brief()` admitting an operator-NAMED, unstamped legacy
    baton by explicit path succeeds exactly as it does today -- this field
    is not a second gate on the operator's own instruction.

Spec backlink: docs/plans/2026-08-19-batons-unify-into-one-successor.md § C7
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
from coordinator_core.ops.handoff_normalize import _normalize_one_text
from coordinator_core.pickup_assemble import _role_axis_is_unknown

_BIN_DIR = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


def _load_doc_new_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_kind_axis_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_doc_new_kind_axis_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_doc_new_module()


class TestAuthoringPathsStampWork:
    """Both baton-shaped scaffolders (`--type handoff`, `--type spinoff`) --
    the only two `baton_assemble.KINDS` -- stamp `baton_role: work`
    unconditionally; no flag threads a different value, per the plan body's
    "successor and continuation batons stamp work"."""

    def test_scaffold_handoff_stamps_work(self):
        rendered = _MOD._scaffold_handoff(title="A continuation baton", branch="work/x/2026-08-19")
        assert "baton_role: work\n" in rendered
        assert "kind: session-handoff\n" in rendered

    def test_scaffold_spinoff_stamps_work(self):
        rendered = _MOD._scaffold_spinoff(title="A spinoff baton", branch="work/x/2026-08-19")
        assert "baton_role: work\n" in rendered
        assert "kind: spinoff\n" in rendered


class TestNormalizeNeverBackfills:
    """Mirrors the `producer` field's own no-backfill discipline
    (`handoff_normalize._normalize_one_text`'s docstring): a sweep stamping
    `work` across the legacy corpus would convert honestly-unknown into a
    false assertion in one commit."""

    def test_normalize_leaves_absent_baton_role_absent(self, tmp_path):
        content = (
            "---\n"
            'title: "Legacy Handoff"\n'
            "created: 2026-01-01\n"
            "branch: work/legacy/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            "---\n\n# Handoff\n\nBody.\n"
        )
        file_path = tmp_path / "state" / "handoffs" / "legacy.md"
        result = _normalize_one_text(content, file_path)
        rebuilt = result["rebuilt"] if result is not None else content
        assert "baton_role" not in rebuilt


class TestRoleAxisUnknownPredicate:
    """`pickup_assemble._role_axis_is_unknown` -- the predicate the
    unification held-set and mise-inheritance set gate on -- reads absence
    (never a defaulted `.get`) and every null-ish string this repo's
    line-parser can hand back as unknown; `work`/`record` are known."""

    @pytest.mark.parametrize("raw", [None, "null", "None", "~", "", "  "])
    def test_absent_or_null_ish_is_unknown(self, raw):
        assert _role_axis_is_unknown(raw) is True

    @pytest.mark.parametrize("raw", ["work", "record"])
    def test_stamped_value_is_known(self, raw):
        assert _role_axis_is_unknown(raw) is False


class TestPickupOfUnstampedLegacyBatonUnaffected:
    """An operator naming a path is the authority; `baton_role` is not a
    second gate on that instruction. `brief()` over an unstamped legacy
    handoff, addressed explicitly by the operator, must succeed exactly as
    it did before this axis existed."""

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_brief_admits_operator_named_unstamped_legacy_baton(self, tmp_path: Path) -> None:
        target = tmp_path / "state" / "handoffs" / "legacy.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            'title: "Legacy Handoff"\n'
            "created: 2026-01-01\n"
            "branch: work/legacy/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            "---\n\n# Handoff\n\nBody.\n",
            encoding="utf-8",
        )
        result = pa.brief("state/handoffs/legacy.md", repo_root=tmp_path)
        assert result.decision_object["artifact"]["path"] == "state/handoffs/legacy.md"
