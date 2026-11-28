"""`hooks.submit_bug_blitz.evaluate` on a tmp repo through the real `dispatch.emit` queue route, with fake
parse and mint: the rewrite envelope, the run records, the denials, and an exact spawn count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.bug_blitz.contract import FailingTest, ParseResult, TargetSpec, run_tag
from coordinator_core.hooks import submit_bug_blitz as mod
from coordinator_core.session_hub_path import hub_for_common

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN_ID = "20261015T120000Z"
_FIXTURE_PROFILE = (
    Path(__file__).resolve().parents[2] / "ops" / "dispatch_emit" / "tests" / "fixtures" / "queue-profiles" / "fixture.yaml"
)
TESTS = (
    FailingTest(file="pkg/a.test.ts", name="a one", package="pkg", message="boom"),
    FailingTest(file="pkg/b.test.ts", name="b one", package="pkg"),
)


def _budget() -> int:
    return load_manifest()["overrides"]["hooks.submit_bug_blitz"]["spawn_count_budget"]["targets_submission"]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    from coordinator_core import machine_resolver

    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: str(tmp_path / "scratch") if key == "paths.scratch_root" else None
    )
    root = tmp_path / "repo"
    (root / "state" / "bug-backlog").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    _head(root, "work/box/2026-10-15")
    profiles = tmp_path / "queue-profiles"
    profiles.mkdir()
    (profiles / "bug.yaml").write_text(_FIXTURE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "_profile_dir", lambda: str(profiles))
    return root


def _head(root: Path, branch: str) -> None:
    (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def _payload(root: Path, args, name: str = "coordinator:bug-blitz") -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": name, "args": args},
        "session_id": "sess-blitz",
        "cwd": str(root),
    }


def _resolve(raw: str, root: Path) -> TargetSpec:
    return TargetSpec(kind="backlog" if raw == "backlog" else "dir", path="" if raw == "backlog" else raw)


def _parse(spec: TargetSpec) -> ParseResult:
    return ParseResult(tests=TESTS)


def _mint(root: Path, run_id: str, tests: tuple, *, target_ref: str) -> list[str]:
    return [f"TF-{i}" for i, _ in enumerate(sorted({t.file for t in tests}))]


def _call(root: Path, args, **kw) -> dict:
    kw.setdefault("resolve", _resolve)
    kw.setdefault("parse", _parse)
    kw.setdefault("mint", _mint)
    kw.setdefault("run_id", RUN_ID)
    return mod.evaluate(_payload(root, args), **kw)


def _reason(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _no_grant_records(root: Path) -> None:
    assert not [p for p in root.rglob("*") if p.is_file() and "tier-u" in p.name.lower()]


def test_targets_submission_emits_the_grind_and_rewrites_the_call(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = _call(repo, {"targets": "red-set"})

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    updated = hso["updatedInput"]
    script = Path(updated["scriptPath"])
    assert script.is_file()
    assert script.parent == repo / "state" / "scratch" / "bug-blitz" / RUN_ID
    assert script.with_name(script.name + ".emitted.json").is_file()
    assert updated["args"]["run_stamp"] == RUN_ID
    assert updated["args"]["script_path"] == str(script)
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert decisions["commit_authority"] == {"basis": "submission"}
    assert decisions["verify"] == "targeted-tests"
    assert decisions["parse"] == {"parsed": 2}
    assert decisions["minted"] == ["state/bug-backlog/TF-0.yaml", "state/bug-backlog/TF-1.yaml"]
    assert len(spawns) == _budget(), [s.argv for s in spawns]
    _no_grant_records(repo)


def test_a_cone_write_failure_refuses_before_any_row_is_minted(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    minted: list = []

    def _boom(run_dir, tests) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_cone", _boom)
    env = _call(repo, {"targets": "red-set"}, mint=lambda *a, **k: minted.append(1) or [])

    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert "sparse cone was not written" in _reason(env)
    assert minted == []


def test_decisions_name_the_run_clone_path_without_provisioning_it(repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import tempfile

    from coordinator_core.bug_blitz import worker_clone

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = _call(repo, {"targets": "red-set"})
        expected = worker_clone._clones_dir(repo) / RUN_ID

    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    work_root = Path(decisions["work_root"])
    assert work_root.parent.name == worker_clone.CLONES_DIRNAME and work_root.name == RUN_ID
    assert work_root.is_relative_to(tmp_path / "scratch")
    assert work_root == expected
    assert not work_root.exists()
    assert worker_clone.clone_root(repo, RUN_ID) is None
    assert len(spawns) == _budget(), [s.argv for s in spawns]


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.fixture
def real_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo on a work branch, with the bug profile in place."""
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID", "QUEUE_APPEND_OUTPUT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    root = tmp_path / "real"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/box/2026-10-15")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "state" / "bug-backlog").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    profiles = tmp_path / "queue-profiles"
    profiles.mkdir()
    (profiles / "bug.yaml").write_text(_FIXTURE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "_profile_dir", lambda: str(profiles))
    return root


