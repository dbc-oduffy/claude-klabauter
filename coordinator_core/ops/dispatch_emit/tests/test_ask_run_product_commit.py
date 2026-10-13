"""dispatch.terminal_commit on a warp ask run: product only, process records named by trailer,
and the requirements ledger's verdict gating the commit."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.completion_receipts import store
from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DLV = "dlv-alpha-aaaaaa"
RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"
NODE = "tests/test_a.py::test_x"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_SID = "11111111-2222-3333-4444-555555555555"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    # The in-run plan is authored mid-run: on disk, never committed.
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\nprime_exit_criterion:\n"
        f"  statement: done\n  falsifier:\n    how: pytest {NODE} -q\n    expected_when_true: passes\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    run_dir = root / RUN_DIR_ROOT / RUN
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    rl.seed(run_dir, RUN, ["ship a.py"], origin=rl.ORIGIN_PM_VERBATIMS, all_of_pm_ask=False)
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _call(repo: Path, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met"}]}, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_ask_run_lands_product_only_and_names_its_process_records_by_trailer(repo):
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    _write(repo, "docs/delivery/run1.md", "# delivered\n")
    _write(repo, "docs/reference/x.md", "# ref\n")
    out = _call(
        repo, doc_paths=["docs/delivery/run1.md", PLAN, "docs/reference/x.md"],
        doc_writes=["docs/delivery/run1.md", PLAN, "docs/reference/x.md"],
    )
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/delivery/run1.md"}
    assert sorted(out["doc_paths_dropped"]) == [PLAN, "docs/reference/x.md"]
    # One commit: no second, plan-only stamp commit.
    assert _git(repo, "rev-parse", "HEAD~1").strip() == head_before
    assert _git(repo, "rev-parse", "HEAD").strip() == out["sha"]
    message = _git(repo, "log", "-1", "--format=%B", out["sha"])
    assert f"Falsifier: {NODE}" in message and "Resolves-Sizing: 20260101-a S" in message
    receipt = out["receipts"][0]
    assert receipt.startswith(".coordinator-local/state/completion-receipts/")
    assert f"Completion-Receipt: {Path(receipt).stem}" in message
    assert store.introducing_commits(repo, [receipt]) == {receipt: out["sha"]}
    assert [r["_path"] for r in store.read_receipts(repo)] == [receipt]
    # The process records moved on disk, uncommitted: the sizing shipped, the plan row coded.
    import yaml

    sizing = yaml.safe_load((repo / SIZING).read_text(encoding="utf-8"))
    assert sizing["status"] == "shipped" and sizing["resolution"] == {"sha": out["sha"], "run_id": RUN}
    assert "resolution_error" not in out
    assert f"disposition_ref: {out['sha']}" in (repo / PLAN).read_text(encoding="utf-8")


def test_a_ledgered_run_without_a_verdict_is_refused(repo):
    out = _call(repo, ledger_verdict=None)
    assert out["committed"] is False and out["refused"] == "ledger-unjudged"
    assert _git(repo, "log", "--format=%s").splitlines() == ["seed"]


def test_a_revision_the_checker_never_saw_refuses_the_commit(repo):
    rl.revise(repo, RUN, verbatim="and b.py too")
    out = _call(repo)
    assert out["refused"] == "ledger-unmet"
    assert out["ledger_failures"] == [{"id": "R2", "reason": "unjudged (arrived after the check, or skipped)"}]


def test_an_inessential_unmet_item_passes_without_all_of_pm_ask(repo):
    rl.revise(repo, RUN, verbatim="nice to have: a changelog line")
    verdict = {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "not_met", "essential": False}]}
    assert _call(repo, ledger_verdict=verdict)["committed"] is True


def test_all_of_pm_ask_set_mid_run_makes_every_item_mandatory(repo):
    rl.revise(repo, RUN, verbatim="nice to have: a changelog line", all_of_pm_ask=True)
    verdict = {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "not_met", "essential": False}]}
    out = _call(repo, ledger_verdict=verdict)
    assert out["refused"] == "ledger-unmet" and out["all_of_pm_ask"] is True


def test_a_run_emitted_before_the_ledger_is_not_gated(repo):
    (repo / RUN_DIR_ROOT / RUN / rl.LEDGER_FILE).unlink()
    assert _call(repo, ledger_verdict=None)["committed"] is True


def test_review_trailer_pins_the_uncommitted_review_record(tmp_path):
    record = tmp_path / "rec.md"
    record.write_text("---\nplan_id: p\n---\n", encoding="utf-8")
    trailers = terminal_commit._ask_trailers(tmp_path, None, record, [])
    assert trailers == [f"Review: {hashlib.sha256(record.read_bytes()).hexdigest()}"]


def test_the_sizings_doc_consumers_and_ask_conflicts_ride_the_product_commit(repo):
    text = (repo / SIZING).read_text(encoding="utf-8")
    _write(
        repo, SIZING,
        text + 'doc_consumers: ["docs/reference/x.md:3", "state/sizings/other.yaml"]\n'
        'ask_conflicts: [{"path": "docs/decisions/DR-1.md#rule", "conflict": "says the opposite"}]\n',
    )
    for rel in ("docs/reference/x.md", "docs/decisions/DR-1.md", "docs/reference/y.md", "state/sizings/other.yaml"):
        _write(repo, rel, "# doc\n")
    every = ["docs/reference/x.md", "docs/decisions/DR-1.md", "docs/reference/y.md", "state/sizings/other.yaml"]
    out = _call(repo, doc_paths=every, doc_writes=every)
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/reference/x.md", "docs/decisions/DR-1.md"}
    assert sorted(out["doc_paths_dropped"]) == ["docs/reference/y.md", "state/sizings/other.yaml"]


_L_REVIEW = {
    **_REVIEWED,
    "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
    "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
    "tests": {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"},
    "criterion": {"status": "met", "observation": "a.py exists", "sidecar": None},
}


def _size_l(repo):
    _write(repo, SIZING, (repo / SIZING).read_text(encoding="utf-8").replace("tshirt: S", "tshirt: L"))


def test_the_ledger_closeout_stand_in_is_gone():
    assert not hasattr(terminal_commit, "_ledger_closeout")


def test_an_l_run_with_a_passing_ledger_but_null_delivery_does_not_close(repo):
    _size_l(repo)
    out = _call(repo, inline_review={**_REVIEWED, "delivery": None, "criterion": None})
    assert out["committed"] is True and out.get("plan_status") != "implemented", out
    assert {"review", "completion-entry"} <= set(out["close"]["outstanding"])


def test_an_l_run_with_a_failing_ledger_is_refused_though_delivery_passes(repo):
    _size_l(repo)
    out = _call(repo, inline_review=_L_REVIEW, ledger_verdict={"items": [{"id": "R1", "status": "not_met"}]})
    assert out["committed"] is False and out["refused"] == "ledger-unmet"


def test_an_l_run_with_ledger_delivery_and_criterion_passing_commits(repo):
    _size_l(repo)
    out = _call(repo, inline_review=_L_REVIEW)
    assert out["committed"] is True, out
    assert "a.py" in _git(repo, "show", "--name-only", "--format=", out["sha"])
    assert out["close"]["legs"]["completion-entry"]["detail"] != "criterion absent"


def test_a_peer_dirty_wiki_page_outside_doc_writes_is_not_committed(repo):
    _write(repo, "docs/delivery/run1.md", "# delivered\n")
    _write(repo, "docs/wiki/peer.md", "# peer wip\n")
    out = _call(
        repo, doc_paths=["docs/delivery/run1.md", "docs/wiki/peer.md"], doc_writes=["docs/delivery/run1.md"]
    )
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/delivery/run1.md"}
    assert out["doc_paths_dropped"] == ["docs/wiki/peer.md"]


def test_doc_paths_without_doc_writes_are_all_dropped(repo):
    _write(repo, "docs/delivery/run1.md", "# delivered\n")
    out = _call(repo, doc_paths=["docs/delivery/run1.md"])
    assert out["committed"] is True and out["doc_paths_dropped"] == ["docs/delivery/run1.md"]


def _spawns(monkeypatch) -> list:
    seen: list = []
    real_init = subprocess.Popen.__init__

    def counting(self, *args, **kwargs):
        seen.append(args[0] if args else kwargs.get("args"))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    return seen


def _into_record_dir(repo: Path) -> str:
    """Re-home the fixture run's manifest and ledger under state/warp; the marker stays in scratch."""
    rel = f"{RECORD_DIR_ROOT}/{RUN}"
    manifest = StageManifest(
        run_dir=rel, rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(repo, f"{rel}/manifest.json", json.dumps(manifest.to_json()))
    _write(repo, f"{rel}/pm-intent.md", "intent\n")
    for name in ("requirements.jsonl", "requirements.md"):
        src = repo / RUN_DIR_ROOT / RUN / name
        if src.is_file():
            _write(repo, f"{rel}/{name}", src.read_text(encoding="utf-8"))
            src.unlink()
    _write(repo, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{rel}/manifest.json\n")
    return rel


def test_ask_run_spawn_count_is_pinned(repo, monkeypatch):
    seen = _spawns(monkeypatch)
    out = _call(repo)
    assert out["committed"] is True, out
    assert len(seen) == 2


def test_a_record_dir_run_commits_its_record_files_with_the_run(repo, monkeypatch):
    rel = _into_record_dir(repo)
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/brief.md", "working file\n")
    seen = _spawns(monkeypatch)
    out = _call(repo)
    assert out["committed"] is True, out
    assert len(seen) <= 2
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {
        "a.py", f"{rel}/manifest.json", f"{rel}/pm-intent.md",
        f"{rel}/requirements.jsonl", f"{rel}/requirements.md",
    }


def test_a_scratch_run_dir_adds_nothing(repo):
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/pm-intent.md", "intent\n")
    out = _call(repo)
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py"}
