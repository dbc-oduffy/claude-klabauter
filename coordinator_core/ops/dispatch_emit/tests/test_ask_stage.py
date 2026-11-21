"""ask_stage: briefs equal the plan route's prompts, the manifest round-trips, XS yields row X1."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops.dispatch_emit import emit, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import _handler, stage
from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

def read_stage_reply(root, reply: dict) -> dict:
    """A refusal reply as-is; a success reply's manifest read through its verified `manifest_handle`."""
    if "error" in reply:
        return reply
    assert set(reply) - {"already_staged"} == {"manifest_handle"}, reply
    return reply_sidecar.read_reply(root, reply["manifest_handle"])


PLAN_REL = "docs/plans/2026-10-01-fixture.md"
SIZING_REL = "state/sizings/2026-10-01-xs-fixture.yaml"

PLAN = """---
deliverable_id: dlv-fixture-abc123
---
# Fixture plan

## Problem

Three rows exercise the stage.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
- id: C3
  title: third
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
```
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    write_run_scripts(tmp_path, "r1", "r2", "r3", "x1run", "spine", "gitrun")
    return tmp_path


def test_three_row_plan_yields_briefs_equal_to_the_plan_routes_prompts(repo):
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    plan_path = repo / PLAN_REL
    text = plan_path.read_text(encoding="utf-8")
    ctx = emit.derive_plan_context(text, fallback_title=plan_path.stem, repo_root=repo.as_posix())
    rows = [r for w in build_waves(read_spine(plan_path)) for r in w]
    assert {r.id for r in m.rows} == {"C1", "C2", "C3"}
    for mr in m.rows:
        row = next(r for r in rows if r.id == mr.id)
        brief = (repo / mr.brief_path).read_text(encoding="utf-8")
        assert brief.startswith(emit._row_prompt(row, PLAN_REL, ctx).rstrip())
    waves = {r.id: r.wave for r in m.rows}
    assert waves["C2"] > waves["C1"]
    assert {"pkg/a.py", "pkg/b.py", "pkg/c.py"} <= set(m.review_declared_paths)


def test_manifest_round_trips_and_request_names_every_row(repo):
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    on_disk = json.loads((repo / RECORD_DIR_ROOT / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert StageManifest.from_json(on_disk) == m
    assert m.run_dir == f"{RUN_DIR_ROOT}/r1"
    assert not (repo / m.run_dir / "manifest.json").exists()
    req = parse_marker((repo / m.marker_path).read_text(encoding="utf-8"))
    assert {c.id for c in req.chunks} == {"C1", "C2", "C3", "docs"}
    assert req.deliverable_id == "dlv-fixture-abc123"
    assert req.plan_path == PLAN_REL


def test_xs_sizing_yields_one_row_x1(repo):
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["X1"]
    assert m.rows[0].writes[0] == "pkg/a.py"
    assert (repo / m.rows[0].brief_path).is_file()
    assert (repo / RECORD_DIR_ROOT / "x1run" / "2026-10-01-xs-fixture.spine.md").is_file()
    assert not list((repo / RUN_DIR_ROOT / "x1run").glob("*.spine.md"))


@pytest.mark.parametrize("run_id", ["..", "a/b", ""])
def test_unsafe_run_id_refused(repo, run_id):
    with pytest.raises(AskStageError):
        stage(repo, run_id=run_id, plan_rel=PLAN_REL)


def test_plan_outside_repo_refused(repo):
    with pytest.raises(AskStageError):
        stage(repo, run_id="r1", plan_rel="../elsewhere.md")


def test_both_or_neither_input_refused(repo):
    with pytest.raises(AskStageError):
        stage(repo, run_id="r1")
    with pytest.raises(AskStageError):
        stage(repo, run_id="r1", plan_rel=PLAN_REL, sizing_rel=SIZING_REL)


def test_handler_resolves_the_sizing_from_the_common_dir_the_engine_passes(repo):
    """The dispatcher hands common_dir-scoped ops `<repo>/.git`, not the worktree root."""
    (repo / ".git").mkdir()
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    reply = read_stage_reply(repo, _handler(
        {"run_id": "gitrun", "sizing_path": SIZING_REL, "writes": ["pkg/a.py"]},
        repo_root=repo / ".git",
    ))
    assert "error" not in reply, reply
    assert [r["id"] for r in reply["rows"]] == ["X1"]


_UNKEYED_MEMO_PLAN = PLAN.split("```yaml plan-tasks")[0] + """```yaml plan-tasks
- id: M1
  title: send the memo
  change_kind: doc-edit
  kind: memo-send
  memo_to: coordinator-content-repo-em
  surface: docs/x.md
  writes: []
```
"""


def test_unkeyed_memo_send_row_is_a_named_refusal_not_an_internal_error(repo):
    (repo / PLAN_REL).write_text(_UNKEYED_MEMO_PLAN, encoding="utf-8", newline="\n")
    with pytest.raises(AskStageError, match=r"SPINE-REFUSED.*MemoRowNoReceiptError.*'M1'"):
        stage(repo, run_id="r1", plan_rel=PLAN_REL)
    (repo / ".git").mkdir()
    reply = _handler({"run_id": "r2", "plan_path": PLAN_REL, "writes": []}, repo_root=repo / ".git")
    assert "MemoRowNoReceiptError" in reply["error"] and "SPINE-REFUSED" in reply["error"]


def test_xs_spine_and_manifest_carry_a_real_form_plan_id(repo):
    import re

    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    doc = {"intent": "Fix the thing", "exit_criterion": {"statement": "it works"}}
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    spine = repo / RECORD_DIR_ROOT / "x1run" / "2026-10-01-xs-fixture.spine.md"
    plan_id = read_fm_field_unquoted(split_frontmatter(spine.read_text(encoding="utf-8")).fm_text, "plan_id")
    assert re.fullmatch(r"pln-[a-z0-9-]+-[0-9a-f]{6}", plan_id)
    assert m.plan_id == plan_id
    assert StageManifest.from_json(json.loads((repo / RECORD_DIR_ROOT / "x1run" / "manifest.json").read_text())).plan_id == plan_id


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {},
        {"run_id": 5},
        {"run_id": "r", "plan_path": 3},
        {"run_id": "r", "sizing_path": ["x"]},
        {"run_id": "r", "writes": "a.py"},
        {"run_id": "r", "writes": [1]},
    ],
)
def test_handler_refuses_malformed_params_with_a_structured_error(repo, params):
    reply = _handler(params, repo_root=repo)
    assert set(reply) == {"error"} and "dispatch.ask_stage" in reply["error"]


def test_stage_registers_declared_paths_as_the_sessions_review_targets(repo):
    (repo / ".git").mkdir(exist_ok=True)
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL, session_id="sess-1")
    targets = (session_dir_for(repo / ".git", "sess-1") / "review-targets.txt").read_text(encoding="utf-8")
    assert set(targets.split()) >= set(m.review_declared_paths)


def test_marker_carries_the_sizing_record_so_the_terminal_commit_lands_it(repo):
    m = stage(repo, run_id="r2", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    req = parse_marker((repo / m.marker_path).read_text(encoding="utf-8"))
    sizing = [c for c in req.chunks if c.id == "sizing"]
    assert [c.paths for c in sizing] == [(SIZING_REL,)]
    assert SIZING_REL not in m.review_declared_paths


def test_a_row_waits_on_its_own_predecessors_never_on_wave_siblings(repo):
    # C3 shares wave 0 with C1 but depends on nothing; C2 waits on C1 alone.
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    deps = {r.id: r.deps for r in m.rows}
    assert deps == {"C1": (), "C2": ("C1",), "C3": ()}


def _pinned_plan(repo, pinned_text, sizing_text):
    from coordinator_core.ops import requirement_register as rr

    doc = {"requirement_register": {"sources": [{"kind": "pm"}],
                                    "rows": [{"id": "R1", "source_text": sizing_text}]}}
    (repo / "state" / "sizings" / "pinned.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    claims = (
        "register_claims:\n  sizing: state/sizings/pinned.yaml\n  rows: [R1]\n"
        f"  pins:\n    R1: '{rr.row_pin(pinned_text)}'\n"
    )
    (repo / PLAN_REL).write_text(PLAN.replace("---\n#", f"{claims}---\n#", 1), encoding="utf-8", newline="\n")


def test_drifted_pin_refuses_naming_the_row(repo):
    _pinned_plan(repo, "original words", "edited words")
    with pytest.raises(AskStageError, match="register row R1"):
        stage(repo, run_id="r1", plan_rel=PLAN_REL)


def test_matching_pin_stages_normally(repo):
    _pinned_plan(repo, "same words", "same   words")
    assert {r.id for r in stage(repo, run_id="r1", plan_rel=PLAN_REL).rows} == {"C1", "C2", "C3"}


def test_audit_and_scratch_only_rows_declare_no_reviewable_path(repo):
    """Rows writing only bookkeeping or scratch freeze no reviewable diff: an empty
    `review_declared_paths` makes the composed script skip review instead of halting at prep."""
    plan = PLAN.split("```yaml plan-tasks")[0] + """```yaml plan-tasks
- id: C1
  title: audit
  change_kind: code-edit
  surface: state/audits/x.md
  writes:
    - state/audits/x.md
    - scratch/notes.md
```
"""
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    m = stage(repo, run_id="r2", plan_rel=PLAN_REL)
    assert [r.id for r in m.rows] == ["C1"]
    assert m.review_declared_paths == ()


def test_plan_branch_stages_the_spines_writes_not_the_sizing_footprint_or_caller_writes(repo):
    """A sizing footprint guess and caller `writes` never reach a plan-branch row or the review-declared paths."""
    sizing_rel = "state/sizings/2026-10-01-guess.yaml"
    (repo / sizing_rel).write_text(
        yaml.safe_dump({"intent": "x", "footprint": ["guess/old.py"]}), encoding="utf-8", newline="\n"
    )
    plan = PLAN.replace("---\n#", f"sizing_object: {sizing_rel}\n---\n#", 1)
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    (repo / ".git").mkdir(exist_ok=True)
    reply = read_stage_reply(repo, _handler(
        {"run_id": "spine", "plan_path": PLAN_REL, "writes": ["guess/old.py", "guess/other.py"]},
        repo_root=repo / ".git",
    ))
    assert "error" not in reply, reply
    staged = {w for r in reply["rows"] for w in r["writes"]}
    assert {"pkg/a.py", "pkg/b.py", "pkg/c.py"} <= staged
    assert not any(w.startswith("guess/") for w in staged)
    assert not any(w.startswith("guess/") for w in reply["review_declared_paths"])


def test_plugin_agent_row_without_agent_model_stages_with_frontmatter_model(repo, tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    agents = home / ".claude" / "plugins" / "game-dev" / "agents"
    agents.mkdir(parents=True)
    (agents / "staff-game-dev.md").write_text(
        "---\nname: staff-game-dev\nmodel: opus\n---\nbody\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    plan = PLAN.split("```yaml plan-tasks")[0] + """```yaml plan-tasks
- id: C1
  title: plugin agent row
  change_kind: code-edit
  surface: pkg/a.py
  agent_type: game-dev:staff-game-dev
  writes:
    - pkg/a.py
```
"""
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    m = stage(repo, run_id="r3", plan_rel=PLAN_REL)
    row = next(r for r in m.rows if r.id == "C1")
    assert (row.agent_type, row.model) == ("game-dev:staff-game-dev", "opus")
    assert row.brief_path


_NO_EDGE_PLAN = PLAN.replace(
    "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n", ""
)
_BAD_EDGE_PLAN = PLAN.replace("      gate_kind: output-consumption-runtime\n", "")


def test_plan_without_depends_on_stages_to_a_manifest_at_default_arguments(repo):
    (repo / PLAN_REL).write_text(_NO_EDGE_PLAN, encoding="utf-8", newline="\n")
    assert "depends_on" not in _NO_EDGE_PLAN
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert {r.id: r.deps for r in m.rows} == {"C1": (), "C2": (), "C3": ()}


def test_plan_rewritten_on_disk_mid_stage_refuses_and_stages_from_the_text_stage_read(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_stage as mod

    real = mod.scaffold_markers

    def rewrite_then_check(text):
        (repo / PLAN_REL).write_text(_BAD_EDGE_PLAN, encoding="utf-8", newline="\n")
        return real(text)

    # The text stage read is clean; the disk copy turns malformed afterwards.
    (repo / PLAN_REL).write_text(_NO_EDGE_PLAN, encoding="utf-8", newline="\n")
    monkeypatch.setattr(mod, "scaffold_markers", rewrite_then_check)
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert {r.id for r in m.rows} == {"C1", "C2", "C3"}

    # Reverse: the text stage read is malformed; the disk copy is clean afterwards. Still refused.
    def rewrite_clean(text):
        (repo / PLAN_REL).write_text(_NO_EDGE_PLAN, encoding="utf-8", newline="\n")
        return real(text)

    (repo / PLAN_REL).write_text(_BAD_EDGE_PLAN, encoding="utf-8", newline="\n")
    monkeypatch.setattr(mod, "scaffold_markers", rewrite_clean)
    with pytest.raises(AskStageError, match="gate_kind"):
        stage(repo, run_id="r2", plan_rel=PLAN_REL)


def test_spine_refusal_names_the_plan_and_the_text_it_parsed(repo):
    import hashlib

    (repo / PLAN_REL).write_text(_BAD_EDGE_PLAN, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(_BAD_EDGE_PLAN.encode("utf-8")).hexdigest()[:12]
    with pytest.raises(AskStageError) as ei:
        stage(repo, run_id="r1", plan_rel=PLAN_REL)
    msg = str(ei.value)
    assert msg.startswith(f"SPINE-REFUSED: {PLAN_REL}:") and "gate_kind" in msg
    assert f"sha256 {digest}" in msg


def _kept_world(repo, kept_run, own_paths):
    """`kept_run`'s record: t.py evicted (tree at HEAD bytes), declared write set `own_paths`."""
    import subprocess

    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    if not (repo / ".git").exists():
        git("init", "-q")
    (repo / "t.py").write_bytes(b"a = 1\n")
    git("add", "t.py")
    git("commit", "-q", "-m", "A")
    rec = repo / "state" / "warp" / kept_run
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "review_declared_paths": own_paths}), encoding="utf-8"
    )
    held = b"a = 1\nheld\n"
    (rec / "withheld-blobs").mkdir()
    (rec / "withheld-blobs" / blob_sha(held)).write_bytes(held)
    (rec / "evicted.json").write_text(
        json.dumps({"t.py": {"blob": blob_sha(held), "head": blob_sha(b"a = 1\n")}}), encoding="utf-8"
    )
    return held


