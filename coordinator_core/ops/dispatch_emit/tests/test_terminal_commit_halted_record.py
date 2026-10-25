"""dispatch.terminal_commit's halted-run mode: one commit of the record dir plus the run's candidate set."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"
OTHER = "ask-20261015T000001-bbbbbb"
REASON = "sizing refused: estimate L needs a PM touchpoint"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("seed\n", encoding="utf-8")
    (root / "product.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def _write_record(root: Path, run_id: str, names=("ask.md", "pm-intent.md", "run.json")) -> Path:
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    for name in names:
        (rec / name).write_text(f"{name}\n", encoding="utf-8")
    return rec


def _dirty_elsewhere(root: Path) -> set:
    # Edits change the file SIZE too: a same-size rewrite within one filesystem timestamp tick
    # leaves git's stat cache looking clean (racy-git), so `git status` omits the file under load.
    (root / "docs" / "guide.md").write_text("edited elsewhere\n", encoding="utf-8")
    (root / "product.py").write_text("x = 2000\n", encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "state" / "sizings" / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    _write_record(root, OTHER, ("ask.md",))
    return {"run.mjs", "docs/guide.md", "product.py", "state/sizings/x.yaml", f"{RECORD_DIR_ROOT}/{OTHER}/ask.md"}


LEDGER = {"halted": "ledger-check", "reason": REASON}
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}


def _call(root: Path, halted: dict | None = None, **extra) -> dict:
    params = {
        "script_path": "run.mjs",
        "run_id": RUN,
        HALTED_RECORD_PARAM: halted or {"halted": "refusal", "reason": REASON},
        **extra,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _landed(root: Path, out: dict, *show_args: str) -> list:
    """`git show` lines over every commit the reply names: `sha`, plus `process_sha` when mixed."""
    shas = [out["sha"]] + ([out["process_sha"]] if out.get("process_sha") else [])
    return [line for sha in shas for line in _git(["show", *show_args, "--format=", sha], root).splitlines()]


def _status(root: Path) -> dict:
    out = _git(["status", "--porcelain", "-uall"], root)
    return {line[3:]: line[:2] for line in out.splitlines()}


def test_stop_rule_typed_halt_lands_and_bare_string_halt_is_refused(repo):
    _write_record(repo, RUN)
    typed = {"halted": "stop_rule", "reason": "C1", "sizing": "state/sizings/x.yaml", "run_id": RUN}

    bare = _call(repo, **{HALTED_RECORD_PARAM: "C1"})
    assert bare.get("refused") == "missing-run-outcome"

    out = _call(repo, **{HALTED_RECORD_PARAM: typed})
    assert out.get("refused") != "missing-run-outcome"
    assert out["committed"] is True
    assert "Halted-By: stop_rule" in _git(["log", "-1", "--format=%B"], repo)


def test_non_halted_product_commit_without_run_outcome_still_refused(repo):
    out = terminal_commit._handler({"script_path": "run.mjs", "run_id": RUN}, repo_root=repo / ".git")
    assert out.get("refused") == "missing-run-outcome"


def test_sizing_refusal_lands_one_commit_of_record_and_leaves_unrelated_dirt(repo):
    _write_record(repo, RUN)
    others = _dirty_elsewhere(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _call(repo)

    assert out["committed"] is True
    assert _git(["rev-parse", "HEAD~1"], repo).strip() == head
    names = _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
    # run-stage.json rides every terminal commit: the stage stamp is written before staging.
    assert sorted(names) == sorted(
        f"{RECORD_DIR_ROOT}/{RUN}/{n}" for n in ("ask.md", "pm-intent.md", "run.json", "halted.json", "run-stage.json")
    )
    message = _git(["log", "-1", "--format=%B"], repo)
    assert "Halted-By: refusal" in message
    assert REASON in message
    status = _status(repo)
    assert set(status) == others
    assert _git(["diff", "--cached", "--name-only"], repo) == ""


def test_halt_after_stage_lands_manifest_and_requirements(repo):
    _write_record(repo, RUN, ("ask.md", "pm-intent.md", "run.json", "manifest.json", "requirements.md"))

    out = _call(repo, **{HALTED_RECORD_PARAM: {"halted": "ledger-check", "detail": "row R1 open"}})

    assert out["committed"] is True
    names = _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
    assert len(names) == 7  # five record files + halted.json + run-stage.json
    assert f"{RECORD_DIR_ROOT}/{RUN}/run-stage.json" in names
    assert all(n.startswith(f"{RECORD_DIR_ROOT}/{RUN}/") for n in names)
    assert "Halted-By: ledger-check" in _git(["log", "-1", "--format=%B"], repo)


def test_tracked_record_file_deleted_from_disk_lands(repo):
    rec = _write_record(repo, RUN)
    _git(["add", RECORD_DIR_ROOT], repo)
    _git(["commit", "-q", "-m", "record"], repo)
    (rec / "run.json").unlink()

    out = _call(repo)

    assert out["committed"] is True, out
    assert sorted(_git(["show", "--name-status", "--format=", "HEAD"], repo).splitlines()) == [
        f"A\t{RECORD_DIR_ROOT}/{RUN}/halted.json",
        f"A\t{RECORD_DIR_ROOT}/{RUN}/run-stage.json",
        f"D\t{RECORD_DIR_ROOT}/{RUN}/run.json",
    ]


def test_nothing_changed_returns_nothing_to_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _call(repo)

    assert out["nothing_to_commit"] is True
    assert out["committed"] is False
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_unsafe_run_id_and_malformed_halted_by_refuse(repo):
    _write_record(repo, RUN)
    out = _call(repo, run_id="../x")
    assert out["refused"] == "run-id-unsafe"
    assert out["committed"] is False

    malformed = _call(repo, **{HALTED_RECORD_PARAM: {"reason": REASON}})
    assert malformed.get("refused") == "missing-run-outcome"
    assert malformed["committed"] is False
    assert _status(repo)[f"{RECORD_DIR_ROOT}/{RUN}/ask.md"] == "??"


def test_token_stays_live_after_landed_record_commit_and_wrong_token_refuses(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")

    refused = _call(repo, run_token="0" * 32)
    assert refused["refused"] == "run-token"
    assert run_token.is_live(repo, RUN)

    out = _call(repo, run_token=token)

    assert out["committed"] is True
    assert run_token.is_live(repo, RUN)
    assert run_token.admits(repo, RUN, token, "run.mjs")


def _assert_success_commit_redeems(root: Path, token: str) -> None:
    assert run_token.redeem(root, RUN, token, "run.mjs")


def test_token_redeems_after_landed_halt_record(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")

    assert _call(repo, run_token=token)["committed"] is True

    _assert_success_commit_redeems(repo, token)


def test_token_redeems_after_nothing_to_commit_halt(repo):
    token = run_token.mint(repo, RUN, "run.mjs")

    assert _call(repo, run_token=token)["nothing_to_commit"] is True

    _assert_success_commit_redeems(repo, token)


def test_token_redeems_after_refused_halt(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")

    assert _call(repo, run_token="0" * 32)["refused"] == "run-token"

    _assert_success_commit_redeems(repo, token)


def test_token_redeems_after_land_error_halt(repo, monkeypatch):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *a, **k: None)

    assert _call(repo, run_token=token)["committed"] is False

    _assert_success_commit_redeems(repo, token)


def test_token_redeems_after_halt_raising_mid_commit(repo, monkeypatch):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")

    def boom(*a, **k):
        raise RuntimeError("commit_v2 blew up")

    monkeypatch.setattr("coordinator_core.warm.entry_seam.reentrant_dispatch", boom)

    with pytest.raises(RuntimeError):
        _call(repo, run_token=token)

    _assert_success_commit_redeems(repo, token)


def test_token_unclaimed_while_halt_commit_in_flight(repo, monkeypatch):
    """A halt op killed mid-commit strands nothing: the token is never claimed."""
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")
    seen: dict = {}

    def killed(*a, **k):
        seen["live"] = run_token.is_live(repo, RUN)
        raise KeyboardInterrupt

    monkeypatch.setattr("coordinator_core.warm.entry_seam.reentrant_dispatch", killed)

    with pytest.raises(KeyboardInterrupt):
        _call(repo, run_token=token)

    assert seen["live"] is True
    _assert_success_commit_redeems(repo, token)


def _seed_siblings(root: Path) -> str:
    """Parent record naming RUN and a successor `after` it; RUN's status reads running."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

    parent = "ask-parent-20261015T000000-abcdef"
    runs = (
        c.SiblingRun(RUN, RUN, (), "state/sizings/a.yaml", c.STATUS_READY),
        c.SiblingRun(OTHER, OTHER, (RUN,), "state/sizings/b.yaml", c.STATUS_AFTER),
    )
    rec = root / c.parent_record_rel(parent)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(parent, runs).to_json()), encoding="utf-8")
    st = root / c.status_rel(RUN)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"status": c.STATUS_RUNNING, "parent_id": parent}), encoding="utf-8")
    return parent


