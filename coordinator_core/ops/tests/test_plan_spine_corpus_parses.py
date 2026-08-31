"""Every located `yaml plan-tasks` spine in this repo's plan corpus parses.

The fence renders, `git diff` is clean, and a human reads the block
correctly — while `yaml.safe_load` on its body raises and every spine CLI
reports a visibly present spine as ABSENT, first noticed at
`/execute-plan`, after review and PM ratification. Three distinct
producers have written this class across the fleet (DoE-claude's
`review-integrator` HTML comments, a title with an unquoted inline
`kind:`, and this repo's own dedented block-scalar continuation in
`docs/plans/2026-08-11-designated-holder-repo-for-unowned-identity.md`),
so the write-time refusal in `plan_tasks_mutate._parse_rows_or_abort` is
paired with this corpus sweep: the refusal stops the next one, the sweep
names the ones already on disk.

Fence structure is asserted alongside the parse, because scoping the
sweep to LOCATED plans is exactly how two of them hid: a MALFORMED fence
(wrong heading, or two fences) drops the plan out of the parse sweep
entirely, so its body is never looked at. Both claude-klabauter plans in that state
turned out to ALSO be unparseable underneath — one of them by this memo's
own HTML-comment-in-fence class. A status the sweep skips is a place the
next one hides.

Negative-spec: asserts locatability and PARSE-ability only, never schema
shape — a row missing `writes:` is `schema_validate.py`'s finding, not
this test's. ABSENT is not a failure: most plans carry no spine.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

_PLANS = Path(__file__).resolve().parents[3] / "docs" / "plans"


def _plans() -> list:
    return sorted(_PLANS.rglob("*.md"))


@pytest.mark.skipif(not _PLANS.is_dir(), reason="no docs/plans/ in this checkout")
@pytest.mark.parametrize("plan", _plans(), ids=lambda p: p.name)
def test_plan_spine_locates_and_parses(plan: Path) -> None:
    result = locate_fenced_block(plan.read_text(encoding="utf-8"))
    if result.status is LocateStatus.ABSENT:
        pytest.skip("no spine in this plan")
    assert result.status is LocateStatus.LOCATED, (
        f"{plan}: spine fence is MALFORMED — two `yaml plan-tasks` fences, or none "
        f"inside a `## Tasks` section. Every spine CLI refuses this plan, and the "
        f"parse assertion below never runs against it."
    )
    body = result.body
    try:
        rows = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1} of the fenced block" if mark else ""
        pytest.fail(f"{plan}: spine does not parse{where}: {getattr(exc, 'problem', exc)}")
    assert rows is None or isinstance(rows, list), f"{plan}: spine body is not a YAML list"


def _load_doc_new():
    """Import `coordinator/bin/coordinator-doc-new.py` by path (hyphenated name)."""
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-doc-new.py"
    spec = importlib.util.spec_from_file_location("_coordinator_doc_new_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_new_plan_scaffold_spine_parses() -> None:
    """The emitter's own scaffold is the corpus's first row — assert it parses.

    A scaffold whose sample rows do not load hands every new plan a broken
    spine at authoring time, which the write-time refusal would then bounce
    on the first `plan-tasks add-task`.
    """
    source = _load_doc_new()._scaffold_plan(
        title="Scaffold parse probe", branch="work/probe", author="test",
    )
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.LOCATED, f"scaffold spine not located: {result.status}"
    rows = yaml.safe_load(result.body)
    assert isinstance(rows, list) and rows, "scaffold spine parsed to no rows"
