"""A scaffolded plan is GOVERNED, and its deferral gate actually refuses.

Purpose: bare presence of the plan-document-level `grouping_approvals` key is
the entire governed/legacy discriminator (`schema_validate.is_governed_plan`).
`coordinator-doc-new --type plan` is the sanctioned -- and, per
`coordinator:plan`'s Exit, the MANDATORY -- way to produce a plan, and it did
not emit that key. So no plan could be born governed, and opting in meant
hand-authoring frontmatter the plan skill forbids hand-authoring. A census of
27 closed rows on 2026-09-01 found the gate had never fired once; the cause was
mechanical, not cultural.

PM ruling 2026-09-01, verbatim: "we can have it by default yeah, because I
don't want a plan to execute that has deferred, won't do, spunoff without
getting approval. that approval can come from the `Uhura` or `G-EM` and not
just myself. that's better than only discovering at our workstream-complete
exit!"

WHY THE SECOND TEST EXISTS. Asserting the key is present asserts the plan is
CLASSIFIED governed, which is not the same claim as the gate REFUSING
anything -- and that gap is exactly the shape of the defect being closed here,
where a mechanism everyone believed was live had never once fired. So the
refusal is driven end to end: scaffold a real plan, flip one spine row to
`backlogged`, and assert `check_plan_tasks_grouping_approval` names the
grouping and declines.

NEGATIVE SPEC -- what must never be "fixed" into this file:
  - No block is scaffolded as `approved`. The schema requires a verbatim
    utterance and a fresh membership digest beside that status, so a
    scaffolder writing it would be minting assent nobody gave. `do` is
    emitted `pending` and stays pending for its whole life; it gates nothing.
  - `do` is emitted rather than omitted. If the one harmless grouping were
    absent, a reader would learn to treat a missing block as ordinary, and
    the three that matter are told apart from damage only by being present.

Non-spawning by construction: `_scaffold_plan` is called in-process.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
import yaml

_REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))

from coordinator_core.frontmatter import schema_validate as sv  # noqa: E402

_GATED_GROUPINGS = ("spun_off", "defer", "ruled_out")


def _doc_new():
    """Import `coordinator-doc-new.py` by path -- the hyphens in its filename
    make it un-importable by name, which is why every sibling test does this
    too rather than any one of them having found a nicer route."""
    path = _REPO / "coordinator" / "bin" / "coordinator-doc-new.py"
    spec = importlib.util.spec_from_file_location("coordinator_doc_new", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scaffolded() -> str:
    mod = _doc_new()
    return mod._scaffold_plan(
        title="governance pin",
        branch="work/test/governance-pin",
        author="test-em",
        plan_id="pln-governance-pin-000000",
    )


@pytest.fixture(scope="module")
def frontmatter(scaffolded: str) -> dict:
    fm = sv.parse_frontmatter(scaffolded).get("frontmatter")
    assert isinstance(fm, dict), "scaffolded plan has no parseable frontmatter"
    return fm


def test_a_scaffolded_plan_is_governed(frontmatter):
    assert sv.is_governed_plan(frontmatter)


def test_every_grouping_is_emitted(frontmatter):
    blocks = frontmatter["grouping_approvals"]
    assert set(blocks) == {"do", *_GATED_GROUPINGS}


@pytest.mark.parametrize("grouping", ("do", *_GATED_GROUPINGS))
def test_no_grouping_is_born_approved(frontmatter, grouping):
    """A scaffolder cannot approve anything. See this file's negative spec."""
    assert frontmatter["grouping_approvals"][grouping]["status"] == "pending"


def test_the_scaffolded_plan_validates_against_the_plan_schema(frontmatter):
    """The block has `additionalProperties: false` and a required `status`, so
    a shape error here is a schema violation rather than something that reads
    as merely absent -- which means an emitter typo would otherwise surface as
    a plan that silently fails validation on first write."""
    import json

    schema = json.loads(
        (_REPO / "coordinator_core" / "frontmatter" / "schemas" / "plan.schema.json")
        .read_text(encoding="utf-8")
    )
    result = sv.validate_frontmatter_obj(frontmatter, schema)
    assert result["ok"], result.get("errors")


def test_an_unapproved_deferral_is_actually_refused(scaffolded):
    """The claim that matters: not that the plan is labelled governed, but
    that closing a row into a gated grouping is declined."""
    mutated = scaffolded.replace("  disposition: open  #", "  disposition: backlogged  #", 1)
    rows = sv._plan_tasks_spine_rows(mutated)
    assert rows and rows[0].get("disposition") == "backlogged", (
        "fixture did not actually flip the row -- the template row carries its "
        "own `disposition: open` line, and inserting a second key ahead of it "
        "is silently overridden by the later duplicate"
    )
    err = sv.check_plan_tasks_grouping_approval(mutated)
    assert err is not None
    assert err["field"] == "grouping_approvals.defer"


def test_an_untouched_scaffold_is_not_refused(scaffolded):
    """The false-positive floor. Every row is born `open`, which derives into
    the ungated `do` grouping, so a freshly scaffolded plan must be clean --
    otherwise the gate fires on every new plan and gets routed around."""
    assert sv.check_plan_tasks_grouping_approval(scaffolded) is None