def _status_text(root: Path) -> str:
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

    return (root / c.status_rel(RUN)).read_text(encoding="utf-8")


def _assert_successor_told_halted(root: Path, parent: str) -> None:
    from coordinator_core.hooks.submit_engine_ask import _slice_deny

    assert '"halted"' in _status_text(root)
    deny = _slice_deny(root, (parent, OTHER))
    assert deny and "halted" in deny and "has not landed" not in deny


def test_wrong_token_refusal_still_settles_status_halted(repo):
    _write_record(repo, RUN)
    parent = _seed_siblings(repo)
    token = run_token.mint(repo, RUN, "run.mjs")
    assert '"running"' in _status_text(repo)

    refused = _call(repo, run_token="0" * 32)

    assert refused["refused"] == "run-token"
    assert refused["siblings"]["blocked"] == [{"run_id": OTHER, "slice_sizing": "state/sizings/b.yaml"}]
    _assert_successor_told_halted(repo, parent)
    assert run_token.admits(repo, RUN, token, "run.mjs")


def test_land_error_still_settles_status_halted(repo, monkeypatch):
    _write_record(repo, RUN)
    parent = _seed_siblings(repo)
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *a, **k: None)

    out = _call(repo)

    assert out["committed"] is False and "failed" in out["error"]
    _assert_successor_told_halted(repo, parent)


