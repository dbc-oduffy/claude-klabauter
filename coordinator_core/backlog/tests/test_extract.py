from coordinator_core.backlog.extract import MAX_RECORD_TOKENS, extract_record, record_tokens

PLAN = """---
status: active
title: ignored
created: 2026-11-13
---
# A plan

## Problem
Long prose here.

## Tasks

```yaml plan-tasks
- id: C0
  title: Pin interfaces
  disposition: done
  body: |
    # not a heading
- id: C1
  title: "Walk weeks"
  disposition: open
```

## Outcome
It shipped.
More outcome text.
"""

DR = """---
status: accepted
---
# DR-344 brightline

## Decision
500ms or deleted.
Second line is dropped.

## Consequences
Over the bar means kill.
"""

HANDOFF = """# Handoff abc123

**Outcome:** landed C3
Body.
"""


def test_plan_with_tasks_spine():
    rec = extract_record("docs/plans/p.md", text=PLAN)
    assert rec.tasks_ledger == ("C0 | Pin interfaces | done", "C1 | Walk weeks | open")
    assert "fm: status: active" in rec.headings
    assert not any("ignored" in h for h in rec.headings)
    assert "# A plan" in rec.headings and "## Tasks" in rec.headings
    assert not any("not a heading" in h for h in rec.headings)
    assert rec.outcomes == ("outcome: It shipped.",)
    assert record_tokens(rec) < MAX_RECORD_TOKENS


def test_decision_keeps_first_line_only():
    rec = extract_record("docs/decisions/DR-344-x.md", text=DR)
    assert rec.decisions == ("decision: 500ms or deleted.", "consequences: Over the bar means kill.")


def test_handoff_bold_outcome():
    rec = extract_record("state/handoffs/h.md", text=HANDOFF)
    assert rec.outcomes == ("Outcome: landed C3",)


def test_outline_stands_in_for_file_read(tmp_path):
    missing = str(tmp_path / "gone.md")
    assert extract_record(missing) .headings == ()
    rec = extract_record(missing, outline="# Only outline\n")
    assert rec.headings == ("# Only outline",)


def test_oversized_artifact_is_trimmed_under_budget():
    rows = "\n".join(f"- id: T{i}\n  title: {'word ' * 30}\n  disposition: open" for i in range(80))
    text = "# big\n" + "\n".join(f"## Heading number {i} " + "x" * 90 for i in range(80)) + f"\n```yaml plan-tasks\n{rows}\n```\n"
    rec = extract_record("big.md", text=text)
    assert record_tokens(rec) < MAX_RECORD_TOKENS
    assert rec.headings and rec.tasks_ledger


def test_real_file_reads_from_disk(tmp_path):
    p = tmp_path / "d.md"
    p.write_text(DR)
    assert extract_record(str(p)).decisions[0] == "decision: 500ms or deleted."
