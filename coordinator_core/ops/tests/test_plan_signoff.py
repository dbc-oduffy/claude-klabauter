"""coordinator_core/ops/tests/test_plan_signoff.py — "plan.signoff": rows, groupings, countersign, refusals."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops import plan_signoff as mod

PLAN = """---
title: t
created: 2026-10-09
author: a
status: executing
plan_id: pln-x-111111
grouping_approvals:
  # kept comment
  do:
    status: pending
  spun_off:
    status: pending
    approver: null
---

## Tasks

```yaml plan-tasks
- id: A1
  title: first
  change_kind: code-edit
  surface: a.py
  writes: [a.py]
  disposition: open
- id: A2
  title: second
  change_kind: code-edit
  surface: b.py
  writes: [b.py]
  disposition: open
- id: S1
  title: spun
  disposition: spun_off
  disposition_detail: moved elsewhere
```
"""

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RULING = "Angelique ruled the rows may proceed"


@pytest.fixture
def root(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True, **no_console_creationflags())
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/p.md").write_text(PLAN, encoding="utf-8")
    monkeypatch.setattr(mod, "main_worktree_root", lambda r: r)
    return tmp_path


def _call(root, target, source="pm", **kw):
    params = {"plan_path": "docs/plans/p.md", "target": target, "source": source, "on": "2026-10-09", **kw}
    if source == "pm":
        params.setdefault("pm_quote", "yes do it")
    else:
        params.setdefault("apm_ruling", RULING)
        params.setdefault("ruling_ref", "docs/plans/p.md")
    return mod._handler(params, root)


def _text(root):
    return (root / "docs/plans/p.md").read_text(encoding="utf-8")


def _rows(root):
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(_text(root)).body)}


def _fm(root):
    return parse_frontmatter(_text(root))["frontmatter"]


def test_row_pm_signoff(root):
    res = _call(root, {"rows": ["A1"]})
    assert res["applied"] is True
    row = _rows(root)["A1"]
    assert row["pm_approved"] is True
    assert row["signoff"] == {"source": "pm", "pm_quote": "yes do it", "on": "2026-10-09"}
    assert "pm_approved" not in _rows(root)["A2"]


def test_row_apm_signoff(root):
    _call(root, {"rows": ["A1", "A2"]}, source="apm")
    sig = _rows(root)["A2"]["signoff"]
    assert sig["source"] == "apm" and sig["apm_ruling"] == RULING and sig["ruling_ref"] == "docs/plans/p.md"


def test_row_countersign_keeps_apm_in_history(root):
    _call(root, {"rows": ["A1"]}, source="apm")
    _call(root, {"rows": ["A1"]}, pm_quote="verified")
    sig = _rows(root)["A1"]["signoff"]
    assert sig["source"] == "pm" and sig["pm_quote"] == "verified"
    assert [h["source"] for h in sig["history"]] == ["apm"]
    assert sig["history"][0]["apm_ruling"] == RULING


def test_apm_over_pm_refused(root):
    _call(root, {"rows": ["A1"]})
    before = _text(root)
    with pytest.raises(ValueError, match="already signed off by the PM"):
        _call(root, {"rows": ["A1"]}, source="apm")
    assert _text(root) == before


def test_apm_over_apm_refused(root):
    _call(root, {"rows": ["A1"]}, source="apm")
    with pytest.raises(ValueError, match="already signed off by the APM"):
        _call(root, {"rows": ["A1"]}, source="apm")


def test_irreversible_ruling_refused(root):
    before = _text(root)
    with pytest.raises(ValueError, match="irreversible"):
        _call(root, {"rows": ["A1"]}, source="apm", apm_ruling="ok to merge to main")
    assert _text(root) == before


def test_apm_requires_ruling_ref(root):
    with pytest.raises(ValueError, match="ruling_ref"):
        _call(root, {"rows": ["A1"]}, source="apm", ruling_ref="")


def test_absolute_ruling_ref_refused(root):
    with pytest.raises(ValueError, match="absolute"):
        _call(root, {"rows": ["A1"]}, source="apm", ruling_ref="/etc/ruling.md")


def test_batch_is_atomic(root):
    before = _text(root)
    with pytest.raises(ValueError, match="not found"):
        _call(root, {"rows": ["A1", "NOPE"]})
    assert _text(root) == before


def test_untouched_rows_keep_their_bytes(root):
    _call(root, {"rows": ["A1"]})
    assert "- id: A2\n  title: second\n  change_kind: code-edit\n  surface: b.py\n  writes: [b.py]\n" in _text(root)


def test_grouping_pm_signoff(root):
    res = _call(root, {"grouping": "spun_off"}, pm_quote="spin it off")
    assert res["applied"] is True
    block = _fm(root)["grouping_approvals"]["spun_off"]
    assert block["status"] == "approved" and block["approver"] == "PM"
    assert block["pm_utterance"] == "spin it off"
    assert block["digest"].startswith("sha256:")
    assert block["signoff"]["source"] == "pm"
    assert "# kept comment" in _text(root)
    assert _fm(root)["grouping_approvals"]["do"] == {"status": "pending"}


def test_grouping_apm_then_pm_countersign(root):
    _call(root, {"grouping": "spun_off"}, source="apm")
    block = _fm(root)["grouping_approvals"]["spun_off"]
    assert block["approver"] == "APM" and block["pm_utterance"] == RULING
    _call(root, {"grouping": "spun_off"}, pm_quote="countersigned")
    block = _fm(root)["grouping_approvals"]["spun_off"]
    assert block["approver"] == "PM" and block["pm_utterance"] == "countersigned"
    assert block["signoff"]["history"][0]["source"] == "apm"


@pytest.mark.parametrize("source", ["pm", "apm"])
def test_a_grouping_this_op_signs_passes_the_grouping_check(root, source):
    from coordinator_core.frontmatter.schema_validate import _grouping_signoff_error

    _call(root, {"grouping": "spun_off"}, **({"source": "apm"} if source == "apm" else {}))
    assert _grouping_signoff_error("spun_off", _fm(root)["grouping_approvals"]["spun_off"]) is None


@pytest.mark.parametrize("source,approver", [("g-em", "G-EM"), ("uhura", "Uhura")])
def test_grouping_delegate_role_signoff(root, source, approver):
    from coordinator_core.frontmatter.schema_validate import _grouping_signoff_error
    from coordinator_core.ops.signoff_digest import _bucket
    from coordinator_core.ops.signoff_provenance import read_grouping

    mod._handler({"plan_path": "docs/plans/p.md", "target": {"grouping": "spun_off"}, "source": source,
                  "approver": "claude-klabauter-6e", "ruling": "spin it off", "ruling_ref": "docs/plans/p.md",
                  "on": "2026-10-09"}, root)
    block = _fm(root)["grouping_approvals"]["spun_off"]
    assert block["approver"] == approver and block["pm_utterance"] == "spin it off"
    assert block["signoff"] == {"source": source, "approver": "claude-klabauter-6e", "ruling": "spin it off",
                                "ruling_ref": "docs/plans/p.md", "on": "2026-10-09"}
    assert _grouping_signoff_error("spun_off", block) is None
    assert _bucket(read_grouping(block)) == "delegated"
    _call(root, {"grouping": "spun_off"}, pm_quote="countersigned")
    block = _fm(root)["grouping_approvals"]["spun_off"]
    assert block["approver"] == "PM" and block["signoff"]["source"] == "pm"
    assert block["signoff"]["history"] == [{"source": source, "approver": "claude-klabauter-6e",
                                            "ruling": "spin it off", "ruling_ref": "docs/plans/p.md",
                                            "on": "2026-10-09"}]
    assert _grouping_signoff_error("spun_off", block) is None
    with pytest.raises(ValueError, match="already signed off by the PM"):
        mod._handler({"plan_path": "docs/plans/p.md", "target": {"grouping": "spun_off"}, "source": source,
                      "approver": "x", "ruling": "again", "ruling_ref": "docs/plans/p.md"}, root)


def test_delegate_role_on_rows_refused(root):
    with pytest.raises(ValueError, match="grouping only"):
        mod._handler({"plan_path": "docs/plans/p.md", "target": {"rows": ["A1"]}, "source": "g-em",
                      "approver": "x", "ruling": "ok", "ruling_ref": "docs/plans/p.md"}, root)


def test_delegate_role_requires_approver(root):
    with pytest.raises(ValueError, match="approver"):
        mod._handler({"plan_path": "docs/plans/p.md", "target": {"grouping": "spun_off"}, "source": "uhura",
                      "ruling": "ok", "ruling_ref": "docs/plans/p.md"}, root)


def test_grouping_apm_over_pm_refused(root):
    _call(root, {"grouping": "spun_off"})
    with pytest.raises(ValueError, match="already signed off by the PM"):
        _call(root, {"grouping": "spun_off"}, source="apm")


def test_grouping_stale_digest_refused(root):
    before = _text(root)
    with pytest.raises(ValueError, match="digest"):
        _call(root, {"grouping": "spun_off"}, digest="sha256:" + "0" * 64)
    assert _text(root) == before


def test_grouping_digest_match_accepted(root):
    from coordinator_core.frontmatter.schema_validate import compute_grouping_digest

    digest = compute_grouping_digest(list(_rows(root).values()), "spun_off")
    assert _call(root, {"grouping": "spun_off"}, digest=digest)["applied"] is True


def test_target_shape_refused(root):
    with pytest.raises(ValueError, match="exactly one"):
        _call(root, {"rows": ["A1"], "grouping": "do"})
    with pytest.raises(ValueError, match="digest applies"):
        _call(root, {"rows": ["A1"]}, digest="sha256:" + "0" * 64)
