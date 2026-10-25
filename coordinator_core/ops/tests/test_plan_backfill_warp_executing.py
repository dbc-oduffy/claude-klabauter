"""plan.backfill_warp_executing: which pre-executing plans a warp run coded rows from, and the one commit that advances them."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import plan_backfill_warp_executing as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SPINE_CODED = (
    "## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: t\n  disposition: coded\n  disposition_ref: abc1234\n"
    "- id: C2\n  title: u\n  disposition: open\n```\n"
)
SPINE_OPEN = "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n```\n"
SPINE_CODED_NO_REF = "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: coded\n```\n"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _plan(status: str, plan_id: str, spine: str = SPINE_CODED) -> str:
    return f"---\ntitle: {plan_id}\nplan_id: {plan_id}\nstatus: {status}\n---\n\n# {plan_id}\n\n{spine}"


def _manifest(root, run: str, plan_id, rows=None) -> None:
    d = root / "state" / "warp" / run
    d.mkdir(parents=True, exist_ok=True)
    body = {"plan_id": plan_id, "rows": rows if rows is not None else [], "head_sha": "0" * 40}
    (d / "manifest.json").write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")


def _add_plan(root, name: str, text: str, newline=None) -> str:
    rel = f"docs/plans/{name}.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return rel


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "00000000-0000-4000-8000-000000000000")
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "seed").write_text("1", encoding="utf-8")
    return tmp_path


def _commit_all(root) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")


def _call(repo_dir, **params):
    return op._handler(params, repo_root=repo_dir / ".git")


def _snapshot(root) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _populate(root) -> dict:
    rels = {
        "reviewed": _add_plan(root, "a-reviewed", _plan("reviewed", "pln-a")),
        "draft": _add_plan(root, "b-draft", _plan("draft", "pln-b")),
        "approved": _add_plan(root, "c-approved", _plan("approved", "pln-c")),
        "no_coded": _add_plan(root, "d-open", _plan("reviewed", "pln-d", SPINE_OPEN)),
        "no_ref": _add_plan(root, "e-noref", _plan("reviewed", "pln-e", SPINE_CODED_NO_REF)),
        "unnamed": _add_plan(root, "f-unnamed", _plan("reviewed", "pln-f")),
        "executing": _add_plan(root, "g-executing", _plan("executing", "pln-g")),
        "landed": _add_plan(root, "h-landed", _plan("landed", "pln-h")),
        "implemented": _add_plan(root, "i-implemented", _plan("implemented", "pln-i")),
    }
    for run, pid in [("r1", "pln-a"), ("r2", "pln-b"), ("r3", "pln-c"), ("r4", "pln-d"), ("r5", "pln-e"),
                     ("r7", "pln-g"), ("r8", "pln-h"), ("r9", "pln-i")]:
        _manifest(root, run, pid)
    _commit_all(root)
    return rels


def test_dry_run_lists_exactly_the_pre_executing_plans_with_a_coded_row_and_writes_nothing(repo):
    rels = _populate(repo)
    before = _snapshot(repo)
    head = _git(repo, "rev-parse", "HEAD")

    out = _call(repo)

    assert [c["plan"] for c in out["candidates"]] == [rels["reviewed"], rels["draft"], rels["approved"]]
    first = out["candidates"][0]
    assert first["plan_id"] == "pln-a" and first["status"] == "reviewed"
    assert first["coded_rows"] == ["C1"] and first["runs"] == ["r1"]
    assert out["applied"] == [] and out["commit_sha"] is None
    assert _snapshot(repo) == before and _git(repo, "rev-parse", "HEAD") == head


def test_a_manifest_with_no_rows_still_links_its_plan(repo):
    _add_plan(repo, "a", _plan("reviewed", "pln-a"))
    _manifest(repo, "resume", "pln-a", rows=[])
    _commit_all(repo)
    assert [c["plan_id"] for c in _call(repo)["candidates"]] == ["pln-a"]


def test_a_manifest_naming_a_null_plan_links_nothing(repo):
    _add_plan(repo, "a", _plan("reviewed", "pln-a"))
    _manifest(repo, "minted", None)
    _commit_all(repo)
    assert _call(repo)["candidates"] == []


def test_apply_flips_every_candidate_in_one_commit_touching_only_them(repo):
    rels = _populate(repo)
    head = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, apply=True)

    flipped = [rels["reviewed"], rels["draft"], rels["approved"]]
    assert out["applied"] == flipped and out["skipped"] == [] and out["commit_error"] is None
    assert _git(repo, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert _git(repo, "rev-parse", "HEAD") == out["commit_sha"]
    assert sorted(_git(repo, "diff", "--name-only", head, "HEAD").splitlines()) == sorted(flipped)
    for rel in flipped:
        assert "status: executing\n" in (repo / rel).read_text(encoding="utf-8")
    for key in ("no_coded", "no_ref", "unnamed"):
        assert "status: reviewed\n" in (repo / rels[key]).read_text(encoding="utf-8")
    assert _call(repo)["candidates"] == []
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_keeps_crlf(repo):
    rel = _add_plan(repo, "crlf", _plan("reviewed", "pln-a").replace("\n", "\r\n"), newline="")
    _manifest(repo, "r1", "pln-a")
    _commit_all(repo)
    assert b"\r\n" in (repo / rel).read_bytes()

    out = _call(repo, apply=True)

    assert out["applied"] == [rel]
    data = (repo / rel).read_bytes()
    assert b"status: executing\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_plan_ids_narrows_apply_and_reports_unmatched(repo):
    rels = _populate(repo)

    out = _call(repo, apply=True, plan_ids=["pln-b", "pln-nope"])

    assert out["applied"] == [rels["draft"]]
    assert out["unmatched_plan_ids"] == ["pln-nope"]
    assert len(out["candidates"]) == 3
    assert "status: reviewed\n" in (repo / rels["reviewed"]).read_text(encoding="utf-8")


def test_a_plan_dirty_against_head_is_skipped_and_listed(repo):
    rels = _populate(repo)
    dirty = repo / rels["reviewed"]
    dirty.write_text(dirty.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
    on_disk = dirty.read_bytes()

    out = _call(repo, apply=True)

    assert [s["plan"] for s in out["skipped"]] == [rels["reviewed"]]
    assert out["skipped"][0]["reason"].startswith("dirty")
    assert out["applied"] == [rels["draft"], rels["approved"]]
    assert dirty.read_bytes() == on_disk


def test_an_untracked_plan_is_skipped_and_listed(repo):
    rel = _add_plan(repo, "fresh", _plan("reviewed", "pln-a"))
    _manifest(repo, "r1", "pln-a")
    (repo / "docs" / "other").mkdir(parents=True)
    (repo / "docs" / "other" / "x").write_text("x", encoding="utf-8")
    _git(repo, "add", "state", "docs/other")
    _git(repo, "commit", "-q", "-m", "fixture")

    out = _call(repo, apply=True)

    assert out["applied"] == [] and out["skipped"][0]["plan"] == rel
    assert out["skipped"][0]["reason"].startswith("untracked")
    assert out["commit_sha"] is None


@pytest.mark.parametrize(
    "params",
    [{"apply": "yes"}, {"plan_ids": []}, {"plan_ids": "pln-a"}, {"plan_ids": [""]}, {"root": "/x"}],
)
def test_bad_params_are_refused(repo, params):
    with pytest.raises(CallerFacingValidationError):
        _call(repo, **params)


def test_a_missing_repo_root_is_refused():
    with pytest.raises(CallerFacingValidationError):
        op._handler({}, repo_root=None)


def test_an_empty_repo_has_no_candidates(repo):
    assert _call(repo)["candidates"] == []


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_dry_run_spawns_nothing_and_apply_spawns_nothing(repo, monkeypatch):
    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

    _populate(repo)
    with _count_spawns_attributed(monkeypatch) as dry:
        assert len(_call(repo)["candidates"]) == 3
    assert dry == [], [s.argv for s in dry]

    with _count_spawns_attributed(monkeypatch) as applied:
        assert len(_call(repo, apply=True)["applied"]) == 3
    assert applied == [], [s.argv for s in applied]