def test_kept_row_whose_evicted_path_is_outside_the_kept_runs_write_set_stages_fresh(repo):
    from coordinator_core.ops.dispatch_emit import ask_stage, resume_snapshot

    _kept_world(repo, "kept-run", ["other.py"])
    snap = resume_snapshot.Snapshot(frozenset(), frozenset(), kept=frozenset({"R1"}), kept_run="kept-run")
    assert ask_stage._kept_row_holds(repo, snap, "R1", "report.md") is False
    assert (repo / "t.py").read_bytes() == b"a = 1\n"


def test_kept_row_restores_an_owned_evicted_path_before_reading_the_kept_record(repo):
    from coordinator_core.ops.dispatch_emit import ask_stage, resume_snapshot
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import write_object
    from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE

    held = _kept_world(repo, "kept-run", ["t.py"])
    oid = write_object(resolve_git_common_dir(repo), b"blob", held)
    (repo / "state/warp/kept-run" / WITHHELD_OWN_DELTA_FILE).write_text(
        json.dumps({"paths": {"t.py": {"blob": oid}}}), encoding="utf-8",
    )
    snap = resume_snapshot.Snapshot(frozenset(), frozenset(), kept=frozenset({"R1"}), kept_run="kept-run")
    ask_stage._kept_row_holds(repo, snap, "R1", "report.md")  # no kept.json: False, but the restore ran
    assert (repo / "t.py").read_bytes() == held


