"""review_stamp.accept — a PM-accepted verdict refusal is recorded and the stamp it binds is minted."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.tests.test_review_stamp import (
    _commit,
    _mint_success_fixture,
    _setup_repo,
    _stage_record_fixture,
    _write_sidecar,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN_ID = "pln-example-abc123"
_UTTERANCE = "ship it, the FAIL is the verifier's blind spot"
_REF = "chat:2026-10-14T10:00Z"
_SHARE = ".coordinator-local/subagent-share/sess1"


def _plan(repo: Path) -> Path:
    return repo / "docs" / "plans" / "example.md"


def _fm(plan: Path) -> dict:
    split = split_frontmatter(plan.read_text(encoding="utf-8").replace("\r\n", "\n"))
    return yaml.safe_load(split.fm_text)


def _accept(repo: Path, build_test, **kw):
    return m.accept(
        _plan(repo),
        repo,
        pm_utterance=kw.pop("pm_utterance", _UTTERANCE),
        pm_utterance_ref=kw.pop("pm_utterance_ref", _REF),
        build_test_path=str(build_test) if build_test else None,
        **kw,
    )


def _fail_delivery_terminal(repo: Path) -> str:
    """A newer terminal commit whose delivery verdict is FAIL; returns its sha."""
    share = repo / _SHARE
    _write_sidecar(share / "delivery-fail.md", {"verdict": "FAIL"})
    _write_sidecar(
        share / "prep-fail.md",
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {"delivery": f"{_SHARE}/delivery-fail.md"},
        },
    )
    _write_sidecar(
        share / "integration-fail.md",
        {
            "plan_id": _PLAN_ID,
            "prep_sidecar": f"{_SHARE}/prep-fail.md",
            "unresolved": [],
            "confinement_violations": [],
        },
    )
    return _commit(repo, "land fail\n\nInline-Review: applies integration-fail -- execute-review: 1 slices, 0 fixes")


def _fail_repo(tmp_path: Path):
    repo = _setup_repo(tmp_path)
    _, build_test = _mint_success_fixture(repo)
    terminal = _fail_delivery_terminal(repo)
    return repo, build_test, terminal


def test_a_delivery_fail_plus_accept_writes_both_blocks_and_the_gates_pass(tmp_path):
    repo, build_test, terminal = _fail_repo(tmp_path)
    done = _accept(repo, build_test)
    fm = _fm(_plan(repo))
    assert fm["review_stamp"]["delivery"]["verdict"] == "FAIL"
    assert fm["review_stamp"]["terminal_commit_sha"] == terminal
    acceptance = fm["review_stamp_acceptance"]
    assert acceptance["pm_utterance"] == _UTTERANCE
    assert acceptance["pm_utterance_ref"] == _REF
    assert acceptance["terminal_commit_sha"] == terminal
    assert "delivery verdict is 'FAIL'" in acceptance["refusal"]
    assert acceptance["accepted_at"]
    assert done["review_stamp_acceptance"] == acceptance
    plan_text = _plan(repo).read_text(encoding="utf-8")
    assert plan_text.count("review_stamp:") == 1 and plan_text.count("review_stamp_acceptance:") == 1
    assert m.subject_refusal(_plan(repo), repo, split_frontmatter(plan_text.replace("\r\n", "\n")).fm_text) is None
    assert m.check(_plan(repo), repo, supersession=True) is None


def test_b_accept_on_a_plan_that_mints_cleanly_refuses_and_writes_nothing(tmp_path):
    repo = _setup_repo(tmp_path)
    _, build_test = _mint_success_fixture(repo)
    before = _plan(repo).read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="nothing to accept"):
        _accept(repo, build_test)
    assert _plan(repo).read_text(encoding="utf-8") == before


def test_c_a_structural_refusal_refuses_accept(tmp_path):
    repo = _setup_repo(tmp_path)
    before = _plan(repo).read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="no terminal commit"):
        _accept(repo, None)
    assert _plan(repo).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("utterance,ref", [("", _REF), ("   ", _REF), (_UTTERANCE, ""), (_UTTERANCE, "  ")])
def test_d_an_empty_utterance_or_ref_refuses(tmp_path, utterance, ref):
    repo, build_test, _ = _fail_repo(tmp_path)
    before = _plan(repo).read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="both required"):
        _accept(repo, build_test, pm_utterance=utterance, pm_utterance_ref=ref)
    assert _plan(repo).read_text(encoding="utf-8") == before


def test_e_an_existing_acceptance_on_another_terminal_refuses(tmp_path):
    repo, build_test, _ = _fail_repo(tmp_path)
    plan = _plan(repo)
    text = plan.read_text(encoding="utf-8")
    plan.write_text(
        text.replace("---\n\n# Example", "review_stamp_acceptance:\n  terminal_commit_sha: 'aaaa" + "0" * 36 + "'\n---\n\n# Example", 1),
        encoding="utf-8",
    )
    before = plan.read_text(encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="already bound to"):
        _accept(repo, build_test)
    assert plan.read_text(encoding="utf-8") == before


def test_f_a_later_commit_on_a_scope_path_still_fails_the_supersession_check(tmp_path):
    repo, build_test, _ = _fail_repo(tmp_path)
    _accept(repo, build_test)
    (repo / "coordinator_core" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    _commit(repo, "later change")
    assert "a later commit touches a declared write" in m.check(_plan(repo), repo, supersession=True)


def test_g_a_fail_refusal_names_accept(tmp_path):
    repo, build_test, _ = _fail_repo(tmp_path)
    with pytest.raises(m.MintRefusal, match="review-stamp accept") as exc:
        m.mint(_plan(repo), repo, build_test_path=str(build_test))
    assert "--pm-utterance <verbatim> --pm-utterance-ref <ref>" in str(exc.value)


def test_a_structural_refusal_does_not_offer_accept(tmp_path):
    repo = _setup_repo(tmp_path)
    with pytest.raises(m.MintRefusal) as exc:
        m.mint(_plan(repo), repo, build_test_path=None)
    assert "review-stamp accept" not in str(exc.value)


_UNMET = {"status": "not_met", "observation": "o", "sidecar": None}
_PASS_TESTS = {"status": "pass", "run": 3, "failed": 0, "sidecar": "x.md"}


def test_a_criterion_acceptance_is_honoured_while_the_terminal_is_unchanged(tmp_path):
    repo = _setup_repo(tmp_path)
    _stage_record_fixture(repo, tests=_PASS_TESTS, criterion=_UNMET)
    plan = _plan(repo)
    _accept(repo, None)
    fm_text = split_frontmatter(plan.read_text(encoding="utf-8").replace("\r\n", "\n")).fm_text
    assert m.criterion_refusal(plan, fm_text) is None
    assert _fm(plan)["review_stamp"]["criterion"]["status"] == "not_met"


def test_a_delivery_acceptance_does_not_clear_an_unmet_criterion(tmp_path):
    repo = _setup_repo(tmp_path)
    _stage_record_fixture(repo, tests=_PASS_TESTS, criterion=_UNMET)
    plan = _plan(repo)
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "---\n\n# Example",
            "review_stamp:\n  terminal_commit_sha: abc\n  criterion:\n    status: not_met\n"
            "review_stamp_acceptance:\n  terminal_commit_sha: abc\n  refusal: 'delivery verdict is FAIL'\n---\n\n# Example",
            1,
        ),
        encoding="utf-8",
    )
    fm_text = split_frontmatter(plan.read_text(encoding="utf-8").replace("\r\n", "\n")).fm_text
    assert "criterion is not_met" in m.criterion_refusal(plan, fm_text)


def _superseding_record(repo: Path, head: str) -> Path:
    path = repo / "state" / "superseding-reviews" / "2026-10" / "rec.md"
    _write_sidecar(
        path,
        {
            "kind": "superseding-review",
            "plan_id": _PLAN_ID,
            "commit_range": {"base": head, "head": head},
            "supersedes": None,
            "prep_sidecar": None,
            "unresolved": [],
            "confinement_violations": 0,
            "fixes_applied": 0,
            "slices": 1,
            "brief_conformance": {"items": 0, "met": 0, "unmet": 0},
            "prep": {"run_base_sha": head, "product_files": 1, "foreign_claims": [], "slice_files": ["a.txt"]},
            "delivery": {"verdict": "PASS", "product_files": 1},
            "criterion": _UNMET,
        },
    )
    return path


def test_h_a_superseding_acceptance_binds_its_terminal_and_a_remint_that_moves_it_is_not_honoured(tmp_path):
    repo = _setup_repo(tmp_path)
    walked, _, _ = _stage_record_fixture(repo, tests=_PASS_TESTS, criterion=_UNMET)
    (repo / "later.txt").write_text("x\n", encoding="utf-8")
    head = _commit(repo, "later")
    assert head != walked
    plan = _plan(repo)
    runner = repo / _SHARE / "runner.md"
    _write_sidecar(runner, {"status": "pass", "run": 7, "failed": 0})

    _accept(repo, runner, superseding_record=_superseding_record(repo, head))
    fm = _fm(plan)
    assert fm["review_stamp"]["terminal_commit_sha"] == head
    assert fm["review_stamp_acceptance"]["terminal_commit_sha"] == head
    text = split_frontmatter(plan.read_text(encoding="utf-8").replace("\r\n", "\n")).fm_text
    assert m.criterion_refusal(plan, text) is None

    m.mint(plan, repo, build_test_path=None, criterion_open_ok=True)
    remint = _fm(plan)
    assert remint["review_stamp"]["terminal_commit_sha"] == walked
    assert remint["review_stamp_acceptance"]["terminal_commit_sha"] == head
    text = split_frontmatter(plan.read_text(encoding="utf-8").replace("\r\n", "\n")).fm_text
    assert "criterion is not_met" in m.criterion_refusal(plan, text)
