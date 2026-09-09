"""
coordinator_core/ops/tests/test_plan_stamp_prepped.py — the "plan.stamp_prepped" op.

Subject: `coordinator_core.ops.plan_stamp_prepped`, the ONLY writer of the
four-field mise-prep attest.

The claim under test: the stamp certifies the bytes the bar actually read. Every
case here either pins that (gate inside the lock, sha over the BODY, refusal
without a passing verdict), pins the quartet's all-or-nothing shape, or pins the
idempotence that keeps `mise_prepped_at` meaning "when this body was certified".

Zero spawns. `locked_rmw` needs only a resolvable git common dir for its lock
sidecar, so every fixture is a bare `tmp_path` tree with a `.git` DIRECTORY and
no `git init`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.benchmarks.budget import resolve_budget
from coordinator_core.frontmatter.primitives import canonical_body_sha
from coordinator_core.frontmatter.schema_validate import validate
from coordinator_core.ops import plan_stamp_prepped as mod
from coordinator_core.roadmap import prep_gate as pg

OP_KEY = "plan.stamp_prepped"
BY = "test-session-01"


def _run(coro):
    return asyncio.run(coro)


def _stamp(params: dict, repo_root: Path) -> dict:
    return _run(mod._handler(params, repo_root))


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "coordinator_core").mkdir()
    return tmp_path / ".git"


_FM = """census: []
prime_exit_criterion:
  statement: the four fields land and validate
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_SPINE = """- id: C1
  title: Ship it
  change_kind: code-edit
  surface: coordinator_core/ops/plan_stamp_prepped.py
  writes: [coordinator_core/ops/plan_stamp_prepped.py]
  queue_scope: project
  disposition: open
"""

REL = "docs/plans/2026-09-07-fixture.md"


def _plan(
    root: Path, *, frontmatter: str = _FM, spine: str | None = _SPINE, extra: str = ""
) -> str:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    body = ["", "# Fixture", "", "A body whose sha is the thing being certified.", ""]
    if spine is not None:
        body += ["## Tasks", "", "```yaml plan-tasks", spine.strip(), "```", ""]
    (plans / "2026-09-07-fixture.md").write_text(
        "---\ntitle: fixture\nauthor: claude-klabauter-em\nstatus: draft\n"
        "created: 2026-09-07\n" + frontmatter + extra + "---\n" + "\n".join(body),
        encoding="utf-8",
    )
    return REL


