"""
coordinator_core.ops.tests.test_handoff_normalize

Tests for the handoff.normalize op (coordinator_core/ops/handoff_normalize.py).

Import guard: coordinator_core.ops.handoff_normalize MUST be imported at module
load time to fire the @register_op("handoff.normalize") side-effect and populate
_REGISTRY.  Without this the decorator has not run and the registry assertion
passes vacuously over an empty (or incomplete) registry.

Coverage:
  (a) registry     — op name present in _REGISTRY after import
  (b) dry-run      — write=False (default) does not modify files; reports drift
  (c) write        — write=True modifies files on disk
  (d) rule 1       — created: ISO time stripped to bare YYYY-MM-DD
  (e) rule 2       — pickup_ready: quoted "true"/"false" → bare bool
  (f) rule 3       — category: backfill when absent; keyword heuristic fires correctly
  (g) rule 4       — summary: backfill from first H1; inline markdown stripped; 140-cap
  (h) rule 5       — deliverable_id: minted when absent (structural shape); carried when present
  (i) rule 6       — initiative: inserted as null when absent; carried when present
  (j) idempotent   — second run on an already-normalized file returns no changes
  (k) multi-file   — multiple files in state/handoffs/ processed in one call
  (l) no-repo-root — exit_code 2 when repo_root is absent (params.root fallback
      removed entirely — op-family path-containment sweep, 2026-07-08 § 1c)

Spec backlink: coordinator/bin/normalize-handoff-frontmatter.js (port source)
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_normalize  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.handoff_normalize import _handler, _match_category
from coordinator_core.session import claims as session_claims

# Positive floor assertion: the op must be registered before any test runs.
_OP_NAME = "handoff.normalize"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.handoff_normalize @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio required."""
    return asyncio.run(coro)


# Minimal valid handoff frontmatter with NO normalizable fields
# (all six fields already present and clean).
_CLEAN_HANDOFF = """\
---
title: My Handoff
created: 2026-07-01
pickup_ready: false
category: infra
summary: Already normalized summary text here.
deliverable_id: dlv-2026-07-01-my-handoff-abc123
initiative: null
---

# My Handoff

Body text.
"""

# Handoff with ALL six drift conditions present.
_DIRTY_HANDOFF = """\
---
title: Research spike investigation
created: 2026-07-01T12:34:56Z
pickup_ready: "true"
---

# My Research Handoff

Body text.
"""


# ---------------------------------------------------------------------------
# (a) Registry completeness
# ---------------------------------------------------------------------------


def test_registry_contains_op():
    """handoff.normalize must be present in _REGISTRY after module import."""
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} not in _REGISTRY — check @register_op decorator"
    )


# ---------------------------------------------------------------------------
# (b) dry-run: no disk writes, drift reported
# ---------------------------------------------------------------------------


def test_dry_run_reports_drift_without_writing(norm_repo):
    """write=False (dry-run default): reports drift but does NOT write files."""
    norm_repo.seed_handoff("2026-07-01-dirty.md", _DIRTY_HANDOFF)
    original = norm_repo.read_handoff("2026-07-01-dirty.md")

    result = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert len(result["changed"]) == 1
    assert result["errors"] == []

    # File must be UNCHANGED on disk.
    after = norm_repo.read_handoff("2026-07-01-dirty.md")
    assert after == original, "dry-run must not modify files on disk"


def test_dry_run_default_write_absent(norm_repo):
    """write param absent defaults to dry-run behavior."""
    norm_repo.seed_handoff("2026-07-01-dirty2.md", _DIRTY_HANDOFF)
    original = norm_repo.read_handoff("2026-07-01-dirty2.md")

    result = _run(_handler({}, repo_root=norm_repo.common_dir))

    assert result["dry_run"] is True
    assert result["applied"] is False
    assert norm_repo.read_handoff("2026-07-01-dirty2.md") == original


# ---------------------------------------------------------------------------
# (c) write=True applies changes to disk
# ---------------------------------------------------------------------------