def _row(rid: str, writes, depends=()):
    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

    return WaveRow(rid, rid, "", writes, [], list(depends))


def _naive_wave_test_scopes(waves, root):
    from coordinator_core.ops.dispatch_emit.ask_stage import _test_scope

    out = {}
    for i in range(len(waves) - 1):
        scope = _test_scope(waves[: i + 1], root)[0]
        if scope:
            out[str(i)] = scope
    return out


def test_wave_test_scopes_equals_the_per_wave_terminal_test_scope(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import _wave_test_scopes
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "test_b.py").write_text("", encoding="utf-8")
    waves = [
        [_row("W0", ["pkg/a.py", "pkg/b.py", "docs/x.md"])],
        [_row("W1", UNDECLARED), _row("W1b", ["pkg/c.py", "pkg/tests/test_c.py"])],
        [_row("W2", ["pkg/d.py", "pkg/a.py"])],
        [_row("W3", ["pkg/e.py"])],
    ]
    got = _wave_test_scopes(waves, tmp_path)
    assert got == _naive_wave_test_scopes(waves, tmp_path)
    assert set(got) == {"0", "1", "2"}
    # `pkg/tests/test_c.py` is declared from wave 1 on: wave 0's scope must not carry it
    assert "pkg/tests/test_c.py" not in got["0"] and "pkg/tests/test_c.py" in got["1"]