def test_landed_halt_settles_status_halted_and_names_siblings(repo):
    _write_record(repo, RUN)
    parent = _seed_siblings(repo)

    out = _call(repo)

    assert out["committed"] is True
    assert out["siblings"]["parent_id"] == parent
    _assert_successor_told_halted(repo, parent)


def _marker_script(root: Path, chunks, report_texts=None) -> None:
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    reports = report_texts or {}
    built = []
    for cid, paths in chunks:
        report = f"reports/{cid}.md"
        if cid in reports:
            (root / "reports").mkdir(exist_ok=True)
            (root / report).write_text(reports[cid], encoding="utf-8")
        built.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=report))
    marker = render_marker(CommitRequest(chunks=tuple(built), session_id=None, repo_root=None, plan_path=None))
    (root / "run.mjs").write_text(f"{marker}\n", encoding="utf-8")


def test_halted_run_lands_done_chunk_not_unfinished_one(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "half.py").write_text("h = 1\n", encoding="utf-8")
    _marker_script(
        repo, [("C1", ["done.py"]), ("C2", ["half.py"])],
        {"C1": "DONE: C1\n", "C2": "PARTIAL: C2\n"},
    )

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["landed_chunks"] == ["C1"]
    names = _landed(repo, out, "--name-only")
    assert "done.py" in names and "half.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert _status(repo)["half.py"] == "??"


def test_incomplete_chunks_param_overrides_report_evidence(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, incomplete_chunks=["C1"])

    assert "landed_chunks" not in out
    assert "done.py" not in _landed(repo, out, "--name-only")