def test_write_true_modifies_file(norm_repo):
    """write=True writes normalized content to disk and reports applied=True."""
    norm_repo.seed_handoff("2026-07-01-write.md", _DIRTY_HANDOFF)

    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    assert result["dry_run"] is False
    assert len(result["changed"]) == 1
    assert result["errors"] == []

    # Verify disk was modified.
    after = norm_repo.read_handoff("2026-07-01-write.md")
    assert after != _DIRTY_HANDOFF, "file must be modified by write=True"
    # Normalized file must have valid frontmatter delimiters.
    assert after.startswith("---\n")
    assert "---\n" in after[3:]


# ---------------------------------------------------------------------------
# (d) Rule 1 — created: strip ISO time component
# ---------------------------------------------------------------------------


def test_rule_created_strips_iso_time(norm_repo):
    """Rule 1: created: 2026-07-01T12:34:56Z → 2026-07-01."""
    content = """\
---
title: T
created: 2026-07-01T12:34:56Z
---

# T

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r1.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert result["exit_code"] == 0

    after = norm_repo.read_handoff("2026-07-01-r1.md")
    assert "created: 2026-07-01\n" in after, (
        "ISO time must be stripped — expected 'created: 2026-07-01'"
    )
    assert "T12:" not in after, "time component must not remain in the created field"

    # Change entry should mention the rule.
    changes = result["changed"][0]["changes"]
    assert any("created" in c for c in changes), f"no created change entry; got {changes!r}"


def test_rule_created_bare_date_unchanged(norm_repo):
    """Rule 1: bare YYYY-MM-DD date is already clean — no change."""
    content = """\
---
title: T
created: 2026-07-01
---

# T

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r1-clean.md", content)
    result = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))

    # May have other changes (category, summary, etc.) but NOT a created change.
    if result["changed"]:
        changes = result["changed"][0]["changes"]
        assert not any(c.startswith("created:") for c in changes), (
            "bare date must not trigger a created change"
        )


# ---------------------------------------------------------------------------
# (e) Rule 2 — pickup_ready: unquote boolean strings
# ---------------------------------------------------------------------------


def test_rule_pickup_ready_unquotes_double_quoted_true(norm_repo):
    """Rule 2: pickup_ready: \"true\" → pickup_ready: true (no quotes)."""
    content = """\
---
title: T
pickup_ready: "true"
---

# T

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r2-true.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert result["exit_code"] == 0

    after = norm_repo.read_handoff("2026-07-01-r2-true.md")
    assert 'pickup_ready: true\n' in after, (
        "double-quoted 'true' must be unquoted"
    )
    assert 'pickup_ready: "true"' not in after


def test_rule_pickup_ready_unquotes_double_quoted_false(norm_repo):
    """Rule 2: pickup_ready: \"false\" → pickup_ready: false (no quotes)."""
    content = """\
---
title: T
pickup_ready: "false"
---

# T

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r2-false.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r2-false.md")
    assert 'pickup_ready: false\n' in after


def test_rule_pickup_ready_bare_bool_unchanged(norm_repo):
    """Rule 2: bare pickup_ready: true is already clean — no change recorded."""
    content = """\
---
title: T
pickup_ready: true
---

# T

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r2-bare.md", content)
    result = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))
    if result["changed"]:
        changes = result["changed"][0]["changes"]
        assert not any("pickup_ready" in c for c in changes), (
            "bare bool must not trigger a pickup_ready change"
        )


# ---------------------------------------------------------------------------
# (f) Rule 3 — category: backfill with keyword heuristic
# ---------------------------------------------------------------------------


