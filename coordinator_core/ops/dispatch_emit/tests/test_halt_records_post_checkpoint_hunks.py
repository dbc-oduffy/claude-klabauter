"""A commit halt records the run's own post-checkpoint hunks in kept.json, withholds and names them."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import (
    halt_refire,
    halted_record_commit,
    kept_work,
    review_edits,
    sibling_claims,
)
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _git,
    _marker_script,
    _status,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FILES = ["pkg/land_run.py", "pkg/test_land.py", "pkg/test_terminal.py"]
KEPT_REL = f"{RECORD_DIR_ROOT}/{RUN}/{kept_work.KEPT_FILE}"
HALTED_REL = f"{RECORD_DIR_ROOT}/{RUN}/halted.json"
RUN_DIRT = halted_record_commit.RUN_DIRT_UNCOMMITTED


def _halt() -> dict:
    return {
        "halted": "commit",
        "reason": "peer-hunks-co-declared",
        "next_action": halt_refire.refire_next_action(
            call='Workflow({script: "x.mjs"})', run_id=RUN, sizing="state/sizings/s.yaml", halted="commit"
        ),
    }


def _call(root, halted=None) -> dict:
    from coordinator_core.ops.dispatch_emit import terminal_commit

    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted or _halt()}
    return terminal_commit._handler(params, repo_root=root / ".git")


def _manifest(root, *, rows=None, review=None, baseline=None) -> None:
    doc: dict = {
        "rows": rows if rows is not None else [{"id": "C2", "writes": FILES[:2]}, {"id": "C3", "writes": FILES[2:]}],
        "review_declared_paths": review if review is not None else [],
    }
    if baseline is not None:
        doc["write_baseline"] = baseline
    (root / RECORD_DIR_ROOT / RUN / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")


def _dirty(root, paths=FILES) -> None:
    for p in paths:
        target = root / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{p} reviewed\n", encoding="utf-8")


def _blobs(root, paths=FILES) -> dict:
    return {p: kept_work.fingerprint(root, p) for p in paths}


def _checkpointed_shape(root, monkeypatch, *, review_source="answers") -> None:
    """Rows C2/C3 are checkpointed (absent from the request, which finishes only C4); a review
    agent then wrote their files. The sibling-claim lookup claims them, as in f0f6de."""
    _write_record(root, RUN)
    _manifest(root)
    _dirty(root)
    (root / "c4.py").write_text("c4 = 1\n", encoding="utf-8")
    _marker_script(root, [("C4", ["c4.py"])], {"C4": "DONE: C4\n"})
    monkeypatch.setattr(
        review_edits, "review_writes",
        (lambda *a, **k: {p: "agent-1" for p in FILES}) if review_source == "answers" else (lambda *a, **k: None),
    )
    monkeypatch.setattr(
        sibling_claims, "sibling_run_claims",
        lambda *a, **k: {p: "ask-other" for p in FILES} if review_source == "answers" else {},
    )


def _assert_recorded(root, out, paths=FILES) -> None:
    assert out["committed"] is True, out
    kept = json.loads(_git(["show", f"HEAD:{KEPT_REL}"], root))
    assert kept["post_checkpoint"] == _blobs(root, paths)
    for p in paths:
        assert out["run_artifacts"]["withheld"][p] == RUN_DIRT
    message = _git(["log", "-1", "--format=%B"], root)
    assert [l for l in message.splitlines() if l.startswith("Kept-Post-Checkpoint: ")] == [
        f"Kept-Post-Checkpoint: {p}" for p in sorted(paths)
    ]
    halted = json.loads(_git(["show", f"HEAD:{HALTED_REL}"], root))
    assert halted["next_action"]["resume"]["stage"]["post_checkpoint"] == sorted(paths)
    assert out["kept"]["post_checkpoint"] == sorted(paths)
    landed = _git(["show", "--name-only", "--format=", "HEAD"], root).split()
    assert not set(paths) & set(landed)
    assert set(paths) <= set(_status(root))


def test_review_hunks_on_checkpointed_rows_are_recorded_withheld_and_named(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch)

    out = _call(repo)

    _assert_recorded(repo, out)
    assert out["review_edits_unlanded"] == {}


def test_recorded_blobs_resolve_in_the_object_store(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch)

    out = _call(repo)

    _assert_recorded(repo, out)
    for p, blob in _blobs(repo).items():
        assert _git(["cat-file", "-p", blob], repo) == f"{p} reviewed\n"


def test_recorded_blobs_resolve_when_dot_git_is_a_pointer_file(repo, tmp_path, monkeypatch):
    _git(["init", "-q", "--separate-git-dir", str(tmp_path / "gd")], repo)
    assert (repo / ".git").is_file()
    _checkpointed_shape(repo, monkeypatch)

    out = _call(repo)

    _assert_recorded(repo, out)
    for p, blob in _blobs(repo).items():
        assert _git(["cat-file", "-p", blob], repo) == f"{p} reviewed\n"


def test_record_does_not_depend_on_the_review_source_answering(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")

    out = _call(repo)

    _assert_recorded(repo, out)


def test_review_declared_paths_alone_are_recorded(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    _manifest(repo, rows=[], review=FILES)

    out = _call(repo)

    _assert_recorded(repo, out)


def test_file_only_a_sibling_declares_is_not_recorded(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    _manifest(repo, rows=[{"id": "C2", "writes": FILES[:1]}])
    _dirty(repo, ["sibling_only.py"])

    out = _call(repo)

    _assert_recorded(repo, out, FILES[:1])
    kept = json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    assert "sibling_only.py" not in kept["post_checkpoint"]
    assert "sibling_only.py" not in out["run_artifacts"]["withheld"]


def test_declared_file_equal_to_its_write_baseline_is_not_recorded(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    stale = FILES[0]
    _manifest(repo, baseline={stale: kept_work.fingerprint(repo, stale)})

    out = _call(repo)

    _assert_recorded(repo, out, FILES[1:])
    assert stale not in out["run_artifacts"]["withheld"]


def test_dirty_file_of_an_unfinished_row_in_the_request_is_not_recorded(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    (repo / "half.py").write_text("h = 1\n", encoding="utf-8")
    _manifest(repo, rows=[{"id": "C4", "writes": ["c4.py"]}, {"id": "C5", "writes": ["half.py"]}])
    _marker_script(
        repo, [("C4", ["c4.py"]), ("C5", ["half.py"])], {"C4": "DONE: C4\n", "C5": "PARTIAL: C5\n"}
    )

    out = _call(repo)

    assert out["committed"] is True, out
    assert "post_checkpoint" not in json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    assert "post_checkpoint" not in out["kept"]
    assert "Kept-Post-Checkpoint" not in _git(["log", "-1", "--format=%B"], repo)


PLAN = "docs/plans/p.md"
COMPOSED = {"C9": "pkg/nine.py", "C11": "pkg/eleven.py"}


def _plan(root, coded) -> None:
    rows = "".join(
        f"- id: {rid}\n  title: t\n  change_kind: script-edit\n  surface: {path}\n  writes: [{path}]\n"
        + (f"  disposition: coded\n  disposition_ref: {'a' * 40}\n" if rid in coded else "  disposition: open\n")
        + "  body: |\n    Do it.\n"
        for rid, path in COMPOSED.items()
    )
    target = root / PLAN
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8"
    )
    _git(["add", PLAN], root)
    _git(["commit", "-q", "-m", "plan", "--", PLAN], root)


def _d0400d_shape(root, monkeypatch, *, coded) -> None:
    """C9/C11 reported PARTIAL in the commit request; a review edit then dirtied one file of each."""
    _write_record(root, RUN)
    _plan(root, coded)
    paths = list(COMPOSED.values())
    _manifest(root, rows=[{"id": "C4", "writes": ["c4.py"]}, *({"id": r, "writes": [p]} for r, p in COMPOSED.items())])
    _dirty(root, paths)
    (root / "c4.py").write_text("c4 = 1\n", encoding="utf-8")
    _marker_script(
        root, [("C4", ["c4.py"]), *((r, [p]) for r, p in COMPOSED.items())],
        {"C4": "DONE: C4\n", **{r: f"PARTIAL: {r}\n" for r in COMPOSED}},
    )
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: None)
    monkeypatch.setattr(sibling_claims, "sibling_run_claims", lambda *a, **k: {})


def test_post_checkpoint_hunks_of_coded_rows_still_in_the_request_are_recorded(repo, monkeypatch):
    _d0400d_shape(repo, monkeypatch, coded=set(COMPOSED))

    out = _call(repo, {**_halt(), "plan": PLAN})

    _assert_recorded(repo, out, list(COMPOSED.values()))
    for p, blob in _blobs(repo, list(COMPOSED.values())).items():
        assert _git(["cat-file", "-p", blob], repo) == f"{p} reviewed\n"


@pytest.mark.parametrize("plan_field", [True, False])
def test_uncoded_rows_in_the_request_are_withheld_not_recorded(repo, monkeypatch, plan_field):
    _d0400d_shape(repo, monkeypatch, coded=set())

    out = _call(repo, {**_halt(), "plan": PLAN} if plan_field else None)

    assert out["committed"] is True, out
    assert "post_checkpoint" not in json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    for p in COMPOSED.values():
        assert out["run_artifacts"]["withheld"][p] == RUN_DIRT


def test_unreadable_plan_is_surfaced_and_credits_no_row(repo, monkeypatch):
    _d0400d_shape(repo, monkeypatch, coded=set(COMPOSED))
    (repo / PLAN).write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    out = _call(repo, {**_halt(), "plan": PLAN})

    assert out["committed"] is True, out
    assert PLAN in out["coded_rows_unreadable"]
    assert "post_checkpoint" not in json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    for p in COMPOSED.values():
        assert out["run_artifacts"]["withheld"][p] == RUN_DIRT


def test_readable_plan_reports_no_coded_rows_error(repo, monkeypatch):
    _d0400d_shape(repo, monkeypatch, coded=set(COMPOSED))

    out = _call(repo, {**_halt(), "plan": PLAN})

    assert "coded_rows_unreadable" not in out


def test_coded_rows_lets_an_unexpected_error_propagate(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import delivery_credit

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(delivery_credit, "coded_row_refs", boom)
    with pytest.raises(RuntimeError):
        halted_record_commit._coded_rows(tmp_path, {"plan": PLAN})


def test_blobs_that_could_not_be_written_are_not_recorded_and_are_reported(repo, monkeypatch):
    from coordinator_core.git import git_objects

    _checkpointed_shape(repo, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(git_objects, "write_object", boom)

    out = _call(repo)

    assert out["committed"] is True, out
    assert "post_checkpoint" not in json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    assert out["post_checkpoint_unpersisted"] == sorted(FILES)
    assert "post_checkpoint" not in out["kept"]
    for p in FILES:
        assert out["run_artifacts"]["withheld"][p] == RUN_DIRT


def test_coded_and_uncoded_request_rows_start_the_same_processes(repo, tmp_path, monkeypatch):
    import shutil

    _d0400d_shape(repo, monkeypatch, coded=set(COMPOSED))
    control = tmp_path / "control"
    shutil.copytree(repo, control)
    _plan(control, set())
    real = subprocess.Popen
    calls: list = []

    class Spy(real):
        def __init__(self, *a, **k):
            calls.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    _call(control, {**_halt(), "plan": PLAN})
    baseline = len(calls)
    del calls[:]

    out = _call(repo, {**_halt(), "plan": PLAN})

    assert out["kept"]["post_checkpoint"] == sorted(COMPOSED.values())
    assert baseline > 0
    assert len(calls) == baseline


def test_second_halt_of_a_same_id_refire_carries_the_prior_record_forward(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    first = _call(repo)
    _assert_recorded(repo, first)
    _manifest(repo, rows=[{"id": "C4", "writes": ["c4.py"]}])
    (repo / "c4.py").write_text("c4 = 2\n", encoding="utf-8")

    out = _call(repo)

    assert out["committed"] is True, out
    kept = json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    assert kept["post_checkpoint"] == _blobs(repo)
    assert all(out["run_artifacts"]["withheld"][p] == RUN_DIRT for p in FILES)


def test_prior_record_drops_a_path_that_is_no_longer_dirty(repo, monkeypatch):
    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    _call(repo)
    _git(["add", FILES[0]], repo)
    _git(["commit", "-q", "-m", "hand-land", "--", FILES[0]], repo)
    _manifest(repo, rows=[])
    (repo / "c4.py").write_text("c4 = 3\n", encoding="utf-8")

    out = _call(repo)

    assert out["committed"] is True, out
    kept = json.loads(_git(["show", f"HEAD:{KEPT_REL}"], repo))
    assert sorted(kept["post_checkpoint"]) == sorted(FILES[1:])


def test_halt_starts_no_extra_process_for_the_recorded_hunks(repo, tmp_path, monkeypatch):
    import shutil

    _checkpointed_shape(repo, monkeypatch, review_source="unanswerable")
    control = tmp_path / "control"
    shutil.copytree(repo, control)
    real = subprocess.Popen
    calls: list = []

    class Spy(real):
        def __init__(self, *a, **k):
            calls.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    with monkeypatch.context() as unmodified:
        unmodified.setattr(halted_record_commit, "_post_checkpoint_candidates", lambda *a, **k: [])
        _call(control)
    baseline = len(calls)
    del calls[:]

    out = _call(repo)

    assert out["kept"]["post_checkpoint"] == sorted(FILES)
    assert baseline > 0
    assert len(calls) == baseline
