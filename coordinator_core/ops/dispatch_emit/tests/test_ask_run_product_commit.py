"""dispatch.terminal_commit on a warp ask run: a product commit carrying the run's delivery note,
a stamp commit for a stageable plan, and the requirements ledger's verdict gating the commit and
written back onto the register."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.terminal_commit_full_path import INLINE_REVIEW, PLAN_ID
from coordinator_core.completion_receipts import store
from coordinator_core.ops import requirement_register as rr
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
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship a.py\n",
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
    _request(root)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _request(repo: Path, docs: tuple = ()) -> None:
    chunks = (ChunkCommit(id="C1", title="t", paths=("a.py",)),)
    if docs:
        chunks += (ChunkCommit(id="docs", title="run docs", paths=docs),)
    request = CommitRequest(chunks=chunks, deliverable_id=DLV, plan_path=PLAN)
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")


def _call(repo: Path, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]}, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


# The budget driver's minting review, without its lesson: this fixture points no lessons outbox.
_MINTING_REVIEW = {**INLINE_REVIEW, "criterion": {**INLINE_REVIEW["criterion"], "lesson_candidates": []}}


def _closable_plan(repo: Path, plan_rel: str) -> None:
    """Rewrite the in-run plan so the review stamp mints and the implemented flip is legal."""
    _write(
        repo, plan_rel,
        f"---\ntitle: p\nstatus: executing\nplan_id: {PLAN_ID}\ndeliverable_id: {DLV}\n"
        f"sizing_object: {SIZING}\nprime_exit_criterion:\n  statement: done\n  falsifier:\n"
        f"    how: pytest {NODE} -q\n    expected_when_true: passes\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: a.py\n  disposition: open\n  deferred: false\n```\n",
    )


def _count_commit_v2(monkeypatch) -> list:
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def counting(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(params)
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", counting)
    return seen


def test_ask_run_stamps_its_plan_in_a_second_commit_and_its_note_rides_the_first(repo, monkeypatch):
    import yaml

    _closable_plan(repo, PLAN)
    _write(repo, "docs/delivery/run1.md", "# delivered\n")
    _write(repo, "docs/reference/x.md", "# ref\n")
    calls = _count_commit_v2(monkeypatch)
    _request(repo, docs=("docs/delivery/run1.md",))
    out = _call(repo, inline_review=_MINTING_REVIEW)
    assert out["committed"] is True, out
    assert out["review_stamp"] == "minted" and out["plan_status"] == "implemented", out
    assert "coded_stamp_error" not in out, out
    assert len(calls) == 2
    assert set(calls[0]["paths"]) >= {"a.py", "docs/delivery/run1.md"}
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/delivery/run1.md"}
    stamp = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    entries = [f for f in stamp if f.startswith("archive/completed/")]
    assert PLAN in stamp and SIZING in stamp and len(entries) == 1
    assert "implemented" in _git(repo, "show", f"HEAD:{PLAN}")
    tracked = set(_git(repo, "ls-files").split())
    assert {PLAN, "docs/delivery/run1.md", *entries} <= tracked
    assert _git(repo, "status", "--porcelain", "--", PLAN, "docs/delivery/run1.md", SIZING, *entries) == ""
    message = _git(repo, "log", "-1", "--format=%B", out["sha"])
    assert f"Falsifier: {NODE}" in message and "Resolves-Sizing: 20260101-a S" in message
    receipt = out["receipts"][0]
    assert f"Completion-Receipt: {Path(receipt).stem}" in message
    assert store.introducing_commits(repo, [receipt]) == {receipt: out["sha"]}
    sizing = yaml.safe_load((repo / SIZING).read_text(encoding="utf-8"))
    assert sizing["status"] == "shipped" and sizing["resolution"] == {"sha": out["sha"], "run_id": RUN}
    assert "resolution_error" not in out
    assert f"disposition_ref: {out['sha']}" in (repo / PLAN).read_text(encoding="utf-8")


def test_ask_run_stamp_commit_carries_the_baton_its_sizing_names(repo, monkeypatch):
    baton = "state/handoffs/2026-01-01-p.md"
    _closable_plan(repo, PLAN)
    _write(repo, SIZING, (repo / SIZING).read_text(encoding="utf-8") + f"baton: {baton}\n")
    _write(repo, baton, "---\ntitle: p\nstatus: open\nkind: session-handoff\n---\n\n# handoff\n")
    out = _call(repo, inline_review=_MINTING_REVIEW)
    assert out["committed"] is True and "coded_stamp_error" not in out, out.get("coded_stamp_error")
    stamp = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert {PLAN, SIZING, baton} <= stamp
    tracked = set(_git(repo, "ls-files").split())
    assert {PLAN, SIZING, baton} <= tracked
    assert _git(repo, "status", "--porcelain", "--", PLAN, SIZING, baton) == ""


@pytest.mark.parametrize("cited", ["docs/reference/x.md", "state/handoffs/../../docs/reference/x.md"])
def test_a_baton_outside_the_handoffs_home_is_not_committed(repo, cited):
    _closable_plan(repo, PLAN)
    _write(repo, SIZING, (repo / SIZING).read_text(encoding="utf-8") + f"baton: {cited}\n")
    _write(repo, "docs/reference/x.md", "# not a handoff\n")
    out = _call(repo, inline_review=_MINTING_REVIEW)
    assert out["committed"] is True, out
    assert "docs/reference/x.md" not in _git(repo, "ls-files").split()


def test_ask_run_with_a_gitignored_plan_makes_one_commit_and_stamps_on_disk(repo, monkeypatch):
    ignored_plan = "scratch/plans/p.md"
    _closable_plan(repo, ignored_plan)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id=DLV, plan_path=ignored_plan
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    calls = _count_commit_v2(monkeypatch)
    out = _call(repo, inline_review=_MINTING_REVIEW)
    assert out["committed"] is True, out
    assert len(calls) == 1
    assert "coded_stamp_error" not in out, out
    assert f"disposition_ref: {out['sha']}" in (repo / ignored_plan).read_text(encoding="utf-8")


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


def test_a_post_commit_ledger_item_commits_and_reports_what_is_owed(repo):
    rl.revise(repo, RUN, verbatim="send a memo naming the landed sha")
    verdict = {"items": [
        {"id": "R1", "status": "post_commit", "wired": True, "post_commit_acts": ["commit", "sha_reply"]},
        {"id": "R2", "status": "post_commit", "wired": True, "post_commit_acts": ["sha_memo"]},
    ]}
    out = _call(repo, ledger_verdict=verdict)
    assert out["committed"] is True
    assert out["post_commit_owed"] == [{"id": "R2", "acts": ["sha_memo"], "sha": out["sha"]}]
    rows = {r["id"]: r for r in rr.read_register((repo / SIZING).read_text(encoding="utf-8")).rows}
    assert rows["R1"]["status"] == "met" and rows["R1"]["met_by"] == [f"{RUN}@{out['sha']}"]
    assert rows["R2"]["status"] == "partial"


def test_a_post_commit_item_naming_no_act_is_refused_unmet(repo):
    verdict = {"items": [{"id": "R1", "status": "post_commit", "post_commit_acts": []}]}
    out = _call(repo, ledger_verdict=verdict)
    assert out["committed"] is False and out["refused"] == "ledger-unmet"


def test_a_run_emitted_before_the_ledger_is_not_gated(repo):
    (repo / RUN_DIR_ROOT / RUN / rl.RUN_FILE).unlink()
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
    _request(repo, docs=tuple(every[:2]))
    out = _call(repo)
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/reference/x.md", "docs/decisions/DR-1.md"}


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


def test_a_peer_dirty_wiki_page_outside_the_declared_docs_is_not_committed(repo):
    _write(repo, "docs/delivery/run1.md", "# delivered\n")
    _write(repo, "docs/wiki/peer.md", "# peer wip\n")
    _request(repo, docs=("docs/delivery/run1.md",))
    out = _call(repo)
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/delivery/run1.md"}


def test_the_runs_own_delivery_note_lands_without_a_docs_chunk_and_other_docs_stay_out(repo):
    _write(repo, "docs/delivery/run1.md", "# delivered\n")
    _write(repo, "docs/reference/x.md", "# ref\n")
    out = _call(repo)
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py", "docs/delivery/run1.md"}


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
    for name in (rl.RUN_FILE, "requirements.md"):
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
        f"{rel}/{rl.RUN_FILE}", f"{rel}/requirements.md",
    }


def test_a_scratch_run_dir_adds_nothing(repo):
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/pm-intent.md", "intent\n")
    out = _call(repo)
    assert out["committed"] is True, out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert landed == {"a.py"}


def _composed_digest(ledger_js: str) -> dict:
    """The composed ask script's terminal digest, run under node with the stage bindings stubbed."""
    import shutil

    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    start = script.index("function _terminalCommitCli")
    stop = script.index("get terminal_commit_cli", script.index("  return { arm:"))
    stop = script.index("};", stop) + 2
    stubs = (
        "const _manifest={plan_id:'p'}, _incompleteChunks=[], _notStarted=[], _planHeld={}, _verifications=[], "
        "_reviewIntegration={fixes_applied:0,sidecar_path:'x'}, _reviewPrep={slices:[1]}, _reviewWave=[], "
        "_deliveryVerdict=null, _testResult=null, _halted=null, _viaRoadmap=false, _RUN_TOKEN=null, "
        "_sizingRel='s', _planRel=null, _gate={arm:'xs'}, _runId='r', _heldBy=[], _unansweredBriefs=[], "
        f"_stoppedBy=[], _blockedChunks=[], _docs=null, _commit=null; let _nextAction=null; const _ledger={ledger_js};\n"
    )
    body = script[start:stop].replace("  return { arm:", "  const _out = { arm:", 1)
    prog = f"(() => {{\n{stubs}{body}\nconsole.log(JSON.stringify(_out));\n}})();"
    run = subprocess.run([node, "-e", prog], capture_output=True, text=True, **no_console_creationflags())
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _printed_params(digest: dict) -> dict:
    cli = digest["terminal_commit_cli"]
    return json.loads(cli.split("\n")[1])


def test_the_printed_terminal_commit_params_carry_the_ledger_verdict_top_level(repo):
    digest = _composed_digest("{items:[{id:'R1',status:'met'}],verdict:'pass'}")
    params = _printed_params(digest)
    assert params["ledger_verdict"] == {"items": [{"id": "R1", "status": "met", "essential": True, "wired": False}]}
    assert "ledger_verdict" not in params["inline_review"]
    out = _call(repo, **{k: v for k, v in params.items() if k in ("ledger_verdict",)})
    assert out["committed"] is True, out


def test_a_digest_whose_ledger_check_never_returned_prints_no_verdict_and_the_gate_refuses(repo):
    params = _printed_params(_composed_digest("null"))
    assert "ledger_verdict" not in params
    out = _call(repo, ledger_verdict=None)
    assert out["refused"] == "ledger-unjudged"