def _fm_of(path: Path) -> dict:
    return pg.plan_frontmatter(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Wire registration
# ---------------------------------------------------------------------------


def test_op_resolves_through_the_real_dispatch_path():
    handler = ipc._REGISTRY.get(OP_KEY) or ipc._lazy_import_and_lookup(OP_KEY)
    assert callable(handler), f"{OP_KEY!r} ships present-but-dead"


def test_op_is_classified_mutating():
    """Fail-closed by fact, not by default: this op writes the plan file."""
    assert classify(OP_KEY) is OpClass.MUTATING


def test_op_is_keyed_common_dir():
    assert ipc.OP_KEY_SCOPE.get(OP_KEY) == "common_dir"


def test_op_resolves_a_budget():
    assert resolve_budget(OP_KEY, classify(OP_KEY))["target_ms"] > 0


def test_the_op_declares_the_noun_it_mutates():
    assert mod.MUTATES == ["docs/plans/*.md"]


# ---------------------------------------------------------------------------
# Params and roots
# ---------------------------------------------------------------------------


def test_absent_repo_root_refuses(tmp_path):
    with pytest.raises(ValueError, match="requires a resolved repo_root"):
        _run(mod._handler({"plan": REL, "by": BY}, None))


def test_a_plan_outside_the_worktree_is_refused(tmp_path):
    common = _repo(tmp_path)
    outside = tmp_path.parent / "elsewhere.md"
    outside.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the resolved worktree"):
        _stamp({"plan": str(outside), "by": BY}, common)


def test_a_missing_plan_is_refused_by_name(tmp_path):
    common = _repo(tmp_path)
    with pytest.raises(ValueError, match="no such plan"):
        _stamp({"plan": "docs/plans/nope.md", "by": BY}, common)


def test_an_unresolvable_certifying_session_refuses(tmp_path, monkeypatch):
    """An attest whose author is unknown names nobody to ask."""
    common = _repo(tmp_path)
    _plan(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.session.core.attributable_session_id", lambda *a, **k: ""
    )
    with pytest.raises(ValueError, match="could not resolve a certifying session"):
        _stamp({"plan": REL}, common)


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def test_a_not_prepped_plan_is_refused_and_nothing_is_written(tmp_path):
    common = _repo(tmp_path)
    _plan(tmp_path, frontmatter="", spine=None)
    path = tmp_path / REL
    before = path.read_bytes()

    result = _stamp({"plan": REL, "by": BY}, common)
    assert result["stamped"] is False
    assert result["outcome"] == "refused"
    assert result["verdict"] == pg.NOT_PREPPED
    assert path.read_bytes() == before


def test_the_refusal_carries_the_per_class_breakdown(tmp_path):
    """A refusal is a REPLY, not an exception: raising would collapse four
    routable findings into one string."""
    common = _repo(tmp_path)
    _plan(tmp_path, frontmatter="", spine=None)
    result = _stamp({"plan": REL, "by": BY}, common)
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    assert result["classes"]["CENSUS"]["kind"] == "census-undeclared"
    assert "mise-prep: NOT-PREPPED" in result["message"]


def test_a_commit_in_owner_repo_gate_stamps_with_its_row_withheld(tmp_path):
    """PM ruling: a plan is not rejected because part of it needs code in another repo.

    The stamp lands, and the commit-gated row rides `mise_prepped_findings` as a withheld row —
    the same treatment `landed-work` always got. That is what puts the cross-repo work in front
    of an operator as something to dispatch, instead of deleting the plan's other rows with it.
    """
    common = _repo(tmp_path)
    spine = f"""- id: C1
  title: Reaches out
  change_kind: code-edit
  surface: DoE-claude/coordinator/schemas/plan.schema.json
  writes: []
  queue_scope: project
  disposition: open
  external_gate:
    - owner_repo: DoE-claude
      condition: someone commits there
      requires: {pg.REQUIRES_COMMIT}
"""
    _plan(tmp_path, spine=spine)
    result = _stamp({"plan": REL, "by": BY}, common)
    assert result["verdict"] == pg.PREPPED
    assert result["stamped"] is True
    fm = _fm_of(tmp_path / REL)
    assert fm["mise_prepped_findings"] == ["C1"]


def test_there_is_no_override_parameter(tmp_path):
    """The only way past this bar is to declare what it names."""
    import inspect

    source = inspect.getsource(mod)
    for escape in ("force", "override", "--force", "skip_gate"):
        assert f'"{escape}"' not in source and f"'{escape}'" not in source


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------


def test_a_prepped_plan_gets_all_four_fields(tmp_path):
    common = _repo(tmp_path)
    _plan(tmp_path)
    result = _stamp({"plan": REL, "by": BY}, common)

    assert result["stamped"] is True
    assert result["outcome"] == "stamped"
    fm = _fm_of(tmp_path / REL)
    assert fm["mise_prepped_by"] == BY
    assert fm["mise_prepped_at"].endswith("Z")
    assert fm["mise_prepped_findings"] == []
    assert isinstance(fm["mise_prepped_sha"], str)


def test_the_recorded_sha_is_the_body_sha_and_survives_its_own_write(tmp_path):
    """Frontmatter is excluded by design — that exclusion is what lets the stamp
    survive the write that places it, and every later `status` flip."""
    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    body_sha_before = canonical_body_sha(path.read_text(encoding="utf-8"))

    result = _stamp({"plan": REL, "by": BY}, common)
    text_after = path.read_text(encoding="utf-8")

    assert result["mise_prepped_sha"] == body_sha_before
    assert canonical_body_sha(text_after) == body_sha_before
    assert pg.read_stamp(text_after)["state"] == pg.CERTIFIED


def test_a_status_flip_after_the_stamp_leaves_it_certified(tmp_path):
    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    _stamp({"plan": REL, "by": BY}, common)

    path.write_text(
        path.read_text(encoding="utf-8").replace("status: draft", "status: executing"),
        encoding="utf-8",
    )
    assert pg.read_stamp(path.read_text(encoding="utf-8"))["state"] == pg.CERTIFIED


def test_a_body_edit_after_the_stamp_makes_it_stale(tmp_path):
    """STALE and UNSTAMPED are different words on purpose: stale re-gates, absent
    stamps, and the wrong repair re-stamps a plan whose defects were never
    re-checked."""
    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    _stamp({"plan": REL, "by": BY}, common)

    path.write_text(
        path.read_text(encoding="utf-8") + "\nA sentence the bar never read.\n",
        encoding="utf-8",
    )
    assert pg.read_stamp(path.read_text(encoding="utf-8"))["state"] == pg.STALE


def test_withheld_rows_land_in_findings_as_ids(tmp_path):
    """Ids, not prose and not a count: the ids point into the plan's own spine,
    which is the record."""
    common = _repo(tmp_path)
    spine = f"""- id: C1
  title: Local work
  change_kind: code-edit
  surface: coordinator_core/x.py
  writes: []
  queue_scope: project
  disposition: open
- id: C4
  title: Waits on a sibling
  change_kind: code-edit
  surface: DoE-claude/coordinator/bin/mise-prep-gate.py
  writes: []
  queue_scope: project
  disposition: open
  external_gate:
    - owner_repo: DoE-claude
      condition: the gate script lands
      requires: {pg.REQUIRES_LANDED}
"""
    _plan(tmp_path, spine=spine)
    result = _stamp({"plan": REL, "by": BY}, common)

    assert result["verdict"] == pg.PREPPED
    assert result["mise_prepped_findings"] == ["C4"]
    assert _fm_of(tmp_path / REL)["mise_prepped_findings"] == ["C4"]


def test_the_stamp_touches_no_other_field(tmp_path):
    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    before = _fm_of(path)
    _stamp({"plan": REL, "by": BY}, common)
    after = _fm_of(path)
    assert {k: v for k, v in after.items() if not k.startswith("mise_prepped_")} == before


def test_the_body_is_byte_identical_after_the_stamp(tmp_path):
    """`rebuild` is byte-identical outside the mutated lines — the anti-clobber
    guarantee. A full YAML re-emit would rewrite the whole document."""
    from coordinator_core.frontmatter.primitives import frontmatter_body_text

    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    before = frontmatter_body_text(path.read_text(encoding="utf-8"))
    _stamp({"plan": REL, "by": BY}, common)
    assert frontmatter_body_text(path.read_text(encoding="utf-8")) == before


def test_the_quartet_is_contiguous_under_status(tmp_path):
    common = _repo(tmp_path)
    _plan(tmp_path)
    _stamp({"plan": REL, "by": BY}, common)
    lines = (tmp_path / REL).read_text(encoding="utf-8").splitlines()
    start = lines.index("status: draft")
    assert [line.split(":")[0] for line in lines[start + 1 : start + 5]] == list(
        pg.STAMP_FIELDS
    )


# ---------------------------------------------------------------------------
# Idempotence and re-stamping
# ---------------------------------------------------------------------------


def test_re_stamping_a_certified_plan_writes_nothing(tmp_path):
    """Byte-identical output skips the write entirely — that is how idempotence
    is spelled here, and it keeps `mise_prepped_at` meaning "when this body was
    certified" rather than "when someone last ran the op"."""
    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    first = _stamp({"plan": REL, "by": BY}, common)
    mtime = path.stat().st_mtime_ns
    bytes_before = path.read_bytes()

    second = _stamp({"plan": REL, "by": "a-different-session"}, common)

    assert second["stamped"] is False
    assert second["outcome"] == "already-certified"
    assert second["mise_prepped_sha"] == first["mise_prepped_sha"]
    assert path.read_bytes() == bytes_before
    assert path.stat().st_mtime_ns == mtime


def test_a_stale_stamp_is_replaced_not_duplicated(tmp_path):
    common = _repo(tmp_path)
    _plan(tmp_path)
    path = tmp_path / REL
    _stamp({"plan": REL, "by": BY}, common)
    path.write_text(
        path.read_text(encoding="utf-8") + "\nA later paragraph.\n", encoding="utf-8"
    )

    result = _stamp({"plan": REL, "by": "second-session"}, common)
    text = path.read_text(encoding="utf-8")

    assert result["stamped"] is True
    assert text.count("mise_prepped_sha:") == 1
    assert text.count("mise_prepped_by:") == 1
    assert pg.read_stamp(text)["state"] == pg.CERTIFIED
    assert _fm_of(path)["mise_prepped_by"] == "second-session"


def test_a_hand_written_block_value_is_refused_by_name(tmp_path):
    """The write op cannot emit a partial or indented stamp, so it refuses to
    flatten one — silently truncating would orphan the continuation lines."""
    common = _repo(tmp_path)
    _plan(
        tmp_path,
        extra="mise_prepped_findings:\n  - C1\n  - C2\n",
    )
    result = _stamp({"plan": REL, "by": BY}, common)
    assert result["stamped"] is False
    assert "mise_prepped_findings" in result["message"]
    assert "indented YAML block" in result["message"]


def test_a_partial_hand_stamp_is_completed_by_a_full_re_stamp(tmp_path):
    """MALFORMED is the consumer state for a hand-written stamp; the repair is a
    full re-stamp, not a hand-completed quartet."""
    common = _repo(tmp_path)
    _plan(tmp_path, extra="mise_prepped_by: someone\n")
    path = tmp_path / REL
    assert pg.read_stamp(path.read_text(encoding="utf-8"))["state"] == pg.MALFORMED

    result = _stamp({"plan": REL, "by": BY}, common)
    assert result["stamped"] is True
    assert pg.read_stamp(path.read_text(encoding="utf-8"))["state"] == pg.CERTIFIED
    assert _fm_of(path)["mise_prepped_by"] == BY


# ---------------------------------------------------------------------------
# The stamp validates
# ---------------------------------------------------------------------------


def test_what_the_op_writes_passes_plan_schema_validation(tmp_path):
    """Including the cross-field rule: the quartet is the only shape that clears
    `_cf_mise_prepped_stamp_quartet`, and this op is what emits it."""
    common = _repo(tmp_path)
    _plan(tmp_path)
    _stamp({"plan": REL, "by": BY}, common)
    assert validate("plan", _fm_of(tmp_path / REL)) == {"ok": True}


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_the_write_path_never_spawns_a_subprocess(tmp_path, monkeypatch):
    """The gate is pure and `canonical_body_sha` is pure Python. `git --version`
    alone costs 25.3ms, and this op runs once per plan on the fire path."""
    import subprocess

    common = _repo(tmp_path)
    _plan(tmp_path)
    _stamp({"plan": REL, "by": BY}, common)  # warm the deferred imports

    def _boom(*args, **kwargs):
        raise AssertionError("plan.stamp_prepped must not create a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    path = tmp_path / REL
    path.write_text(path.read_text(encoding="utf-8") + "\nMore body.\n", encoding="utf-8")
    assert _stamp({"plan": REL, "by": BY}, common)["stamped"] is True