def test_rule_category_backfilled_when_absent(norm_repo):
    """Rule 3: category absent → backfilled (at minimum to 'uncategorized')."""
    content = """\
---
title: Some unrelated title
---

# H

B.
"""
    norm_repo.seed_handoff("2026-07-01-r3-basic.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    after = norm_repo.read_handoff("2026-07-01-r3-basic.md")
    assert "category: " in after, "category field must be inserted when absent"
    changes = result["changed"][0]["changes"]
    assert any("category" in c for c in changes)


def test_rule_category_keyword_research(norm_repo):
    """Rule 3 heuristic: 'research' keyword in title → category: research."""
    content = """\
---
title: Deep research spike investigation
---

# H

B.
"""
    norm_repo.seed_handoff("2026-07-01-r3-research.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r3-research.md")
    assert "category: research\n" in after


def test_rule_category_keyword_bug(norm_repo):
    """Rule 3 heuristic: 'bug' keyword in title → category: bug."""
    content = """\
---
title: Fix bug in hotfix path
---

# H

B.
"""
    norm_repo.seed_handoff("2026-07-01-r3-bug.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r3-bug.md")
    assert "category: bug\n" in after


def test_rule_category_keyword_infra(norm_repo):
    """Rule 3 heuristic: 'install' keyword in title → category: infra."""
    content = """\
---
title: install and configure the pipeline hook
---

# H

B.
"""
    norm_repo.seed_handoff("2026-07-01-r3-infra.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r3-infra.md")
    assert "category: infra\n" in after


def test_rule_category_present_unchanged(norm_repo):
    """Rule 3: category already present — not overwritten."""
    content = """\
---
title: Some research title
category: infra
---

# H

B.
"""
    norm_repo.seed_handoff("2026-07-01-r3-present.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r3-present.md")
    # category must still be infra (not overridden by heuristic)
    assert "category: infra\n" in after
    # No category change entry.
    if result["changed"]:
        changes = result["changed"][0]["changes"]
        assert not any(c.startswith("category:") for c in changes)


# ---------------------------------------------------------------------------
# (g) Rule 4 — summary: backfill from first H1; strip markdown; 140-cap
# ---------------------------------------------------------------------------


def test_rule_summary_backfilled_from_h1(norm_repo):
    """Rule 4: summary absent → backfilled from first H1 in body."""
    content = """\
---
title: T
category: infra
---

# My Handoff Title

Body text.
"""
    norm_repo.seed_handoff("2026-07-01-r4-h1.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r4-h1.md")
    assert "summary: My Handoff Title\n" in after


def test_rule_summary_strips_inline_markdown(norm_repo):
    """Rule 4: inline markdown stripped from H1 before inserting as summary."""
    content = """\
---
title: T
category: infra
---

# **Bold** and `code` and [link](http://example.com)

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r4-md.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r4-md.md")
    # Bold/code/link stripped
    assert "**" not in after.split("---")[1]  # not in frontmatter section
    # The summary line should have the stripped text
    assert "Bold and code and link" in after


def test_rule_summary_truncates_at_140(norm_repo):
    """Rule 4: summary text > 140 chars is truncated to 137 + '...'."""
    long_title = "A" * 145
    content = f"""\
---
title: T
category: infra
---

# {long_title}

Body.
"""
    norm_repo.seed_handoff("2026-07-01-r4-trunc.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r4-trunc.md")
    # summary line must end with ...
    lines = after.splitlines()
    summary_lines = [l for l in lines if l.startswith("summary: ")]
    assert len(summary_lines) == 1
    summary_val = summary_lines[0][len("summary: "):]
    assert summary_val.endswith("..."), "truncated summary must end with '...'"
    assert len(summary_val) <= 140, f"summary must be ≤140 chars; got {len(summary_val)}"


def test_rule_summary_falls_back_to_title_when_no_h1(norm_repo):
    """Rule 4: no H1 in body → summary backfilled from title: field."""
    content = """\
---
title: Fallback from title field
category: infra
---

No H1 here, just body text.
"""
    norm_repo.seed_handoff("2026-07-01-r4-title.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    after = norm_repo.read_handoff("2026-07-01-r4-title.md")
    assert "summary: Fallback from title field\n" in after


# ---------------------------------------------------------------------------
# (h) Rule 5 — deliverable_id: mint when absent; carry when present
# ---------------------------------------------------------------------------


_DLV_PATTERN = re.compile(r'^dlv-[^-]+-[0-9a-f]{6}$')


def test_rule_deliverable_id_minted_when_absent(norm_repo):
    """Rule 5: deliverable_id absent → minted with dlv-<slug>-<6hex> structure."""
    content = """\
---
title: T
category: infra
---

# T

B.
"""
    norm_repo.seed_handoff("2026-07-01-my-handoff.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert result["exit_code"] == 0

    after = norm_repo.read_handoff("2026-07-01-my-handoff.md")
    dlv_lines = [l for l in after.splitlines() if l.startswith("deliverable_id: ")]
    assert len(dlv_lines) == 1, "exactly one deliverable_id line must be present"

    dlv_val = dlv_lines[0][len("deliverable_id: "):]
    assert dlv_val.startswith("dlv-"), f"deliverable_id must start with 'dlv-'; got {dlv_val!r}"
    # Structural check: dlv-<slug>-<6hex> — at minimum starts with dlv- and ends with 6 hex chars.
    assert re.match(r'^dlv-.+-[0-9a-f]{6}$', dlv_val), (
        f"deliverable_id structural shape mismatch; got {dlv_val!r}"
    )
    # Slug is derived from the file basename (without .md).
    assert "2026-07-01-my-handoff" in dlv_val, (
        f"slug must appear in minted id; got {dlv_val!r}"
    )

    # Change entry must mention the mint.
    changes = result["changed"][0]["changes"]
    assert any("deliverable_id" in c and "mint-from-slug" in c for c in changes)


def test_rule_deliverable_id_mint_non_deterministic_structure(norm_repo):
    """Rule 5: two separate mints for the same file produce different hex suffixes (probabilistic)."""
    content = """\
---
title: T
category: infra
---

# T

B.
"""
    norm_repo.seed_handoff("2026-07-02-unique.md", content)

    result1 = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))
    result2 = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))

    assert result1["changed"] and result2["changed"]
    # Both minted ids are dlv-<slug>-<hex> shaped but may differ (non-deterministic).
    changes1 = result1["changed"][0]["changes"]
    changes2 = result2["changed"][0]["changes"]
    dlv1 = next(c for c in changes1 if "deliverable_id" in c)
    dlv2 = next(c for c in changes2 if "deliverable_id" in c)
    # Both have the right shape prefix.
    assert "dlv-2026-07-02-unique-" in dlv1, f"unexpected shape: {dlv1!r}"
    assert "dlv-2026-07-02-unique-" in dlv2, f"unexpected shape: {dlv2!r}"


def test_rule_deliverable_id_carry_when_present(norm_repo):
    """Rule 5 (D1 carry): existing deliverable_id is NOT replaced."""
    original_id = "dlv-2026-07-01-carried-abcdef"
    content = f"""\
---
title: T
category: infra
deliverable_id: {original_id}
---

# T

B.
"""
    norm_repo.seed_handoff("2026-07-01-carried.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    after = norm_repo.read_handoff("2026-07-01-carried.md")
    assert f"deliverable_id: {original_id}\n" in after, (
        "existing deliverable_id must be carried unchanged (D1 carry rule)"
    )
    # No deliverable_id entry in changes (carry = no drift).
    if result["changed"]:
        changes = result["changed"][0]["changes"]
        assert not any("deliverable_id" in c for c in changes), (
            "carry path must NOT emit a changes entry"
        )


# ---------------------------------------------------------------------------
# AC5 (docs/plans/2026-08-01-deliverable-id-carry-onto-executing-handoff.md
# § C2/C3): deliverable_id, when absent on the handoff itself, is carried
# from the RUNNING SESSION's claimed plan before falling back to
# mint-from-slug -- the DR-207 DD#1 "second door" close.
# ---------------------------------------------------------------------------


def test_rule_deliverable_id_carries_claimed_plans_id_when_resolvable(norm_repo, monkeypatch):
    """AC5, first half: a claimed plan whose frontmatter carries a
    deliverable_id is carried onto a handoff whose OWN deliverable_id is
    absent -- not re-minted from the handoff's filename."""
    plan_slug = "2026-08-01-ac5-claimed-plan"
    plan_path = norm_repo.root / "docs" / "plans" / f"{plan_slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "---\ntitle: AC5 Plan\ndeliverable_id: dlv-ac5-claimed-plan-abc123\n---\n\n# AC5 Plan\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac5-carry")
    assert session_claims.claim_plan(plan_slug, cwd=str(norm_repo.root)) is True

    norm_repo.seed_handoff("2026-08-01-ac5-no-id.md", _DIRTY_HANDOFF)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    after = norm_repo.read_handoff("2026-08-01-ac5-no-id.md")
    assert "deliverable_id: dlv-ac5-claimed-plan-abc123\n" in after

    changes = result["changed"][0]["changes"]
    assert any(
        "deliverable_id" in c and "carry-from-claimed-plan" in c for c in changes
    )


def test_rule_deliverable_id_still_mints_from_slug_when_no_plan_claimed(norm_repo, monkeypatch):
    """AC5, second half: a session with no claimed plan keeps today's
    unchanged mint-from-slug behaviour -- an over-eager carry fix must not
    break a handoff genuinely unattached to any plan."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac5-no-claim")
    norm_repo.seed_handoff("2026-08-01-ac5-still-mints.md", _DIRTY_HANDOFF)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    after = norm_repo.read_handoff("2026-08-01-ac5-still-mints.md")
    dlv_lines = [l for l in after.splitlines() if l.startswith("deliverable_id: ")]
    assert len(dlv_lines) == 1
    assert dlv_lines[0].startswith("deliverable_id: dlv-2026-08-01-ac5-still-mints-")

    changes = result["changed"][0]["changes"]
    assert any(
        "deliverable_id" in c and "mint-from-slug" in c for c in changes
    )


# ---------------------------------------------------------------------------
# (i) Rule 6 — initiative: present-as-null when absent; carry when present
# ---------------------------------------------------------------------------


def test_rule_initiative_inserted_as_null_when_absent(norm_repo):
    """Rule 6 (D9): initiative absent → inserted as 'initiative: null'."""
    content = """\
---
title: T
category: infra
---

# T

B.
"""
    norm_repo.seed_handoff("2026-07-01-r6-null.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    after = norm_repo.read_handoff("2026-07-01-r6-null.md")
    assert "initiative: null\n" in after, (
        "initiative must be inserted as bare YAML null (D9 present-as-null)"
    )
    changes = result["changed"][0]["changes"]
    assert any("initiative" in c and "null" in c for c in changes)


def test_rule_initiative_carry_when_present_with_value(norm_repo):
    """Rule 6: initiative already present (non-null) — not overwritten."""
    content = """\
---
title: T
category: infra
initiative: my-initiative
---

# T

B.
"""
    norm_repo.seed_handoff("2026-07-01-r6-present.md", content)
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    after = norm_repo.read_handoff("2026-07-01-r6-present.md")
    assert "initiative: my-initiative\n" in after, "present initiative must be carried"
    # No initiative change entry.
    if result["changed"]:
        changes = result["changed"][0]["changes"]
        assert not any(c.startswith("initiative:") for c in changes)


def test_rule_initiative_carry_when_already_null(norm_repo):
    """Rule 6: initiative: null already present — idempotent, no change emitted."""
    content = """\
---
title: T
category: infra
summary: S
deliverable_id: dlv-x-abc123
initiative: null
---

# T

B.
"""
    norm_repo.seed_handoff("2026-07-01-r6-null-present.md", content)
    result = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))
    # File is already fully normalized — no changes should be reported.
    if result["changed"]:
        changes = result["changed"][0]["changes"]
        assert not any(c.startswith("initiative:") for c in changes)


# ---------------------------------------------------------------------------
# (j) Idempotent second run
# ---------------------------------------------------------------------------


def test_idempotent_second_run_produces_no_changes(norm_repo):
    """Second run on a fully-normalized file returns no changes (idempotent)."""
    norm_repo.seed_handoff("2026-07-01-idem.md", _DIRTY_HANDOFF)

    # First run: apply all normalizations.
    first = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert first["changed"], "first run must detect and apply drift"
    assert first["applied"] is True

    # Second run: file is already normalized — nothing to do.
    second = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert second["changed"] == [], (
        f"second run must find no drift; got changes: {second['changed']!r}"
    )
    assert second["applied"] is False
    assert second["exit_code"] == 0


def test_idempotent_clean_file_produces_no_changes(norm_repo):
    """File with all six fields already correct: zero changes on any run."""
    norm_repo.seed_handoff("2026-07-01-clean.md", _CLEAN_HANDOFF)

    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert result["changed"] == [], (
        f"already-clean file must produce no changes; got {result['changed']!r}"
    )
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (k) Multi-file: multiple handoffs processed in a single call
# ---------------------------------------------------------------------------


def test_multi_file_all_processed(norm_repo):
    """Multiple files in state/handoffs/ are each normalized in one call."""
    dirty1 = """\
---
title: Alpha
created: 2026-06-01T10:00:00Z
---

# Alpha

Body.
"""
    dirty2 = """\
---
title: Beta
pickup_ready: "false"
---

# Beta

Body.
"""
    norm_repo.seed_handoff("2026-07-01-alpha.md", dirty1)
    norm_repo.seed_handoff("2026-07-01-beta.md", dirty2)

    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    changed_files = {c["file"] for c in result["changed"]}
    assert "state/handoffs/2026-07-01-alpha.md" in changed_files
    assert "state/handoffs/2026-07-01-beta.md" in changed_files

    alpha = norm_repo.read_handoff("2026-07-01-alpha.md")
    beta = norm_repo.read_handoff("2026-07-01-beta.md")
    assert "created: 2026-06-01\n" in alpha
    assert "pickup_ready: false\n" in beta


def test_empty_handoffs_dir_returns_clean_result(norm_repo):
    """Empty state/handoffs/ returns exit_code 0 with no changes and no errors."""
    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))
    assert result["exit_code"] == 0
    assert result["changed"] == []
    assert result["errors"] == []
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (k2) archive/ exclusion — normalize MUST NOT touch archive/handoffs/ files
# ---------------------------------------------------------------------------


def test_archive_dir_excluded_from_normalization(norm_repo):
    """archive/handoffs/ files are NOT touched by normalize — negative-spec contract.

    Review: code-reviewer (F5) — the constraint is correctly implemented (normalize only
    globs state/handoffs/) but was untested; an accidental future expansion of the glob
    path would be invisible without this test.
    """
    # Seed a dirty state/handoffs/ file (will be changed by normalize).
    norm_repo.seed_handoff("2026-07-01-active.md", _DIRTY_HANDOFF)

    # Seed a drift-triggering archive/handoffs/ file (must NOT be changed).
    archive_dir = norm_repo.root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / "2026-07-01-archived.md"
    archive_path.write_text(_DIRTY_HANDOFF, encoding="utf-8")
    original_archive = archive_path.read_text(encoding="utf-8")

    result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    # Only the state/handoffs/ file should appear in changed.
    changed_files = {c["file"] for c in result["changed"]}
    assert "state/handoffs/2026-07-01-active.md" in changed_files, (
        "state/handoffs/ file must be in changed list"
    )
    assert not any("archive" in f for f in changed_files), (
        f"archive/handoffs/ file must NOT appear in changed; got {changed_files!r}"
    )

    # The archive/ file must be byte-identical to the original — no write occurred.
    assert archive_path.read_text(encoding="utf-8") == original_archive, (
        "archive/handoffs/ file must be byte-identical to original — normalize must not touch it"
    )


# ---------------------------------------------------------------------------
# (l) Error handling: repo_root absent → exit_code 2 (params.root fallback removed)
# ---------------------------------------------------------------------------


def test_no_repo_root_returns_exit_code_2():
    """repo_root arg absent → exit_code 2 (indeterminate); no params.root fallback."""
    result = _run(_handler({}, repo_root=None))
    assert result["exit_code"] == 2
    assert result["applied"] is False
    assert "error" in result


def test_repo_root_none_rejects_even_with_params_root_supplied(tmp_path):
    """Op-family path-containment sweep, 2026-07-08 § 1c: the permissive params.root
    scan-root fallback (Path(params_root).resolve() used directly as the SCAN ROOT
    with no containment check) has been removed entirely. Supplying params.root MUST
    NOT resurrect the old redirect-the-scan-root behavior — repo_root is required
    regardless of what params.root contains, and the handler must reject up front
    WITHOUT sweeping/mutating anything under the caller-supplied root.
    """
    # Build an out-of-tree directory with its own state/handoffs/ containing a
    # drift-triggering file — if the old fallback resurfaced, this file would be
    # swept and mutated despite repo_root being None.
    rogue_root = tmp_path / "rogue-root"
    rogue_handoffs = rogue_root / "state" / "handoffs"
    rogue_handoffs.mkdir(parents=True)
    rogue_file = rogue_handoffs / "2026-07-01-rogue.md"
    rogue_file.write_text(_DIRTY_HANDOFF, encoding="utf-8")
    original = rogue_file.read_text(encoding="utf-8")

    result = _run(
        _handler({"write": True, "root": str(rogue_root)}, repo_root=None)
    )

    assert result["exit_code"] == 2, (
        f"repo_root=None must reject regardless of params.root; got {result!r}"
    )
    assert result["applied"] is False
    assert result["changed"] == []
    assert "error" in result

    # The rogue file must be byte-identical — no sweep/mutation occurred.
    assert rogue_file.read_text(encoding="utf-8") == original, (
        "params.root must NOT be used as a scan root — file must be untouched"
    )


# ---------------------------------------------------------------------------
# Unit tests for _match_category (pure function — no fixture needed)
# ---------------------------------------------------------------------------


def test_match_category_roadmap():
    assert _match_category("sprint planning roadmap") == "roadmap"
    assert _match_category("Q3 Roadmap 2026") == "roadmap"


def test_match_category_bug():
    assert _match_category("fix bug in parser") == "bug"
    assert _match_category("hotfix rollback") == "bug"
    assert _match_category("regression found in CI") == "bug"


def test_match_category_research():
    assert _match_category("research spike: async patterns") == "research"
    assert _match_category("investigating the root cause") == "research"


def test_match_category_docs():
    assert _match_category("update docs for release") == "docs"
    assert _match_category("wiki page overhaul") == "docs"
    assert _match_category("documentation update") == "docs"
    # "documentation cleanup" correctly returns 'refactor' — cleanup fires before docs (JS order)
    assert _match_category("documentation cleanup") == "refactor"


def test_match_category_infra():
    assert _match_category("install coordinator core") == "infra"
    assert _match_category("pipeline hook setup") == "infra"
    assert _match_category("ci configuration") == "infra"


def test_match_category_refactor():
    assert _match_category("refactor session manager") == "refactor"
    assert _match_category("cleanup dead code") == "refactor"
    assert _match_category("consolidate helpers") == "refactor"


def test_match_category_uncategorized():
    assert _match_category("") == "uncategorized"
    assert _match_category("general work") == "uncategorized"
    # lifecycle words must NOT fire category keywords
    assert _match_category("spinoff recovery review release") == "uncategorized"


# ---------------------------------------------------------------------------
# locked_rmw integration — write path uses flock-protected RMW (C1)
# ---------------------------------------------------------------------------


def test_write_path_lock_timeout_surfaces_as_error(norm_repo):
    """LockTimeout from locked_rmw on write=True → file appears in errors, not changed.

    Mocks locked_rmw to raise LockTimeout so the handler's LockTimeout except-branch
    is exercised without requiring a real competing flock holder.  The file must NOT
    appear in the changed list and the overall exit_code must reflect errors (1).
    """
    norm_repo.seed_handoff("2026-07-01-lock-timeout.md", _DIRTY_HANDOFF)
    original = norm_repo.read_handoff("2026-07-01-lock-timeout.md")

    with patch(
        "coordinator_core.ops.handoff_normalize.locked_rmw",
        side_effect=LockTimeout("Could not acquire coordinator lock within 10s"),
    ):
        result = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    assert result["exit_code"] == 1, (
        f"LockTimeout during write must produce exit_code 1; got {result!r}"
    )
    assert result["applied"] is False
    error_files = {e["file"] for e in result["errors"]}
    assert any("lock-timeout" in f for f in error_files), (
        f"expected lock-timeout file in errors; got {error_files!r}"
    )
    changed_files = {c["file"] for c in result["changed"]}
    assert not any("lock-timeout" in f for f in changed_files), (
        "locked-out file must NOT appear in changed list"
    )
    # File on disk must be untouched.
    assert norm_repo.read_handoff("2026-07-01-lock-timeout.md") == original, (
        "file must be byte-identical after lock timeout — no write should have occurred"
    )


def test_dry_run_does_not_invoke_locked_rmw(norm_repo):
    """Dry-run (write=False) must NOT call locked_rmw — read-only, no lock needed.

    Patching locked_rmw to raise an error ensures that if it is accidentally called
    on the dry-run path, the test fails loudly.
    """
    norm_repo.seed_handoff("2026-07-01-dry-no-lock.md", _DIRTY_HANDOFF)

    with patch(
        "coordinator_core.ops.handoff_normalize.locked_rmw",
        side_effect=RuntimeError("locked_rmw must NOT be called on dry-run path"),
    ):
        result = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))

    # Dry-run must succeed — locked_rmw was NOT called.
    assert result["exit_code"] == 0, (
        f"dry-run must not fail; locked_rmw invocation would indicate a bug; got {result!r}"
    )
    assert result["dry_run"] is True
    assert len(result["changed"]) == 1


def test_write_path_uses_locked_rmw(norm_repo):
    """write=True path invokes locked_rmw; dry-run path does not.

    Wraps locked_rmw with a spy that records calls, then checks:
      - write=True: locked_rmw was called once (for the single dirty file)
      - write=False: locked_rmw was NOT called
    """
    import coordinator_core.ops.handoff_normalize as mod

    norm_repo.seed_handoff("2026-07-01-spy.md", _DIRTY_HANDOFF)

    calls: list = []
    real_locked_rmw = mod.locked_rmw

    def _spy(target, mutate, *, repo_root, **kw):
        calls.append(target)
        return real_locked_rmw(target, mutate, repo_root=repo_root, **kw)

    with patch.object(mod, "locked_rmw", side_effect=_spy):
        result_write = _run(_handler({"write": True}, repo_root=norm_repo.common_dir))

    assert len(calls) == 1, (
        f"locked_rmw must be called exactly once per dirty file on write=True; calls={calls!r}"
    )
    assert result_write["applied"] is True

    # Reset for dry-run check.
    calls.clear()
    norm_repo.seed_handoff("2026-07-01-spy2.md", _DIRTY_HANDOFF)

    with patch.object(mod, "locked_rmw", side_effect=_spy):
        result_dry = _run(_handler({"write": False}, repo_root=norm_repo.common_dir))

    assert len(calls) == 0, (
        f"locked_rmw must NOT be called on dry-run path; calls={calls!r}"
    )
    assert result_dry["dry_run"] is True