def _hold(monkeypatch, held: set) -> None:
    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        holds.extend((p, "peer-sid") for p in paths if p in held)
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)


def test_peer_held_path_withholds_its_whole_chunk_and_names_both(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "other.py").write_text("o = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py", "other.py"])], {"C1": "DONE: C1\n"})
    _hold(monkeypatch, {"other.py"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"other.py": "peer-held", "done.py": "chunk-write-set-partial"}
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names and "other.py" not in names


def test_done_tracked_deletion_is_declared_and_never_tracked_path_is_not_staged(repo):
    _write_record(repo, RUN)
    (repo / "product.py").unlink()
    _marker_script(repo, [("C1", ["product.py", "never-tracked.yaml"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert "D\tproduct.py" in _landed(repo, out, "--name-status")
    assert out.get("dropped", []) == []


def test_no_done_chunk_keeps_record_only_message(repo):
    _write_record(repo, RUN)
    plain = _call(repo)
    plain_message = _git(["log", "-1", "--format=%B"], repo)
    assert "Lands-Chunks" not in plain_message
    assert "landed_chunks" not in plain


def test_token_live_after_halt_landing_done_chunk(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    token = run_token.mint(repo, RUN, "run.mjs")

    out = _call(repo, LEDGER, run_token=token, **PASSING)

    assert out["landed_chunks"] == ["C1"]
    assert run_token.admits(repo, RUN, token, "run.mjs")


def test_refusal_halt_lands_record_only_and_withholds_chunk_product(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "product.py").write_text("x = 9\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py", "product.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo)

    assert out["committed"] is True, out
    assert "landed_chunks" not in out
    assert out["withheld"] == {"done.py": "halt-record-only", "product.py": "halt-record-only"}
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names and "product.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "Lands-Chunks" not in _git(["log", "-1", "--format=%B"], repo)


def test_ledger_halt_withholds_chunk_importing_untracked_py(repo):
    _write_record(repo, RUN)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "pkg"], repo)
    _git(["commit", "-q", "-m", "pkg"], repo)
    (repo / "pkg" / "a.py").write_text("from pkg import b\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("v = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["pkg/a.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["withheld"] == {"pkg/a.py": "import-closure-open"}
    assert list(out["closure_open"]) == ["C1"]
    assert "landed_chunks" not in out
    assert "pkg/a.py" not in _landed(repo, out, "--name-only")


def test_ledger_halt_withholds_tsx_chunk_importing_untracked_alias_module(repo):
    _write_record(repo, RUN)
    (repo / "tsconfig.json").write_text(
        '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}\n', encoding="utf-8"
    )
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "page.tsx").write_text("export const P = 1;\n", encoding="utf-8")
    _git(["add", "tsconfig.json", "src"], repo)
    _git(["commit", "-q", "-m", "ts"], repo)
    write_seen_by_git(repo / "src" / "page.tsx", 'import { X } from "@/components/x";\nexport const P = X;\n')
    write_seen_by_git(repo / "src" / "components" / "x.tsx", "export const X = 1;\n")
    _marker_script(repo, [("C1", ["src/page.tsx"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["withheld"] == {"src/page.tsx": "import-closure-open"}
    assert "ts_import_closure" in " ".join(out["closure_open"]["C1"])
    assert "src/page.tsx" not in _landed(repo, out, "--name-only")
    assert _status(repo)["src/components/x.tsx"] == "??"


def test_ledger_halt_lands_only_the_closed_chunk(repo):
    _write_record(repo, RUN)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "pkg"], repo)
    _git(["commit", "-q", "-m", "pkg"], repo)
    (repo / "pkg" / "a.py").write_text("from pkg import b\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("v = 1\n", encoding="utf-8")
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    _marker_script(
        repo, [("C1", ["pkg/a.py"]), ("C2", ["done.py"])], {"C1": "DONE: C1\n", "C2": "DONE: C2\n"}
    )

    out = _call(repo, LEDGER, **PASSING)

    assert out["landed_chunks"] == ["C2"]
    assert "Lands-Chunks: C2" in _git(["log", "-1", "--format=%B"], repo)
    names = _landed(repo, out, "--name-only")
    assert "done.py" in names and "pkg/a.py" not in names


def _name_sizing(root: Path, sizing_rel: str) -> None:
    (root / RECORD_DIR_ROOT / RUN / "run.json").write_text(f'{{"sizing_rel": "{sizing_rel}"}}\n', encoding="utf-8")


def test_run_json_sizing_lands_when_dirty_and_is_skipped_otherwise(repo):
    _write_record(repo, RUN)
    sizing = repo / "state" / "sizings" / "own.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("a: 1\n", encoding="utf-8")
    _name_sizing(repo, "state/sizings/own.yaml")

    out = _call(repo)

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert "state/sizings/own.yaml" in names
    assert "Lands-Chunks" not in _git(["log", "-1", "--format=%B"], repo)
    assert "landed_chunks" not in out

    record = {f"{RECORD_DIR_ROOT}/{RUN}/{n}" for n in ("ask.md", "run.json", "halted.json", "run-stage.json")}
    for named in ("state/sizings/own.yaml", "../outside.yaml", "/abs/outside.yaml", "state/sizings/absent.yaml"):
        (repo / RECORD_DIR_ROOT / RUN / "ask.md").write_text(f"again {named}\n", encoding="utf-8")
        _name_sizing(repo, named)
        again = _call(repo)
        assert again["committed"] is True, again
        assert set(_landed(repo, again, "--name-only")) <= record, named


def test_peer_held_run_json_sizing_is_withheld(repo, monkeypatch):
    _write_record(repo, RUN)
    sizing = repo / "state" / "sizings" / "own.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("a: 1\n", encoding="utf-8")
    _name_sizing(repo, "state/sizings/own.yaml")
    _hold(monkeypatch, {"state/sizings/own.yaml"})

    out = _call(repo)

    assert out["withheld"] == {"state/sizings/own.yaml": "peer-held"}
    assert "state/sizings/own.yaml" not in _landed(repo, out, "--name-only")


def _write_graph(root: Path, run_id: str) -> None:
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings" / "g.yaml").write_text(
        "baton: state/handoffs/g.md\nplan: docs/plans/g.md\n", encoding="utf-8"
    )
    (root / "state" / "handoffs" / "g.md").write_text("---\ncarried_items: []\n---\nbaton\n", encoding="utf-8")
    (root / "docs" / "plans" / "g.md").write_text("plan\n", encoding="utf-8")
    (root / "docs" / "plans" / "g.evidence.yaml").write_text("e: 1\n", encoding="utf-8")
    (root / RECORD_DIR_ROOT / run_id / "run.json").write_text(
        '{"sizing_rel": "state/sizings/g.yaml"}\n', encoding="utf-8"
    )


def test_halt_lands_plan_baton_sizing_and_evidence_sidecar(repo):
    _write_record(repo, RUN)
    others = _dirty_elsewhere(repo)
    _write_graph(repo, RUN)

    out = _call(repo)

    assert out["committed"] is True, out
    names = set(_landed(repo, out, "--name-only"))
    assert {
        "state/sizings/g.yaml", "state/handoffs/g.md", "docs/plans/g.md", "docs/plans/g.evidence.yaml",
    } <= names
    assert "Lands-Artifacts:" in _git(["log", "-1", "--format=%B"], repo)
    assert "run_artifact_residue" not in out
    assert set(_status(repo)) == others


def test_halt_lands_superseded_predecessor_record(repo):
    _write_record(repo, RUN)
    _write_graph(repo, RUN)
    old = "ask-20261015T000000-000000"
    _write_record(repo, old, ("ask.md", "run.json"))
    (repo / RECORD_DIR_ROOT / old / "run.json").write_text(
        '{"sizing_rel": "state/sizings/g.yaml"}\n', encoding="utf-8"
    )

    out = _call(repo)

    assert out["committed"] is True, out
    names = set(_landed(repo, out, "--name-only"))
    assert f"{RECORD_DIR_ROOT}/{old}/ask.md" in names
    assert not [p for p in _status(repo) if p.startswith(f"{RECORD_DIR_ROOT}/")]


def test_halt_withholds_unclaimed_docs_stage_path(repo):
    _write_record(repo, RUN)
    (repo / "docs" / "guide.md").write_text("edited\n", encoding="utf-8")

    out = _call(repo, doc_paths=["docs/guide.md"])

    assert out["withheld"] == {"docs/guide.md": "not-claimed"}
    assert "docs/guide.md" not in _landed(repo, out, "--name-only")


def test_halt_lands_plan_handoff_and_delivery_note_the_halt_names(repo):
    _write_record(repo, RUN)
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "delivery").mkdir(parents=True, exist_ok=True)
    (repo / RECORD_DIR_ROOT / RUN / "run.json").write_text(
        '{"sizing_rel": "state/sizings/p.yaml"}\n', encoding="utf-8"
    )
    (repo / "state" / "sizings" / "p.yaml").write_text("a: 1\n", encoding="utf-8")
    (repo / "state" / "sizings" / "p-slice-2.yaml").write_text("a: 2\n", encoding="utf-8")
    (repo / "docs" / "plans" / "2026-10-16-p.md").write_text("plan\n", encoding="utf-8")
    (repo / "docs" / "plans" / "2026-10-16-p.evidence.yaml").write_text("e: 1\n", encoding="utf-8")
    (repo / "state" / "handoffs" / "2026-10-16-p.md").write_text(
        "---\ncarried_items: []\n---\nbaton\n", encoding="utf-8"
    )
    (repo / "docs" / "delivery" / f"{RUN}.md").write_text("d\n", encoding="utf-8")

    out = _call(
        repo,
        **{HALTED_RECORD_PARAM: {"halted": "refusal", "reason": REASON, "plan": "docs/plans/2026-10-16-p.md"}},
    )

    assert out["committed"] is True, out
    names = set(_landed(repo, out, "--name-only"))
    assert {
        "docs/plans/2026-10-16-p.md", "docs/plans/2026-10-16-p.evidence.yaml",
        "state/handoffs/2026-10-16-p.md", f"docs/delivery/{RUN}.md", "state/sizings/p-slice-2.yaml",
    } <= names
    assert not [p for p in _status(repo) if p.startswith(("docs/plans", "state/handoffs", "docs/delivery"))]


def test_halt_names_dirty_review_written_file_and_does_not_land_it(repo, monkeypatch):
    """The halt-record commit lands this run's own review edit: leaving it dirty stranded reviewed
    work whenever a refire's commit leg closed on a credited halt (the pre-halt-credit incident)."""
    from coordinator_core.ops.dispatch_emit import review_edits

    _write_record(repo, RUN)
    (repo / "elsewhere").mkdir()
    (repo / "elsewhere" / "rev.py").write_text("edited\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {"elsewhere/rev.py": "agent-1"})

    out = _call(repo)

    assert out["committed"] is True, out
    assert out["review_edits_unlanded"] == {}
    assert "elsewhere/rev.py" in _landed(repo, out, "--name-only")
    assert "Review-Edit-Unlanded" not in _git(["log", "-1", "--format=%B"], repo)
    assert "elsewhere/rev.py" not in _status(repo)


def test_halt_with_no_review_writes_carries_empty_unlanded(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edits

    _write_record(repo, RUN)
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {})

    out = _call(repo)

    assert out["review_edits_unlanded"] == {}
    assert "Review-Edit-Unlanded" not in _git(["log", "-1", "--format=%B"], repo)


def test_halt_with_unanswerable_review_source_reports_null(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edits

    _write_record(repo, RUN)
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: None)

    out = _call(repo)

    assert out["committed"] is True
    assert out["review_edits"] is None