def _porcelain_state(root: Path) -> list[str]:
    return [ln for ln in _git(root, "status", "--porcelain", "--", "state/bug-backlog").splitlines() if ln]


def test_default_mint_lands_every_minted_row_in_one_commit_naming_the_run(real_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    before = int(_git(real_repo, "rev-list", "--count", "HEAD").strip())
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = _call(real_repo, {"targets": "red-set"}, mint=None)
    assert not [s.argv for s in spawns], "the settle commit runs in process; no git spawn"

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    rows = sorted(p.name for p in (real_repo / "state" / "bug-backlog").glob("*.yaml"))
    assert len(rows) == 2
    assert _porcelain_state(real_repo) == []
    assert int(_git(real_repo, "rev-list", "--count", "HEAD").strip()) == before + 1
    assert RUN_ID in _git(real_repo, "log", "-1", "--format=%s")
    assert sorted(_git(real_repo, "show", "--name-only", "--format=", "HEAD").split()) == [
        f"state/bug-backlog/{r}" for r in rows
    ]


_SIX = (
    FailingTest(file="a/test_x.py", name="TestX::test_one", message="boom"),
    FailingTest(file="a/test_x.py", name="TestX::test_two", message="boom"),
    FailingTest(file="a/test_x.py", name="test_plain", message="boom"),
    FailingTest(file="b/test_y.py", name="TestY::test_one", message="boom"),
    FailingTest(file="b/test_y.py", name="TestY::test_two", message="boom"),
    FailingTest(file="c/test_z.py", name="test_only", message="boom"),
)


def test_default_path_six_targets_in_three_files_mint_six_rows_and_six_accounts(real_repo) -> None:
    env = _call(real_repo, {"targets": "red-set"}, mint=None, parse=lambda spec: ParseResult(tests=_SIX))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert len(list((real_repo / "state" / "bug-backlog").glob("*.yaml"))) == 6
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    row_ids = [t["row_id"] for t in decisions["targets"]]
    assert len(row_ids) == 6 == len(set(row_ids))
    assert len(decisions["minted"]) == 6
    _call(real_repo, {"targets": "red-set"}, mint=None, run_id=RUN_ID, parse=lambda spec: ParseResult(tests=_SIX))
    assert len(list((real_repo / "state" / "bug-backlog").glob("*.yaml"))) == 6
    assert _porcelain_state(real_repo) == []


def test_second_run_reusing_rows_commits_their_new_tag(real_repo) -> None:
    _call(real_repo, {"targets": "red-set"}, mint=None)
    _call(real_repo, {"targets": "red-set"}, mint=None, run_id="20261015T130000Z")

    assert _porcelain_state(real_repo) == []
    assert len(list((real_repo / "state" / "bug-backlog").glob("*.yaml"))) == 2
    assert "20261015T130000Z" in _git(real_repo, "log", "-1", "--format=%s")


def test_a_refused_mint_commit_reaps_the_new_rows_and_denies(real_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import commit as commit_mod

    def _refuse(*a, **k):
        raise commit_mod.CommitRefused("refused for test")

    monkeypatch.setattr(commit_mod, "commit_paths", _refuse)
    env = _call(real_repo, {"targets": "red-set"}, mint=None)

    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert not list((real_repo / "state" / "bug-backlog").glob("*.yaml"))


def _seed_busy_box(root: Path, *, rows: int, agents: int, events: int) -> None:
    """An open-row queue and a session whose spawned agents each left a touch record."""
    from coordinator_core.session import touch_record

    queue = root / "state" / "bug-backlog"
    for i in range(rows):
        (queue / f"row-{i}.yaml").write_text(
            f"id: row-{i}\nstatus: open\nseverity: P1\ntags: [t-{i % 7}]\nsurface: pkg/mod_{i}.py\n", encoding="utf-8"
        )
    agents_dir = hub_for_common(root / ".git") / ".agents"
    for a in range(agents):
        agent_dir = agents_dir / f"a{a:04d}"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("sess-blitz\n", encoding="utf-8")
        lines = b"".join(
            touch_record.encode_line(
                session_id="sess-blitz",
                agent_id=f"a{a:04d}",
                verb="T",
                path=f"state/bug-backlog/row-{(a + e) % rows}.yaml" if e % 2 else f"pkg/f{a}_{e}.py",
            )
            for e in range(events)
        )
        (agent_dir / "touch-record.jsonl").write_bytes(lines)


def test_submission_on_a_busy_box_stays_inside_the_hook_process_budget(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    _seed_busy_box(repo, rows=235, agents=3200, events=4)
    target_ms = load_manifest()["overrides"]["hooks.submit_bug_blitz"]["target_ms"]
    _call(repo, {"targets": "red-set"}, run_id="20261015T110000Z")  # imports and record folds paid once, as a warm engine has; the bound is warm-engine, fake parse/mint only

    with _count_spawns_attributed(monkeypatch) as spawns:
        started = time.process_time()
        env = _call(repo, {"targets": "red-set"})
        elapsed_ms = (time.process_time() - started) * 1000

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert len(decisions[mod.BASELINE_DIRTY_KEY]) == 235, "the claim scan did not see the seeded agents"
    assert len(spawns) == 0, [s.argv for s in spawns]
    assert elapsed_ms < target_ms, f"{elapsed_ms:.0f}ms process time"


BRIGHTLINE_MS = 500
_SHARED_DIR = "pkg/with/a/long/shared/path/prefix/for/the/slug/tests"


def _seed_archived_rows(root: Path, *, count: int) -> None:
    """Closed `tf` rows in the archive whose capped title slug all share one prefix, as a long-lived box's do."""
    from coordinator_core.bug_blitz.mint import _slug_from_title
    from coordinator_core.bug_blitz.archive_index import backfill

    month = root / "archive" / "bug-backlog" / "2026-09"
    month.mkdir(parents=True)
    for i in range(count):
        title = f"TF: {_SHARED_DIR}/test_old{i}.py::test_one"
        (month / f"2026-09-01-{_slug_from_title(title)}-{i:012x}.yaml").write_text(
            f'created: 2026-09-01\ntitle: "{title}"\nbody: |-\n  Failing test and first message line:\n'
            f"  - {_SHARED_DIR}/test_old{i}.py::test_one\nstatus: closed-fixed\nsurface: {_SHARED_DIR}/test_old{i}.py\n"
            "severity: P1\nfrom_repo: box\ntags:\n  - tf\n  - bug-blitz-20260901T000000Z\n",
            encoding="utf-8",
        )
    assert backfill(root) == count


def test_default_submission_over_five_targets_stays_inside_the_brightline(real_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    _seed_archived_rows(real_repo, count=800)
    (real_repo / "warm.txt").write_text("warm/test_w.py::test_w\n", encoding="utf-8")
    (real_repo / "red.txt").write_text(
        "".join(f"{_SHARED_DIR}/test_new{i}.py::test_one\n" for i in range(5)), encoding="utf-8"
    )
    _call(real_repo, {"targets": "warm.txt"}, mint=None, parse=None, resolve=None, run_id="20261015T110000Z")  # imports paid once, as a warm engine has them

    with _count_spawns_attributed(monkeypatch) as spawns:
        started = time.process_time()
        env = _call(real_repo, {"targets": "red.txt"}, mint=None, parse=None, resolve=None)
        elapsed_ms = (time.process_time() - started) * 1000

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert script.is_file() and script.with_name(script.name + ".emitted.json").is_file()
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert len(decisions["minted"]) == 5
    assert _porcelain_state(real_repo) == []
    assert not [s.argv for s in spawns]
    assert elapsed_ms < BRIGHTLINE_MS, f"{elapsed_ms:.0f}ms process time"


def _seed_full_backlog(root: Path, *, rows: int) -> None:
    """Open `tf` rows of a long-lived box's size, each a few KB, named as the appender names them."""
    body = "\n".join(f"  line {k} of a long evidence block with some words in it" for k in range(40))
    queue = root / "state" / "bug-backlog"
    for i in range(rows):
        (queue / f"2026-0{1 + i % 9}-01-tf-pkg-mod-{i}-py-test-a-{i:012x}.yaml").write_text(
            f'created: 2026-01-01\ntitle: "TF: pkg/mod_{i}.py::test_a"\nbody: |-\n{body}\nstatus: open\n'
            f"severity: P1\nfrom_repo: box\ntags:\n  - tf\n  - bug-blitz-2026010{i % 9}T000000Z\n"
            f"surface: pkg/mod_{i}.py\nevidence: state/x.txt\nrepro_steps: targeted-test-run --row pkg/mod_{i}.py\n",
            encoding="utf-8",
        )


def test_default_submission_over_a_full_backlog_reads_only_the_rows_its_targets_name(real_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    _seed_full_backlog(real_repo, rows=900)
    _seed_archived_rows(real_repo, count=800)
    _git(real_repo, "add", "-A")
    _git(real_repo, "commit", "-q", "-m", "box state")
    (real_repo / "warm.txt").write_text("warm/test_w.py::test_w\n", encoding="utf-8")
    (real_repo / "red.txt").write_text(
        "".join(f"{_SHARED_DIR}/test_new{i}.py::test_one\n" for i in range(5)), encoding="utf-8"
    )
    _call(real_repo, {"targets": "warm.txt"}, mint=None, parse=None, resolve=None, run_id="20261015T110000Z")

    from coordinator_core.ops import queue_family

    walks: list[str] = []
    real_mint = mod._default_mint

    def watched_mint(*a, **k):
        def whole_queue_walk(*qa, **qk):
            walks.append("query_records")
            raise AssertionError("the mint walked the whole queue")

        monkeypatch.setattr(queue_family, "query_records", whole_queue_walk)
        return real_mint(*a, **k)

    monkeypatch.setattr(mod, "_default_mint", watched_mint)
    real_read = Path.read_bytes

    def watched_read(path):
        assert not (
            path.parent == real_repo / "state" / "bug-backlog"
            and path.name.startswith("2026-0")
            and "-tf-pkg-mod-" in path.name
        ), f"opened an unrelated backlog row: {path.name}"
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", watched_read)
    with _count_spawns_attributed(monkeypatch) as spawns:
        started = time.process_time()
        env = _call(real_repo, {"targets": "red.txt"}, mint=None, parse=None, resolve=None)
        elapsed_ms = (time.process_time() - started) * 1000

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert script.is_file() and script.with_name(script.name + ".emitted.json").is_file()
    assert not [s.argv for s in spawns]
    assert not walks
    assert elapsed_ms < BRIGHTLINE_MS, f"{elapsed_ms:.0f}ms process time"


def _cpu_ms() -> float:
    """Process time of this process plus its reaped children, in ms (os.times children are zero on Windows)."""
    import os

    t = os.times()
    return (t.user + t.system + t.children_user + t.children_system) * 1000


def test_default_submission_of_a_failing_tests_file_spawns_no_pytest_and_stays_inside_the_brightline(
    real_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    (real_repo / "pkg").mkdir()
    (real_repo / "pkg" / "test_red.py").write_text(
        "def test_one():\n    assert False\n\n\ndef test_two():\n    assert False\n", encoding="utf-8"
    )
    _git(real_repo, "add", "pkg")
    _git(real_repo, "commit", "-q", "-m", "red tests")
    (real_repo / "warm.txt").write_text("warm/test_w.py::test_w\n", encoding="utf-8")
    (real_repo / "red.txt").write_text("pkg/test_red.py::test_one\npkg/test_red.py::test_two\n", encoding="utf-8")
    _call(real_repo, {"targets": "warm.txt"}, mint=None, parse=None, resolve=None, run_id="20261015T110000Z")  # imports paid once, as a warm engine has them

    with _count_spawns_attributed(monkeypatch) as spawns:
        before = _cpu_ms()
        env = _call(real_repo, {"targets": "red.txt"}, mint=None, parse=None, resolve=None)
        cpu_ms = _cpu_ms() - before

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert len(decisions["minted"]) == 2
    pytest_children = [s.argv for s in spawns if any("pytest" in a for a in s.argv)]
    assert not pytest_children, (
        f"{len(pytest_children)} pytest children of {len(spawns)} spawns, {cpu_ms:.0f}ms process time: {pytest_children}"
    )
    assert cpu_ms < BRIGHTLINE_MS, f"{cpu_ms:.0f}ms process time, {len(spawns)} spawns"


def test_rewritten_args_carry_a_run_token_that_admits(repo) -> None:
    from coordinator_core.ops.dispatch_emit import run_token

    env = _call(repo, {"targets": "red-set"})
    updated = env["hookSpecificOutput"]["updatedInput"]
    token = updated["args"]["run_token"]
    assert run_token.TOKEN_RE.match(token)
    assert run_token.admits(repo, RUN_ID, token, updated["args"]["script_path"])


def test_mint_failure_submits_without_a_token(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import run_token

    def _boom(*_a, **_k):
        raise OSError("no token store")

    monkeypatch.setattr(run_token, "mint", _boom)
    env = _call(repo, {"targets": "red-set"})
    assert "run_token" not in env["hookSpecificOutput"]["updatedInput"]["args"]


_FIVE = (
    FailingTest(file="pkg/a.test.ts", name="a1", package="pkg"),
    FailingTest(file="pkg/a.test.ts", name="a2", package="pkg"),
    FailingTest(file="pkg/b.test.ts", name="b1", package="pkg"),
    FailingTest(file="pkg/fleet.test.ts", name="f1", package="pkg"),
    FailingTest(file="pkg/fleet.test.ts", name="f2", package="pkg"),
)


def test_declined_target_is_accounted_named_in_the_reason_and_token_is_grind_kind(repo) -> None:
    from coordinator_core.ops.dispatch_emit import run_token

    def emit(params: dict, root: Path) -> dict:
        script = Path(params["output_path"])
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// stub\n", encoding="utf-8")
        script.with_name(script.name + ".emitted.json").write_text(
            json.dumps(
                {
                    "declined": [
                        {"id": "TF-3", "path": "state/bug-backlog/TF-3.yaml", "reason": "route to plan"},
                        {"id": "TF-4", "path": "state/bug-backlog/TF-4.yaml", "reason": "route to plan"},
                    ],
                    "not_admitted": {"row_ids": []},
                    "unselected": {"row_ids": []},
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "path": str(script)}

    env = _call(
        repo,
        {"targets": "red-set"},
        parse=lambda spec: ParseResult(tests=_FIVE),
        mint=lambda root, run_id, tests, *, target_ref: [f"TF-{i}" for i in range(5)],
        emit=emit,
    )
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    script = Path(hso["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert len(decisions["targets"]) == 5
    assert [t["disposition"] for t in decisions["targets"]] == ["row", "row", "row", "declined", "declined"]
    assert [t["row_id"] for t in decisions["targets"]] == [f"TF-{i}" for i in range(5)]
    assert "2 of 5 targets not run: TF-3 (route to plan); TF-4 (route to plan)" in hso["permissionDecisionReason"]
    assert [r["kind"] for r in run_token.live_records(repo) if r["run_id"] == RUN_ID] == ["grind"]


def test_backlog_submission_writes_empty_targets(repo) -> None:
    def emit(params: dict, root: Path) -> dict:
        script = Path(params["output_path"])
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// stub\n", encoding="utf-8")
        return {"ok": True, "path": str(script)}

    env = _call(repo, {"targets": "backlog"}, emit=emit)
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert decisions["targets"] == []
    assert "not run" not in _reason(env)


def test_emit_params_carry_run_tag_where_and_sweep_appetite(repo) -> None:
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False, "findings": "stop here"}

    env = _call(repo, {"targets": "red-set", "limit": 5}, emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen["profile"] == "bug"
    assert seen["queue"] == ["state/bug-backlog"]
    assert seen["appetite"] == "sweep"
    assert seen["overrides"] == {
        "where": [[["status", "==", "open"], ["tags", "contains", run_tag(RUN_ID)]]],
        "limit": 5,
    }


def _seen_overrides(repo, args) -> dict:
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False}

    _call(repo, args, emit=emit)
    return seen.get("overrides", {})


def test_submission_without_budget_tokens_carries_no_ceiling(repo) -> None:
    assert "budget_tokens" not in _seen_overrides(repo, {"targets": "red-set"})
    assert "budget_tokens" not in _seen_overrides(repo, {"targets": "backlog"})


def test_caller_budget_tokens_is_forwarded(repo) -> None:
    assert _seen_overrides(repo, {"targets": "red-set", "budget_tokens": 5_000_000})["budget_tokens"] == 5_000_000


def test_backlog_target_neither_parses_nor_mints_and_keeps_the_profile_where(repo) -> None:
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False}

    def boom(*a, **k):
        raise AssertionError("backlog target must not parse or mint")

    _call(repo, {"targets": "backlog", "appetite": "hunt"}, emit=emit, parse=boom, mint=boom)
    assert seen["appetite"] == "hunt"
    assert "overrides" not in seen


def test_backlog_dir_target_grinds_existing_rows_without_parsing(repo) -> None:
    (repo / "state" / "bug-backlog" / "row-a.md").write_text(
        "---\nid: row-a\nstatus: open\nseverity: P1\npm_ruling: fix it\n---\nbody\n", encoding="utf-8"
    )
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False}

    def boom(*a, **k):
        raise AssertionError("a backlog dir target must not parse or mint")

    env = _call(repo, {"targets": "state/bug-backlog"}, emit=emit, parse=boom, mint=boom, resolve=mod._default_resolve)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"  # the stub emit refused after resolve
    assert "no failing" not in _reason(env)
    assert seen["queue"] == ["state/bug-backlog"]
    assert "overrides" not in seen


def test_backlog_dir_target_allows_through_the_real_emit(repo) -> None:
    (repo / "state" / "bug-backlog" / "row-a.md").write_text(
        "---\nid: row-a\nstatus: open\nseverity: P1\npm_ruling: fix it\n---\nbody\n", encoding="utf-8"
    )
    env = _call(repo, {"targets": "./state/bug-backlog/"}, resolve=mod._default_resolve)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env


def test_backlog_submission_without_a_reap_record_declines_every_tf_row(repo) -> None:
    (repo / "live.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "state" / "bug-backlog" / "tf-a.yaml").write_text(
        "created: 2026-11-12\ntitle: TF a\nbody: b\nstatus: open\nsurface: live.py\nseverity: P1\n"
        "tags:\n  - tf\n",
        encoding="utf-8",
    )
    env = _call(repo, {"targets": "backlog"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    receipt = json.loads(script.with_name(script.name + ".emitted.json").read_text(encoding="utf-8"))
    # Only the receipt's decline reason names bug-backlog-reap; submit_bug_blitz.py emits no not-run line for `targets: backlog`.
    reasons = [d["reason"] for d in receipt["declined"]]
    assert reasons == ["reap record absent; run bug-backlog-reap"]


def test_decisions_record_the_fire_time_claim_baseline(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.session import scope

    claims = [
        "state/sizings/held.yaml",
        "state/bug-backlog/row-a.md",
        "state/queue-grind/bug/runs/x.json",
        "coordinator_core/other.py",
    ]
    seen_sid: list = []

    def fake_claims(sid, cwd=None) -> list[str]:
        seen_sid.append(sid)
        return claims

    monkeypatch.setattr(scope, "own_path_claims_in_process", fake_claims)
    env = _call(repo, {"targets": "red-set"})
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert decisions[mod.BASELINE_DIRTY_KEY] == claims[:3]
    assert seen_sid == ["sess-blitz"]


@pytest.mark.parametrize(
    "branch", ["work/machine-b/2026-10-29b", "work/box/feature-x", "example-operator/machine-a/codex-discoverability", "feature/foo"]
)
def test_any_non_default_branch_is_admitted(repo, branch) -> None:
    _head(repo, branch)
    env = _call(repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] != "deny"


@pytest.mark.parametrize("branch", ["main", "master"])
def test_denies_on_the_default_branch(repo, branch) -> None:
    _head(repo, branch)
    env = _call(repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Check out a work branch" in _reason(env)[:60]


def test_denies_a_detached_head(repo) -> None:
    (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    env = _call(repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "detached" in _reason(env)


def test_denies_an_unparseable_target_and_mints_nothing(repo) -> None:
    def parse(spec):
        raise ValueError("no failing tests parsed from red-set")

    def mint(*a, **k):
        raise AssertionError("must not mint")

    env = _call(repo, {"targets": "red-set"}, parse=parse, mint=mint)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no failing tests parsed" in _reason(env)


@pytest.mark.parametrize(
    "args",
    [{}, {"targets": ""}, {"targets": "x", "suite": "full"}, {"targets": "x", "limit": 0}, {"targets": "x", "limit": "3"}, 7],
)
def test_denies_a_malformed_args_shape(repo, args) -> None:
    env = _call(repo, args)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Submit shape" in _reason(env)


def test_a_subagent_submission_is_denied(repo) -> None:
    payload = _payload(repo, {"targets": "x"})
    payload["agent_id"] = "agent-1"
    assert mod.evaluate(payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("name", ["coordinator:engine-ask", "bug-blitz-extra", "other:bug-blitzer"])
def test_other_workflow_names_are_a_no_op(repo, name) -> None:
    assert mod.evaluate(_payload(repo, {"targets": "x"}, name=name)) == mod.no_advisory()


@pytest.mark.parametrize("name", ["bug-blitz", "coordinator:bug-blitz"])
def test_stub_name_matches_bare_and_namespaced(repo, name) -> None:
    assert mod.is_submission(_payload(repo, {}, name=name))


def test_non_workflow_tool_is_a_no_op(repo) -> None:
    payload = _payload(repo, {"targets": "x"})
    payload["tool_name"] = "Bash"
    assert mod.evaluate(payload) == mod.no_advisory()


def test_a_long_refusal_leads_with_cause_and_log_path_and_logs_the_whole_reason(repo) -> None:
    from coordinator_core.hooks.submit_engine_ask import REFUSAL_LOG_REL

    cause = "no failing tests parsed from red-set " + "x" * 400

    def parse(spec):
        raise ValueError(cause)

    env = _call(repo, {"targets": "red-set"}, parse=parse)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "no failing tests parsed" in _reason(env)[:280]
    assert REFUSAL_LOG_REL in _reason(env)[:280]
    logged = [json.loads(l) for l in (repo / REFUSAL_LOG_REL).read_text(encoding="utf-8").splitlines()]
    assert cause in logged[-1]["refusal"]
    assert "Submit shape" in logged[-1]["refusal"]


def test_a_short_refusal_is_unchanged_and_still_logged(repo) -> None:
    from coordinator_core.hooks.submit_engine_ask import REFUSAL_LOG_REL

    env = _call(repo, {})
    assert "REFUSAL_LOG" not in _reason(env) and REFUSAL_LOG_REL not in _reason(env)
    assert "targets is required." in _reason(env)
    assert (repo / REFUSAL_LOG_REL).is_file()


def test_pytest_node_id_targets_file_is_accepted_at_default_args(repo) -> None:
    (repo / "red.txt").write_text(
        "tests/test_x.py::test_a\ntests/test_x.py::test_b[a b]\n", encoding="utf-8"
    )
    minted: list = []

    def mint(root, run_id, tests, *, target_ref) -> list[str]:
        minted.extend(tests)
        return ["TF-0"]

    env = _call(repo, {"targets": "red.txt"}, resolve=None, parse=None, mint=mint)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert {(t.file, t.name) for t in minted} >= {("tests/test_x.py", "test_a"), ("tests/test_x.py", "test_b[a b]")}, minted


def test_real_parser_mints_a_bare_test_file_target_as_a_file_level_row(repo) -> None:
    (repo / "red.txt").write_text(
        "FAILED pkg/tests/test_a.py::test_one - AssertionError\npkg/tests/test_b.py\n", encoding="utf-8"
    )
    minted: list = []

    def mint(root, run_id, tests, *, target_ref):
        minted.extend(tests)
        return []

    _call(repo, {"targets": "red.txt"}, resolve=None, parse=None, mint=mint)
    assert ("pkg/tests/test_b.py", "") in {(t.file, t.name) for t in minted}, minted

def test_a_target_refusal_leads_with_its_cause_not_the_target_path(repo) -> None:
    """The harness shows ~80 chars of a deny; a long targets path ahead of the cause hid it."""
    def parse(spec):
        raise ValueError("2 target(s) could not be parsed")

    env = _call(repo, {"targets": "scratch/" + "r" * 60 + ".txt"}, parse=parse)
    assert _reason(env).split("] ", 1)[1].startswith("2 target(s) could not be parsed"), _reason(env)


_BACKLOG_ROW = (
    "created: 2026-11-12\ntitle: {title}\nbody: b\nstatus: open\nsurface: live.py\nseverity: P1\ntags:\n  - tf\n"
)


def _pm_ruling(root: Path, row_id: str):
    import yaml

    text = (root / "state" / "bug-backlog" / f"{row_id}.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text).get("pm_ruling")


def _rows_by_title(root: Path) -> dict:
    import yaml

    out = {}
    for p in (root / "state" / "bug-backlog").glob("*.yaml"):
        row = yaml.safe_load(p.read_text(encoding="utf-8"))
        out[row["title"]] = row
    return out


def test_default_submission_with_rulings_lands_pm_ruling_on_matching_rows_only(real_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    before = int(_git(real_repo, "rev-list", "--count", "HEAD").strip())
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = _call(
            real_repo,
            {"targets": "red-set", "rulings": [{"match": "b/test_y.py", "ruling": "measure, do not bump"},
                                               {"match": "no-such-row", "context": "x"}]},
            mint=None,
            parse=lambda spec: ParseResult(tests=_SIX),
        )
    assert not [s.argv for s in spawns]
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    rows = _rows_by_title(real_repo)
    ruled = {t for t, r in rows.items() if r.get("pm_ruling") == "measure, do not bump"}
    assert ruled == {"TF: b/test_y.py::TestY::test_one", "TF: b/test_y.py::TestY::test_two"}
    assert [t for t, r in rows.items() if "pm_ruling" in r and t not in ruled] == []
    assert _porcelain_state(real_repo) == []
    assert int(_git(real_repo, "rev-list", "--count", "HEAD").strip()) == before + 1
    committed = _git(real_repo, "show", "HEAD:state/bug-backlog/" + next(
        p.name for p in (real_repo / "state" / "bug-backlog").glob("*.yaml") if "pm_ruling" in p.read_text(encoding="utf-8")
    ))
    assert "measure, do not bump" in committed
    assert "Rulings applied to 2 row(s)." in _reason(env)
    assert "Matched no row: 'no-such-row'." in _reason(env)


def test_a_string_ruling_applies_to_every_minted_row(real_repo) -> None:
    env = _call(real_repo, {"targets": "red-set", "rulings": "delete dead tests"}, mint=None)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    rows = _rows_by_title(real_repo)
    assert len(rows) == 2 and all(r.get("pm_ruling") == "delete dead tests" for r in rows.values())
    assert _porcelain_state(real_repo) == []


def test_a_submission_without_rulings_leaves_rows_without_pm_ruling(real_repo) -> None:
    _call(real_repo, {"targets": "red-set"}, mint=None)
    assert all("pm_ruling" not in r for r in _rows_by_title(real_repo).values())


@pytest.mark.parametrize(
    "rulings", [5, [{"ruling": "x"}], [{"match": "a"}], [{"match": "a", "ruling": "x", "k": 1}], ["x"], {"match": "a"}]
)
def test_a_malformed_rulings_shape_is_denied_and_mints_nothing(repo, rulings) -> None:
    def boom(*a, **k):
        raise AssertionError("a refused arg shape must not mint")

    env = _call(repo, {"targets": "red-set", "rulings": rulings}, mint=boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "rulings" in _reason(env)


def test_backlog_target_with_list_rulings_writes_and_commits_matching_open_rows(real_repo) -> None:
    (real_repo / "live.py").write_text("X = 1\n", encoding="utf-8")
    for stem, title in (("row-a", "alpha defect"), ("row-b", "beta defect")):
        (real_repo / "state" / "bug-backlog" / f"{stem}.yaml").write_text(_BACKLOG_ROW.format(title=title), encoding="utf-8")
    _git(real_repo, "add", "-A")
    _git(real_repo, "commit", "-q", "-m", "rows")
    env = _call(real_repo, {"targets": "backlog", "rulings": [{"match": "alpha", "ruling": "fix it"}]})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert _pm_ruling(real_repo, "row-a") == "fix it"
    assert _pm_ruling(real_repo, "row-b") is None
    assert _porcelain_state(real_repo) == []
    assert "fix it" in _git(real_repo, "show", "HEAD:state/bug-backlog/row-a.yaml")


def test_backlog_target_with_a_bare_string_ruling_proceeds_applying_none_and_names_the_list_form(real_repo) -> None:
    (real_repo / "state" / "bug-backlog").mkdir(parents=True, exist_ok=True)
    (real_repo / "state" / "bug-backlog" / "row-a.yaml").write_text(_BACKLOG_ROW.format(title="alpha defect"), encoding="utf-8")
    _git(real_repo, "add", "-A")
    _git(real_repo, "commit", "-q", "-m", "rows")
    env = _call(real_repo, {"targets": "backlog", "rulings": "everything"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert "list of {match, ruling}" in _reason(env)
    assert _pm_ruling(real_repo, "row-a") is None


def test_emit_params_carry_the_revisions_preamble_and_the_run_is_revisable(repo) -> None:
    from coordinator_core.bug_blitz.contract import blitz_revisions_rel
    from coordinator_core.ops.dispatch_emit import pipeline_run, requirements_ledger

    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        script = Path(params["output_path"])
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// stub\n", encoding="utf-8")
        return {"ok": True, "path": str(script)}

    env = _call(repo, {"targets": "red-set"}, emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert seen["preamble"] == f"{pipeline_run.REVISIONS_MARKER} Revisions file: {blitz_revisions_rel(RUN_ID)}"
    assert requirements_ledger._parent_lists_run(repo, RUN_ID)


def test_the_real_emit_writes_a_script_that_reads_revisions_and_names_the_file(repo) -> None:
    from coordinator_core.bug_blitz.contract import blitz_revisions_rel, blitz_script_rel
    from coordinator_core.ops.dispatch_emit import pipeline_run

    env = _call(repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = (repo / blitz_script_rel(RUN_ID)).read_text(encoding="utf-8")
    assert pipeline_run.reads_revisions(script)
    assert blitz_revisions_rel(RUN_ID) in script


def test_a_run_parent_failure_does_not_refuse_the_submission(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract

    def refuse(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(ask_fanout_contract, "mint_run_parent", refuse)
    assert _call(repo, {"targets": "red-set"})["hookSpecificOutput"]["permissionDecision"] == "allow"


_RED_SET = "alpha/tests/test_a.py::test_one\nbeta/sub/test_b.py::test_two\nalpha/tests/test_a2.py\ntest_root.py::test_r\n"


def _red_set_file(root: Path) -> str:
    (root / "state" / "red-sets").mkdir(parents=True)
    (root / "state" / "red-sets" / "x.txt").write_text(_RED_SET, encoding="utf-8")
    return "state/red-sets/x.txt"


def _submit_from(root: Path, cwd: Path, targets: str) -> dict:
    payload = _payload(root, {"targets": targets})
    payload["cwd"] = str(cwd)
    return mod.evaluate(payload, resolve=None, parse=None, mint=None, run_id=RUN_ID)


@pytest.mark.parametrize("cwd_sub", ["", "deep/er"])
def test_a_repo_relative_targets_path_is_accepted_exactly_as_its_absolute_spelling(real_repo, cwd_sub: str) -> None:
    rel = _red_set_file(real_repo)
    cwd = real_repo / cwd_sub if cwd_sub else real_repo
    cwd.mkdir(parents=True, exist_ok=True)

    relative = _submit_from(real_repo, cwd, rel)
    assert relative["hookSpecificOutput"]["permissionDecision"] == "allow", relative
    script = Path(relative["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    rel_decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))

    absolute = _submit_from(real_repo, cwd, (real_repo / rel).as_posix())
    assert absolute["hookSpecificOutput"]["permissionDecision"] == "allow", absolute
    abs_decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert rel_decisions["parse"] == abs_decisions["parse"] == {"parsed": 4}
    assert rel_decisions["minted"] == abs_decisions["minted"]


def test_a_relative_targets_path_that_does_not_exist_is_still_refused(real_repo) -> None:
    env = _submit_from(real_repo, real_repo, "state/red-sets/missing.txt")
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env


def test_the_submission_writes_the_cone_of_the_parsed_tests_top_level_directories(real_repo) -> None:
    from coordinator_core.bug_blitz import worker_clone

    env = _submit_from(real_repo, real_repo, _red_set_file(real_repo))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert worker_clone.read_cone(script.parent) == ("alpha", "beta")


def test_a_root_level_only_submission_writes_no_cone_file(real_repo) -> None:
    from coordinator_core.bug_blitz import worker_clone

    (real_repo / "red.txt").write_text("test_root.py::test_r\n", encoding="utf-8")
    env = _submit_from(real_repo, real_repo, "red.txt")
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert not (script.parent / worker_clone.CONE_FILE).exists()