def test_wave_test_scopes_a_later_declared_higher_priority_candidate_replaces_the_disk_one(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import _wave_test_scopes

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_b.py").write_text("", encoding="utf-8")
    waves = [
        [_row("W0", ["pkg/b.py", "pkg/other.py"])],
        [_row("W1", ["pkg/tests/test_b.py", "pkg/tests/test_other.py"])],
        [_row("W2", ["pkg/z.py"])],
    ]
    got = _wave_test_scopes(waves, tmp_path)
    assert got == _naive_wave_test_scopes(waves, tmp_path)
    assert got["0"] == ("pkg/test_b.py",)
    assert got["1"] == ("pkg/tests/test_b.py", "pkg/tests/test_other.py")


def test_wave_test_scopes_a_prefix_without_a_declared_write_has_no_scope_even_with_a_surface(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import _wave_test_scopes
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_a.py").write_text("", encoding="utf-8")
    first = _row("W0", UNDECLARED)._replace(surface="pkg/a.py")
    waves = [[first], [_row("W1", ["pkg/b.py"])], [_row("W2", ["pkg/c.py"])]]
    got = _wave_test_scopes(waves, tmp_path)
    assert got == _naive_wave_test_scopes(waves, tmp_path)
    assert "0" not in got and got["1"] == ("pkg/test_a.py",)


def test_widen_with_existing_tests_shares_one_probe_memo_across_rows(tmp_path, monkeypatch):
    import os

    from coordinator_core.ops.dispatch_emit import ask_stage

    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("", encoding="utf-8")
    rows = [["pkg/a.py"], ["pkg/b.py"], ["pkg/a.py", "pkg/c.py"]]
    expected = [ask_stage._widen_with_existing_tests(r, tmp_path) for r in rows]
    assert expected[0] == ["pkg/a.py", "pkg/tests/test_a.py"] and expected[1] == ["pkg/b.py"]

    probed: list[str] = []
    real = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda p: probed.append(str(p)) or real(p))
    shared: dict = {}
    assert [ask_stage._widen_with_existing_tests(r, tmp_path, shared) for r in rows] == expected
    assert len(probed) == len(set(probed)) == 3
    assert ("f", "pkg/tests/test_a.py") in shared


def test_wave_test_scopes_is_empty_without_a_testable_write_or_a_second_wave(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import _wave_test_scopes
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

    assert _wave_test_scopes([[_row("A", ["pkg/a.py"])]], tmp_path) == {}
    waves = [[_row("A", UNDECLARED)], [_row("B", ["docs/x.md"])], [_row("C", ["docs/y.md"])]]
    assert _wave_test_scopes(waves, tmp_path) == _naive_wave_test_scopes(waves, tmp_path) == {}


def _spy_end(monkeypatch, *, escrowed_token, boom=False):
    from coordinator_core.ops.dispatch_emit import live_run_index, run_token

    calls = []
    monkeypatch.setattr(run_token, "escrowed", lambda root, run_id: escrowed_token)
    monkeypatch.setattr(run_token, "discard", lambda root, run_id: calls.append("discard"))

    def _remove(root, run_id):
        calls.append("remove")
        if boom:
            raise OSError("index gone")

    monkeypatch.setattr(live_run_index, "remove", _remove)
    return calls


def test_a_refused_run_drops_its_unescrowed_token_and_its_index_entry(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_stage import _end_refused_run

    calls = _spy_end(monkeypatch, escrowed_token=None)
    _end_refused_run(tmp_path, "ask-1")
    assert calls == ["discard", "remove"]


def test_a_refused_launcher_child_keeps_its_escrowed_token_but_leaves_the_index(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_stage import _end_refused_run

    calls = _spy_end(monkeypatch, escrowed_token="tok")
    _end_refused_run(tmp_path, "ask-1")
    assert calls == ["remove"]


def test_a_cleanup_failure_never_masks_the_refusal(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_stage import _end_refused_run

    calls = _spy_end(monkeypatch, escrowed_token=None, boom=True)
    _end_refused_run(tmp_path, "ask-1")
    assert calls == ["discard", "remove"]


def _drive_refusal(tmp_path, monkeypatch, exc):
    from coordinator_core.ops.dispatch_emit import ask_stage

    ended = []
    monkeypatch.setattr(ask_stage, "_end_refused_run", lambda root, run_id: ended.append(run_id))
    monkeypatch.setattr(ask_stage, "main_worktree_root", lambda p: tmp_path)
    monkeypatch.setattr(ask_stage, "_consume_park_marker", lambda *a, **k: None)
    monkeypatch.setattr(ask_stage, "stamp_plan_phase", lambda *a, **k: None)
    monkeypatch.setattr(ask_stage, "_spine_sha", lambda *a, **k: "sha")
    monkeypatch.setattr(ask_stage, "record_dir", lambda root, run_id: tmp_path / "rec")
    monkeypatch.setattr(ask_stage, "stage", lambda *a, **k: (_ for _ in ()).throw(exc))
    reply = ask_stage._run_ask_stage({"run_id": "ask-20260101T000000-aaaaaa", "writes": []}, tmp_path)
    return reply, ended


def test_an_owed_handback_refusal_does_not_end_the_run(tmp_path, monkeypatch):
    exc = AskStageError("owed")
    exc.owed_em_ids = ["r1"]
    exc.plan_path = "p"
    reply, ended = _drive_refusal(tmp_path, monkeypatch, exc)
    assert reply["owed_em_ids"] == ["r1"]
    assert ended == []


def test_a_plain_refusal_ends_the_run(tmp_path, monkeypatch):
    reply, ended = _drive_refusal(tmp_path, monkeypatch, AskStageError("nope"))
    assert "nope" in reply["error"]
    assert ended == ["ask-20260101T000000-aaaaaa"]
