"""Pins `workweek_complete.apply`'s commit tail against a real throwaway `tmp_path`
git repo: the after-minus-before path-set rule, the fail-closed skips, the
no-widening stage argument, claim release scope and the post-commit check.

Never this repo: the commit root, session id and peer resolver are patched to
the fixture repo. Directives are fake CLI modules whose `main` edits the fixture.

Run: python3 -m pytest coordinator_core/workweek_complete/test_apply_commit_tail.py -q -p no:randomly
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_commit_tail as tail
from coordinator_core.workweek_complete import apply as wwc_apply

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "test-session-c3"
UNICODE_PATH = "out/sp ace/café ☃.md"


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        **no_console_creationflags(),
    )
    return proc.stdout


def _head_files(repo: Path) -> set[str]:
    out = _git(["-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "HEAD"], repo)
    return set(out.splitlines())


def _head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.example"], root)
    _git(["config", "user.name", "t"], root)
    for name in ("README.md", "tracked.md", "doc.md", "old.md"):
        (root / name).write_text(f"seed {name}\n", encoding="utf-8")
    _git(["add", "--", "README.md", "tracked.md", "doc.md", "old.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(wwc_apply, "_resolve_commit_root", lambda: str(root))
    monkeypatch.setattr(wwc_apply, "resolve_session_id", lambda *_a, **_k: SID)
    monkeypatch.setattr(
        tail, "resolve_known_concurrent_paths", lambda *_a, **_k: set()
    )
    monkeypatch.setattr(wwc_apply, "flush_composition_record", lambda *_a, **_k: None)
    return root


@pytest.fixture
def claims(monkeypatch) -> dict[str, list]:
    """Spies on both claim-release entrypoints so no real session state moves."""
    calls: dict[str, list] = {"committed": [], "all": []}
    monkeypatch.setattr(
        wwc_apply.session_scope,
        "release_committed_claims",
        lambda sid, paths, cwd=None: calls["committed"].append((sid, sorted(paths))),
    )
    monkeypatch.setattr(
        wwc_apply.session_scope,
        "release_all_committed_claims",
        lambda *a, **k: calls["all"].append(a),
    )
    return calls


def _install(
    monkeypatch, bodies: dict[str, Callable[[], int]]
) -> list[dict[str, Any]]:
    """Patches brief and the CLI loader; returns the directive list it served.
    `bodies` maps a consumes-manifest cli name to the fake `main`'s effect."""
    directives = [
        {
            "id": f"d_{cli.replace('-', '_')}",
            "cli": cli,
            "args": [],
            "depends_on": None,
            "already_satisfied": False,
        }
        for cli in bodies
    ]
    modules: dict[str, ModuleType] = {}
    for cli, body in bodies.items():
        mod = ModuleType(f"fake_{cli}")
        mod.main = lambda _argv=None, _body=body: _body()  # type: ignore[attr-defined]
        modules[cli] = mod
    monkeypatch.setattr(
        wwc_apply,
        "brief",
        lambda decisions=None: (
            0,
            {"directives": directives, "judgment_points": [], "decisions": {}},
        ),
    )
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda name: modules[name])
    return directives


def _write(repo: Path, rel: str, text: str = "new\n") -> Callable[[], int]:
    def body() -> int:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return 0

    return body


def _all(*bodies: Callable[[], int]) -> Callable[[], int]:
    def body() -> int:
        for b in bodies:
            b()
        return 0

    return body


