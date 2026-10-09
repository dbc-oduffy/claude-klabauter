"""Close routes for hand-landed work: --review-only row sources, --rejudge inputs, --box-terms."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks.block_dispatch_suite_invocation import _classify
from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.dispatch_emit.emit import _meta_block, compose_script
from coordinator_core.ops.dispatch_emit.spine_read import coded_row_ids, read_spine
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row
from coordinator_core.ops.review_stamp import MintRefusal, mint
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _plan, _record, _repo
from coordinator_core.session import record_homes

_BASE = "abc1234def"
_SPINE = (
    "---\nplan_id: pln-x\nstatus: executing\n---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: one\n  writes: [a.py]\n  disposition: coded\n"
    "- id: C2\n  title: two\n  writes: [b.py]\n  disposition: done\n"
    "- id: C3\n  title: three\n  writes: [c.py]\n```\n"
)


def _plan_file(tmp_path):
    path = tmp_path / "p.md"
    path.write_text(_SPINE, encoding="utf-8")
    return path


def _drive(tmp_path, monkeypatch, extra, *, run_base=True):
    """Run the CLI up to the engine hand-off; return the params it built."""
    captured = {}

    def fake(params, *a, **k):
        captured.update(params)
        return {"ok": True, "path": str(tmp_path / "o.workflow.mjs")}

    monkeypatch.setattr(cli, "_dispatch_emit", fake)
    monkeypatch.chdir(tmp_path)
    argv = ["--plan", str(_plan_file(tmp_path)), "--out", str(tmp_path / "o.workflow.mjs"), *extra]
    if run_base:
        argv += ["--run-base", _BASE]
    cli.main(argv)
    assert captured, "CLI never reached the engine"
    return captured


def test_coded_row_ids_folds_the_done_alias(tmp_path):
    assert coded_row_ids(_plan_file(tmp_path)) == {"C1", "C2"}


def test_keep_coded_returns_the_coded_row_as_a_row(tmp_path):
    plan = _plan_file(tmp_path)

    assert [r.id for r in read_spine(plan)] == ["C3"]
    assert [r.id for r in read_spine(plan, keep_coded=frozenset({"C1"}))] == ["C1", "C3"]


def test_review_only_with_no_source_derives_the_coded_rows(tmp_path, monkeypatch):
    params = _drive(tmp_path, monkeypatch, ["--review-only"])

    assert params["review_only_rows"] == ["C1", "C2"]
    assert params["run_base_sha"] == _BASE


def test_rows_flag_names_rows_explicitly(tmp_path, monkeypatch):
    params = _drive(tmp_path, monkeypatch, ["--review-only", "--rows", "C2, C3"])

    assert params["review_only_rows"] == ["C2", "C3"]


def test_checkpoint_subjects_union_with_rows(tmp_path, monkeypatch):
    run = tmp_path / "run.txt"
    run.write_text("x checkpoint(wave 1): 1 rows — C1", encoding="utf-8")

    params = _drive(tmp_path, monkeypatch, ["--review-only", str(run), "--rows", "C3"])

    assert params["review_only_rows"] == ["C1", "C3"]


def test_rows_without_review_only_is_a_usage_error(tmp_path):
    assert cli.main(["--plan", str(_plan_file(tmp_path)), "--rows", "C1"]) == cli.EXIT_USAGE


def test_review_only_script_composes_over_a_coded_row(tmp_path):
    from coordinator_core.ops.dispatch_emit.emit import emit_script

    script = emit_script(
        _plan_file(tmp_path),
        repo_root=tmp_path,
        review_only_rows=frozenset({"C1"}),
        run_base_sha=_BASE,
        review_roster_fragment=REVIEW_KW["review_roster_fragment"],
        review_stage_schemas=REVIEW_KW["review_stage_schemas"],
    )

    assert "Review-only: rows C1, base " + _BASE in script


def _unmet_run(tmp_path, status="not_met"):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(
        repo, head, criterion={"status": status, "observation": "o", "sidecar": None},
        tests={"status": "pass", "run": 1, "failed": 0, "sidecar": None},
    )
    return repo, head, record


@pytest.mark.parametrize("status", ["not_met", "indeterminate"])
def test_rejudge_emits_from_a_recorded_unmet_delivery_verdict(tmp_path, monkeypatch, status):
    repo, head, record = _unmet_run(tmp_path, status)
    monkeypatch.setattr(rd, "resolve_delivery_in_force", lambda *a, **k: (record, {"verdict": "PASS"}))
    monkeypatch.setattr(rd, "_head_sha", lambda root: head)
    plan = _plan(repo)
    out = repo / "docs" / "plans" / "p.rejudge.workflow.mjs"

    result = rd.emit_rejudge(repo_root=repo, plan_path=str(plan), out_path=str(out))

    assert result["path"] == str(out) and out.exists()
    assert rd.recorded_unmet_criterion(repo, "x", str(plan)) == {"status": status, "run_base_sha": head}


def test_rejudge_still_refuses_when_nothing_records_an_unmet_criterion(tmp_path, monkeypatch):
    repo, head, record = _unmet_run(tmp_path, "met")
    out = str(repo / "o.workflow.mjs")
    monkeypatch.setattr(rd, "resolve_delivery_in_force", lambda *a, **k: (record, {"verdict": "PASS"}))

    with pytest.raises(rd.ReverifyRefused, match="no unmet review_stamp criterion"):
        rd.emit_rejudge(repo_root=repo, plan_path=str(_plan(repo)), out_path=out)

    monkeypatch.setattr(rd, "resolve_delivery_in_force", lambda *a, **k: (None, None))
    with pytest.raises(rd.ReverifyRefused):
        rd.emit_rejudge(repo_root=repo, plan_path=str(_plan(repo)), out_path=out)


def test_mint_refuses_not_met_until_a_met_rejudge_record_follows(tmp_path):
    repo, head, record = _unmet_run(tmp_path)
    with pytest.raises(MintRefusal, match="criterion"):
        mint(_plan(repo), repo, build_test_path=None, superseding_record=record)

    plan_id = yaml.safe_load(_plan(repo).read_text(encoding="utf-8").split("---\n")[1])["plan_id"]
    path = Path(record_homes.home_dir(str(repo), "delivery-verdicts")) / "2026-10" / "x.rejudge.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"kind": "criterion-rejudge", "plan_id": plan_id, "head_sha": head,
          "recorded_at": "2026-10-02T00:00:00.000000Z",
          "criterion": {"status": "met", "observation": "judged", "sidecar": None}}
    path.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")

    stamp = mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["criterion"]["status"] == "met"


def _terms(tmp_path, text="no next build\nno pnpm build (12 GB)\n"):
    path = tmp_path / "terms.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_box_terms_reach_every_brief_via_the_preamble(tmp_path, monkeypatch):
    params = _drive(tmp_path, monkeypatch, ["--box-terms", str(_terms(tmp_path))], run_base=False)

    assert params["box_terms"] == ["no next build", "no pnpm build (12 GB)"]
    assert params["preamble"] == (
        "Box terms (from the driver; binding):\n- no next build\n- no pnpm build (12 GB)"
    )


def test_box_terms_append_after_a_named_preamble(tmp_path, monkeypatch):
    pre = tmp_path / "pre.md"
    pre.write_text("POSTURE", encoding="utf-8")

    params = _drive(
        tmp_path, monkeypatch, ["--preamble", str(pre), "--box-terms", str(_terms(tmp_path))], run_base=False
    )

    assert params["preamble"].startswith("POSTURE\n\nBox terms (from the driver; binding):")


def test_empty_or_unreadable_box_terms_refused(tmp_path):
    base = ["--plan", str(_plan_file(tmp_path)), "--out", str(tmp_path / "o.workflow.mjs"), "--box-terms"]

    assert cli.main([*base, str(_terms(tmp_path, "\n  \n"))]) == cli.EXIT_USAGE
    assert cli.main([*base, str(tmp_path / "missing.txt")]) == cli.EXIT_USAGE


def test_box_terms_recorded_in_script_meta_and_guard_clean():
    script = compose_script(
        [[_write_row("C1")]],
        name="wf", description="d", box_terms=("no next build", "no pnpm build"),
        preamble="Box terms (from the driver; binding):\n- no next build",
        **REVIEW_KW,
    )

    assert "boxTerms: ['no next build', 'no pnpm build']" in script.replace('"', "'")
    assert "Box terms (from the driver; binding)" in script
    assert [h for h in _classify(script, None) if h.position == "imperative"] == []


def test_no_box_terms_leaves_meta_unchanged():
    assert "boxTerms" not in _meta_block("wf", "d", ["P"])


@pytest.mark.parametrize("route", [["--rejudge"], ["--reverify-delivery", "run.json"]])
def test_close_route_fire_fires_and_prints_the_handle(tmp_path, monkeypatch, capsys, route):
    import coordinator_core.ops.workflow_fire.fire as fire_module

    script = tmp_path / "p.workflow.mjs"
    script.write_text("// emitted\n", encoding="utf-8")
    monkeypatch.setattr(rd, "emit_rejudge", lambda **_: {"path": str(script)})
    monkeypatch.setattr(rd, "emit_reverify", lambda **_: {"path": str(script)})
    fired = []
    monkeypatch.setattr(fire_module, "fire_workflow", lambda path, cwd=None: fired.append(path) or {"fire_id": "f1"})

    code = cli.main([*route, "--plan", str(tmp_path / "p.md"), "--repo-root", str(tmp_path), "--fire"])

    assert code == 0 and fired == [str(script)]
    assert '"fire_id": "f1"' in capsys.readouterr().out


def test_box_terms_default_to_the_machine_local_path(tmp_path, monkeypatch):
    from coordinator_core import machine_resolver

    terms = _terms(tmp_path)
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: str(terms) if key == cli.BOX_TERMS_KEY else None
    )

    params = _drive(tmp_path, monkeypatch, [], run_base=False)

    assert params["box_terms"] == ["no next build", "no pnpm build (12 GB)"]
