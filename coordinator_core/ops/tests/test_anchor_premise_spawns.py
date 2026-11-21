"""Pure tests for ops/_anchor_premise.py."""

import hashlib

import pytest

from coordinator_core.ops._anchor_premise import anchor_findings
from pathlib import Path

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

GOOD = {"id": "AP1", "claim": "data lives in x", "check": {"command": "grep -c a b", "expect": "1"}}


def _h(line) -> str:
    return hashlib.sha256(line.rstrip().encode()).hexdigest()


A = [{"id": "AP1", "claim": "c" * 300}]


_AP1_CLAIM = "fan_launcher.write_launcher requires a fifth positional emitted argument at HEAD"
_LAUNCHER = (
    "def _render(root, children, emitted: Sequence[Mapping[str, Any]]) -> str:\n"
    "def write_launcher(\n"
    "    emitted: Sequence[Mapping[str, Any]],\n"
)


def _repo_with_baseline(tmp_path) -> str:
    import subprocess

    def git(*a) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    target = tmp_path / "mod.py"
    target.write_text("refuse here\n")
    git("add", "mod.py")
    git("commit", "-q", "-m", "base")
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    target.write_text("converted\n")
    return sha


BASELINE_ONLY = {"id": "AP1", "claim": "c", "check": {"command": "grep -c refuse mod.py", "expect": "1"}}


@pytest.mark.spawns_process
def test_baseline_only_anchor_falsified_by_own_run_reanchors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    monkeypatch.setattr(ap, "_own_context", lambda text, root: (sha, {"mod.py": "ask-1"}))
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")
    assert v["observed"].startswith(ap.REANCHORED)
    assert ap.anchor_findings([BASELINE_ONLY], [], [], "p", tmp_path) == []


@pytest.mark.spawns_process
def test_baseline_only_anchor_still_contradicts_without_own_run_write_or_baseline_hold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    monkeypatch.setattr(ap, "_own_context", lambda text, root: (sha, {"other.py": "ask-1"}))
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"
    monkeypatch.setattr(ap, "_own_context", lambda text, root: None)
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"
    never_held = {**BASELINE_ONLY, "check": {"command": "grep -c refuse mod.py", "expect": "5"}}
    monkeypatch.setattr(ap, "_own_context", lambda text, root: (sha, {"mod.py": "ask-1"}))
    (v,) = ap.anchor_verdicts([never_held], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"


def _plan(falsifier_ref=None) -> str:
    falsifier = f"  falsifier:\n    baseline_ref: {falsifier_ref}\n" if falsifier_ref else ""
    return f"---\nsizing_object: sz.yaml\nprime_exit_criterion:\n  statement: s\n{falsifier}---\n# p\n"


def _own_run(monkeypatch, ap, written, baseline) -> None:
    from coordinator_core.ops import _own_run_premise

    monkeypatch.setattr(_own_run_premise, "own_run_paths", lambda root, sizing: written)
    monkeypatch.setattr(ap, "_run_baseline", lambda root, run_id: baseline)


@pytest.mark.spawns_process
def test_plan_without_baseline_ref_judges_own_run_anchor_at_the_run_halt_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    _own_run(monkeypatch, ap, {"mod.py": "ask-1"}, sha)
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [], [], _plan(), tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")
    assert v["observed"].startswith(ap.REANCHORED)


@pytest.mark.spawns_process
def test_plan_without_baseline_ref_keeps_a_file_the_run_did_not_write_contradicted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    _own_run(monkeypatch, ap, {"other.py": "ask-1"}, sha)
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [], [], _plan(), tmp_path)
    assert v["verdict"] == "CONTRADICTED"
    _own_run(monkeypatch, ap, {"mod.py": "ask-1"}, None)
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [], [], _plan(), tmp_path)
    assert v["verdict"] == "CONTRADICTED"


@pytest.mark.spawns_process
def test_run_baseline_reads_run_base_sha_from_a_committed_review_blocked_halt(tmp_path: Path) -> None:
    import json
    import subprocess

    from coordinator_core.ops import _anchor_premise as ap

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def git(*a) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True,
                              text=True, creationflags=flags).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    run = "ask-20261106T000000-abcdef"
    base = "a" * 40
    record = tmp_path / "state" / "warp" / run
    record.mkdir(parents=True)
    (record / "halted.json").write_text(json.dumps({"halted": "review_blocked", "run_base_sha": base}))
    git("add", "-A")
    git("commit", "-q", "-m", f"warp: record halted {run} run artifacts", "-m", "Halted-By: review_blocked")
    (record / "halted.json").unlink()
    assert ap._run_baseline(tmp_path, run) == base
    assert ap._run_baseline(tmp_path, "ask-20261106T000001-abcdef") is None


def _named_ref_anchor(sha, claim=None):
    return {
        "id": "AP1",
        "claim": claim or f"at baseline_ref {sha} mod.py has no 'refuse'",
        "check": {"command": "grep -c refuse mod.py", "expect": "0"},
    }