def _spy_close_commit(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    real = tail.run_close_commit

    def spy(root, **kwargs):
        seen.append(kwargs)
        return real(root, **kwargs)

    monkeypatch.setattr(tail, "run_close_commit", spy)
    return seen


SUCCESS = int(wwc_apply.WorkweekApplyExitCode.SUCCESS)
PARTIAL = int(wwc_apply.WorkweekApplyExitCode.PARTIAL_MUTATION)


def test_untracked_dir_and_modified_file_both_committed(repo, monkeypatch, claims):
    _install(
        monkeypatch,
        {
            "list-week-changelog": _all(
                _write(repo, "out/new-dir/a.md"), _write(repo, "tracked.md", "edited\n")
            )
        },
    )
    before = _head_sha(repo)
    code, report = wwc_apply.apply()
    commit = report["commit"]
    assert code == SUCCESS
    assert commit["committed_sha"] and commit["committed_sha"] != before
    assert {"out/new-dir/a.md", "tracked.md"} <= _head_files(repo)
    assert (repo / "tracked.md").read_text(encoding="utf-8") == "edited\n"
    assert _git(["status", "--porcelain"], repo) == ""


def test_preexisting_dirty_file_is_not_committed(repo, monkeypatch, claims):
    (repo / "doc.md").write_text("operator wip\n", encoding="utf-8")
    _install(
        monkeypatch,
        {
            "list-week-changelog": _all(
                _write(repo, "doc.md", "directive edit\n"), _write(repo, "out/b.md")
            )
        },
    )
    _code, report = wwc_apply.apply()
    commit = report["commit"]
    assert commit["preexisting_dirty"] == ["doc.md"]
    assert "doc.md" not in commit["staged"]
    assert "out/b.md" in _head_files(repo)
    assert _git(["show", "HEAD:doc.md"], repo) == "seed doc.md\n"
    assert " M doc.md" in _git(["status", "--porcelain"], repo)


def test_peer_path_is_withheld(repo, monkeypatch, claims):
    monkeypatch.setattr(
        tail, "resolve_known_concurrent_paths", lambda *_a, **_k: {"out/peer.md"}
    )
    _install(
        monkeypatch,
        {"list-week-changelog": _all(_write(repo, "out/peer.md"), _write(repo, "out/mine.md"))},
    )
    _code, report = wwc_apply.apply()
    commit = report["commit"]
    assert commit["withheld_peer"] == ["out/peer.md"]
    files = _head_files(repo)
    assert "out/mine.md" in files and "out/peer.md" not in files


def test_deleted_tracked_file_lands(repo, monkeypatch, claims):
    def delete() -> int:
        (repo / "old.md").unlink()
        return 0

    _install(monkeypatch, {"list-week-changelog": _all(delete, _write(repo, "out/c.md"))})
    _code, report = wwc_apply.apply()
    assert report["commit"]["deleted"] == ["old.md"]
    assert "old.md" not in _head_files(repo)


def test_space_and_non_ascii_path_committed_intact(repo, monkeypatch, claims):
    _install(monkeypatch, {"list-week-changelog": _write(repo, UNICODE_PATH)})
    _code, report = wwc_apply.apply()
    assert UNICODE_PATH in report["commit"]["staged"]
    assert UNICODE_PATH in _head_files(repo)


def test_no_session_id_skips_without_moving_exit_code(repo, monkeypatch, claims):
    monkeypatch.setattr(wwc_apply, "resolve_session_id", lambda *_a, **_k: "")
    _install(monkeypatch, {"list-week-changelog": _write(repo, "out/d.md")})
    before = _head_sha(repo)
    code, report = wwc_apply.apply()
    assert code == SUCCESS
    assert report["commit"] == {"attempted": False, "skipped": "workweek-commit:no-session-id"}
    assert _head_sha(repo) == before


def test_pre_snapshot_failure_skips(repo, monkeypatch, claims):
    real = wwc_apply._dirty_snapshot
    calls = {"n": 0}

    def flaky(root):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(root)

    monkeypatch.setattr(wwc_apply, "_dirty_snapshot", flaky)
    _install(monkeypatch, {"list-week-changelog": _write(repo, "out/e.md")})
    before = _head_sha(repo)
    _code, report = wwc_apply.apply()
    assert report["commit"]["skipped"] == "workweek-commit:snapshot-failed"
    assert report["commit"]["attempted"] is False
    assert _head_sha(repo) == before


def test_admission_refusal_skips_as_nothing_dispatched(repo, monkeypatch, claims):
    _install(monkeypatch, {"list-week-changelog": _write(repo, "out/f.md")})
    monkeypatch.setattr(
        wwc_apply,
        "brief",
        lambda decisions=None: (
            0,
            {
                "directives": [
                    {"id": "d_bad", "cli": "no-such-cli", "args": [], "already_satisfied": False}
                ],
                "judgment_points": [],
                "decisions": {},
            },
        ),
    )
    code, report = wwc_apply.apply()
    assert code == int(wwc_apply.WorkweekApplyExitCode.DIRECTIVE_FAILED)
    assert report["commit"]["skipped"] == "workweek-commit:nothing-dispatched"


def test_failing_directive_beside_writer_still_commits(repo, monkeypatch, claims):
    _install(
        monkeypatch,
        {
            "list-week-changelog": _write(repo, "out/g.md"),
            "lint-frontmatter": lambda: 1,
        },
    )
    code, report = wwc_apply.apply()
    assert code == PARTIAL
    assert report["commit"]["committed_sha"]
    assert "out/g.md" in _head_files(repo)


def test_commit_failed_moves_success_to_partial(repo, monkeypatch, claims):
    monkeypatch.setattr(
        tail,
        "run_close_commit",
        lambda *_a, **_k: SimpleNamespace(
            commit_failed=True, committed_sha=None, diagnostics=["boom"]
        ),
    )
    _install(monkeypatch, {"list-week-changelog": _write(repo, "out/h.md")})
    code, report = wwc_apply.apply()
    assert code == PARTIAL
    assert report["commit"]["commit_failed"] is True


def test_post_commit_dirty_sets_partial_and_keeps_sha(repo, monkeypatch, claims):
    real = wwc_apply.git_native.status_porcelain

    def fake(cwd, paths=None, **kwargs):
        if paths is not None:
            return SimpleNamespace(ok=True, stdout=f" M {paths[0]}\n", stderr="")
        return real(cwd, paths, **kwargs)

    monkeypatch.setattr(wwc_apply.git_native, "status_porcelain", fake)
    _install(monkeypatch, {"list-week-changelog": _write(repo, "out/i.md")})
    code, report = wwc_apply.apply()
    commit = report["commit"]
    assert code == PARTIAL
    assert commit["post_commit_dirty"] == ["out/i.md"]
    assert commit["committed_sha"] == _head_sha(repo)


def test_nothing_written_is_attempted_without_sha(repo, monkeypatch, claims):
    _install(monkeypatch, {"list-week-changelog": lambda: 0})
    before = _head_sha(repo)
    code, report = wwc_apply.apply()
    assert code == SUCCESS
    assert report["commit"]["attempted"] is True
    assert report["commit"]["committed_sha"] is None
    assert _head_sha(repo) == before


def test_claim_release_is_scoped_to_committed_paths(repo, monkeypatch, claims):
    _install(
        monkeypatch,
        {"list-week-changelog": _all(_write(repo, "out/j.md"), _write(repo, "out/k.md"))},
    )
    _code, report = wwc_apply.apply()
    assert report["commit"]["committed_sha"]
    assert claims["committed"] == [(SID, ["out/j.md", "out/k.md"])]
    assert claims["all"] == []


def test_stage_paths_are_exactly_the_delta_and_never_directories(repo, monkeypatch, claims):
    seen = _spy_close_commit(monkeypatch)
    _install(
        monkeypatch,
        {
            "list-week-changelog": _all(
                _write(repo, "out/new-dir/a.md"), _write(repo, "out/new-dir/deep/b.md")
            )
        },
    )
    wwc_apply.apply()
    assert len(seen) == 1
    stage = seen[0]["stage_paths"]
    assert sorted(stage) == ["out/new-dir/a.md", "out/new-dir/deep/b.md"]
    assert not any((repo / p).is_dir() or p.endswith("/") for p in stage)


def test_pre_mutation_budget_breach_skips_distinctly(repo, monkeypatch, claims):
    monkeypatch.setattr(wwc_apply, "budget_check_pre_mutation", lambda _b: {"breach": "x"})
    _install(monkeypatch, {"list-week-changelog": _write(repo, "out/l.md")})
    code, report = wwc_apply.apply()
    assert code == int(wwc_apply.WorkweekApplyExitCode.DIRECTIVE_FAILED)
    assert report["results"] == []
    assert report["commit"]["skipped"] == "workweek-commit:nothing-dispatched"
    assert not (repo / "out/l.md").exists()


def test_linked_worktree_refuses_but_directives_run(repo, tmp_path, monkeypatch, claims):
    linked = (tmp_path / "linked-wt").resolve()
    _git(["worktree", "add", "-q", "-b", "linked-branch", str(linked), "HEAD"], repo)
    monkeypatch.chdir(linked)
    snapshots: list[Any] = []
    monkeypatch.setattr(
        wwc_apply, "_dirty_snapshot", lambda root: snapshots.append(root) or {}
    )
    ran: list[int] = []
    _install(monkeypatch, {"list-week-changelog": lambda: ran.append(1) or 0})
    before = _head_sha(repo)
    code, report = wwc_apply.apply()
    assert code == SUCCESS
    assert report["commit"] == {"attempted": False, "skipped": "workweek-commit:linked-worktree"}
    assert snapshots == []
    assert ran == [1]
    assert _head_sha(repo) == before


def test_staged_rename_commits_source_deletion_and_destination(repo, monkeypatch, claims):
    seen = _spy_close_commit(monkeypatch)

    def rename() -> int:
        _git(["mv", "old.md", "renamed.md"], repo)
        return 0

    _install(monkeypatch, {"list-week-changelog": rename})
    _code, report = wwc_apply.apply()
    assert report["commit"]["committed_sha"]
    files = _head_files(repo)
    assert "old.md" not in files and "renamed.md" in files
    assert seen[0]["deleted_paths"] == ["old.md"]
    assert seen[0]["stage_paths"] == ["renamed.md"]