@pytest.mark.spawns_process
def test_claim_naming_baseline_ref_is_checked_at_that_ref(tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    (tmp_path / "mod.py").write_text("refuse\nrefuse\n")
    on_ref = {**_named_ref_anchor(sha), "check": {"command": "grep -c refuse mod.py", "expect": "1"}}
    (v,) = ap.anchor_verdicts([on_ref], [], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")
    no_ref = {**on_ref, "claim": "mod.py has one refuse"}
    (v,) = ap.anchor_verdicts([no_ref], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"
    wrong_on_ref = {**on_ref, "check": {"command": "grep -c refuse mod.py", "expect": "2"}}
    (v,) = ap.anchor_verdicts([wrong_on_ref], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"


@pytest.mark.spawns_process
def test_bare_baseline_ref_claim_uses_the_falsifier_ref(tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    plan = f"---\nprime_exit_criterion:\n  falsifier:\n    baseline_ref: {sha}\n---\n# p\n"
    anchor = {
        "id": "AP1",
        "claim": "at baseline_ref mod.py holds refuse once",
        "check": {"command": "grep -c refuse mod.py", "expect": "1"},
    }
    (v,) = ap.anchor_verdicts([anchor], [], [], plan, tmp_path)
    assert v["verdict"] == "RESOLVES"
    (v,) = ap.anchor_verdicts([anchor], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"


def _base_anchor(aid: str = "AP1", claim: str = "mod.py holds refuse once", cmd: str = "grep -c refuse mod.py", expect: str = "1"):
    return {"id": aid, "claim": claim, "check": {"command": cmd, "expect": expect}}


def _count_spawns(monkeypatch):
    from coordinator_core.git import run as git_run

    calls = []
    real = git_run.run_git

    def counting(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(git_run, "run_git", counting)
    return calls


@pytest.mark.spawns_process
def test_base_ref_reads_a_premise_true_at_the_ref_but_absent_from_the_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    (tmp_path / "mod.py").write_text("changed\n")
    anchors = [_base_anchor("AP1"), _base_anchor("AP2", cmd="grep -c here mod.py")]
    calls = _count_spawns(monkeypatch)
    verdicts = ap.anchor_verdicts(anchors, [], [], "p", tmp_path, base_ref=sha)
    assert [(v["verdict"], v["source"]) for v in verdicts] == [("RESOLVES", "engine-check")] * 2
    assert [c for c in calls if "cat-file" in c].__len__() == 1 and len(calls) == 1
    assert ap.anchor_findings(anchors, [], [], "p", tmp_path, base_ref=sha) == []


@pytest.mark.spawns_process
def test_no_base_ref_reads_the_tree_with_zero_spawns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    _repo_with_baseline(tmp_path)
    (tmp_path / "mod.py").write_text("changed\n")
    calls = _count_spawns(monkeypatch)
    (v,) = ap.anchor_verdicts([_base_anchor()], [], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED" and calls == []


@pytest.mark.spawns_process
def test_claim_baseline_ref_wins_over_base_ref(tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    claimed = _base_anchor(claim=f"at baseline_ref {sha} mod.py holds refuse once")
    (v,) = ap.anchor_verdicts([claimed], [], [], "p", tmp_path, base_ref="no-such-ref")
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")


@pytest.mark.spawns_process
def test_unreadable_base_ref_leaves_source_none_naming_the_ref(tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    _repo_with_baseline(tmp_path)
    (v,) = ap.anchor_verdicts([_base_anchor()], [], [], "p", tmp_path, base_ref="no-such-ref")
    assert v["verdict"] is None and v["source"] == "none" and "no-such-ref" in v["observed"]
    assert ap.anchor_findings([_base_anchor()], [], [], "p", tmp_path, base_ref="no-such-ref") == []


@pytest.mark.spawns_process
def test_a_stage_ref_wins_over_base_ref(tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    (tmp_path / "mod.py").write_text("changed\n")
    anchor = _base_anchor(cmd=f"git grep -c refuse {sha} -- mod.py")
    (v,) = ap.anchor_verdicts([anchor], [], [], "p", tmp_path, base_ref="no-such-ref")
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")


@pytest.mark.spawns_process
@pytest.mark.parametrize("lane", ["row", "carry"])
def test_row_or_carry_contradicted_on_own_paths_is_reanchored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lane) -> None:
    """df88cd gap (b): a premise-checker row's CONTRADICTED on a file only the run's own work changed
    took no own-run regrade (only the engine-check branch did)."""
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    monkeypatch.setattr(ap, "_own_context", lambda text, root: (sha, {"mod.py": "ask-1"}))
    row = {"anchor": "AP1", "verdict": "CONTRADICTED"}
    rows, carry = ([row], []) if lane == "row" else ([], [{**row, "line_hash": _h("p")}])
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], rows, carry, "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", lane)
    assert v["observed"].startswith(ap.REANCHORED)
    assert ap.anchor_findings([BASELINE_ONLY], rows, carry, "p", tmp_path) == []


@pytest.mark.spawns_process
def test_row_contradicted_on_a_path_the_run_did_not_write_still_contradicts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    row = {"anchor": "AP1", "verdict": "CONTRADICTED"}
    monkeypatch.setattr(ap, "_own_context", lambda text, root: (sha, {"other.py": "ask-1"}))
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [row], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("CONTRADICTED", "row")
    monkeypatch.setattr(ap, "_own_context", lambda text, root: None)
    (v,) = ap.anchor_verdicts([BASELINE_ONLY], [row], [], "p", tmp_path)
    assert v["verdict"] == "CONTRADICTED"


@pytest.mark.spawns_process
def test_base_ref_skips_reanchor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    sha = _repo_with_baseline(tmp_path)
    monkeypatch.setattr(ap, "_own_context", lambda text, root: (sha, {"mod.py": "ask-1"}))
    broken = _base_anchor(expect="2")
    (v,) = ap.anchor_verdicts([broken], [], [], "p", tmp_path, base_ref=sha)
    assert v["verdict"] == "CONTRADICTED" and not v["observed"].startswith(ap.REANCHORED)
