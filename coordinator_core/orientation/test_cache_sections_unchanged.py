"""Byte-pin of every orientation-cache section except ``## Stray venvs``.

Each case builds a fixture repo under ``tmp_path`` and asserts the full
``build_cache(...)["output"]`` against a literal. ``generated_at`` and commit shas are
normalised; box-state emitters are monkeypatched to fixed strings; the
``## Stray venvs`` section is stripped before comparing.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.orientation import regenerate_cache as mod
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OLD = "2020-01-01T00:00:00+0000"
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_GENERATED_AT_RE = re.compile(r"^generated_at: .*$", re.MULTILINE)
_STRAY_SECTION_RE = re.compile(r"\n## Stray venvs\n.*?(?=\n## |\Z)", re.DOTALL)
_PUSH_FAILURE_LINE = (
    "[2999-01-01T00:00:00Z] push failed on {branch} (direct push/network after 3 tries)\n"
)

_TRACKER = """# Tracker

## Active workstreams

### 1. Alpha stream (in flight)
### 2. Beta stream
### 3. Gamma stream (blocked)

## Done
### 9. Old thing
"""

_LOCAL_MD = """---
fast_test_cmd: pytest -q fast
capability_pointers:
  - "example-retrieval-repo query surface: use project_semantic_search"
  - "klabauter publishes the engine"
---
"""


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com",
    )
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, env=env,
        **no_console_passthrough_kwargs(),
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_files(repo: Path) -> None:
    _write(repo / "tasks" / "project-tracker.md", _TRACKER)
    _write(repo / "tasks" / "handoff-recheck-due-2020-01-01.md", "x\n")
    _write(repo / "docs" / "wiki" / "DIRECTORY_GUIDE.md", "# guide\n")
    _write(repo / "docs" / "architecture" / "systems" / "alpha.md", "# a\n")
    _write(repo / "docs" / "architecture" / "systems" / "beta.md", "# b\n")
    _write(repo / "coordinator.local.md", _LOCAL_MD)
    _write(repo / "state" / "audits" / "2026-01-01-one.md", "# audit\n")


def _repo(tmp_path: Path, *, commit: bool = True, extra: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _seed_files(repo)
    if extra:
        _write(repo / extra, "stub\n")
    if commit:
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "base", date=_OLD)
    return repo


def _bare_origin(tmp_path: Path, repo: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, **no_console_passthrough_kwargs(),
    )
    _git(repo, "remote", "add", "origin", str(origin))
    return origin


def _commit(repo: Path, name: str, date: str = _OLD) -> None:
    _write(repo / f"{name}.txt", name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", name, date=date)


def _scenario_work_ahead_behind(tmp_path: Path, *, push_failure: bool = False) -> Path:
    repo = _repo(tmp_path)
    _bare_origin(tmp_path, repo)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "branch", "side")
    _git(repo, "checkout", "-q", "-b", "work/stream")
    _commit(repo, "ahead-one")
    _git(repo, "push", "-q", "-u", "origin", "work/stream")
    _commit(repo, "ahead-two")
    _git(repo, "checkout", "-q", "side")
    _commit(repo, "behind-one")
    _git(repo, "push", "-q", "origin", "side:main")
    _git(repo, "checkout", "-q", "work/stream")
    _git(repo, "fetch", "-q", "origin")
    if push_failure:
        # Recency is measured against wall-clock now; a far-future stamp is always recent.
        _write(
            repo / ".git" / "push-failures.log",
            _PUSH_FAILURE_LINE.format(branch="work/stream"),
        )
    return repo


def _scenario_push_failure(tmp_path: Path) -> Path:
    return _scenario_work_ahead_behind(tmp_path, push_failure=True)


def _scenario_no_upstream(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    _bare_origin(tmp_path, repo)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "-b", "work/stream")
    _commit(repo, "local-only")
    return repo


def _scenario_main_unpushed(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    _bare_origin(tmp_path, repo)
    _git(repo, "push", "-q", "origin", "main")
    _commit(repo, "unpushed-one")
    _commit(repo, "unpushed-two")
    return repo


def _scenario_no_origin_main(tmp_path: Path) -> Path:
    return _repo(tmp_path)


def _scenario_detached(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    _commit(repo, "second")
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    return repo


def _scenario_no_commits(tmp_path: Path) -> Path:
    return _repo(tmp_path, commit=False)


def _scenario_uproject(tmp_path: Path) -> Path:
    return _repo(tmp_path, extra="Game/Game.uproject")


def _scenario_not_git(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_files(repo)
    return repo


SCENARIOS = {
    "work_ahead_behind": _scenario_work_ahead_behind,
    "work_push_failure": _scenario_push_failure,
    "no_upstream": _scenario_no_upstream,
    "main_unpushed": _scenario_main_unpushed,
    "no_origin_main": _scenario_no_origin_main,
    "detached_head": _scenario_detached,
    "no_commits": _scenario_no_commits,
    "uproject": _scenario_uproject,
    "not_a_git_repo": _scenario_not_git,
}


def _normalise(text: str) -> str:
    text = _GENERATED_AT_RE.sub("generated_at: <t>", text)
    text = _SHA_RE.sub("<sha>", text)
    return _STRAY_SECTION_RE.sub("", text)


def _build(tmp_path: Path, monkeypatch, name: str) -> str:
    repo = SCENARIOS[name](tmp_path)
    monkeypatch.setattr(mod, "emit_warm_engine_health", lambda: "")
    monkeypatch.setattr(mod, "emit_route_unreachable", lambda: "")
    monkeypatch.setattr(mod, "emit_budget_breaches", lambda repo_root: "")
    monkeypatch.setattr(mod, "emit_expired_grants", lambda repo_root: "")
    monkeypatch.setattr(mod, "_emit_housekeeping", lambda repo_root: [])
    result = mod.build_cache(invoker="handoff", repo_root=repo)
    assert not result["skipped"]
    return _normalise(result["output"])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_cache_bytes_unchanged(name, tmp_path, monkeypatch):
    assert _build(tmp_path, monkeypatch, name) == EXPECTED[name]


_TAIL = """
## Wiki
- `docs""" """/wiki/DIRECTORY_GUIDE.md` — doctrine/reference index; start there.

## Architecture atlas
- `docs/architecture/systems/` — per-subsystem architecture pages: alpha, beta

## Capabilities
- example-retrieval-repo query surface: use project_semantic_search
- klabauter publishes the engine

## Fast test
- fast test: `pytest -q fast`

## Audits & censuses
- `state/audits/` — existing investigation records
"""

EXPECTED = {
    "detached_head": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`HEAD` (no origin/main reference)

## Recent commits
- <sha> base
""" + _TAIL,
    "main_unpushed": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`main` — 2/0 vs origin/main

## Recent commits
- <sha> unpushed-two
- <sha> unpushed-one
- <sha> base

## Auto-push health
- ⚠ 2 unpushed commit(s) on `main` — the push cadence publishes `work/*` only, so nothing will publish this branch. Push it: git push origin main
""" + _TAIL,
    "no_commits": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: unknown
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`unknown` (no origin/main reference)
""" + _TAIL,
    "no_origin_main": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`main` (no origin/main reference)

## Recent commits
- <sha> base

## Auto-push health
- ⚠ 1 unpushed commit(s) on `main` — no upstream; the push cadence has no ref to compare and never publishes this branch. Adopt one: git push -u origin main
""" + _TAIL,
    "no_upstream": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`work/stream` — 1/0 vs origin/main

## Recent commits
- <sha> local-only
- <sha> base

## Auto-push health
- ⚠ 1 unpushed commit(s) on `work/stream` — no upstream; the push cadence has no ref to compare and never publishes this branch. Adopt one: git push -u origin work/stream
""" + _TAIL,
    "not_a_git_repo": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: unknown
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`unknown` (no origin/main reference)
""" + _TAIL,
    "uproject": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Trust caveats
- Unreal Engine project detected (`Game/Game.uproject`) — do NOT trust your training data on UE5 APIs, classes, or Blueprint semantics. Verify every claim via `mcp__project-rag__*` tools or dispatch `game-dev:staff-game-dev` (the Game Dev Reviewer). This applies to your delegates — restate it in every UE dispatch brief.

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`main` (no origin/main reference)

## Recent commits
- <sha> base

## Auto-push health
- ⚠ 1 unpushed commit(s) on `main` — no upstream; the push cadence has no ref to compare and never publishes this branch. Adopt one: git push -u origin main
""" + _TAIL,
    "work_ahead_behind": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`work/stream` — 2/1 vs origin/main

## Recent commits
- <sha> ahead-two
- <sha> ahead-one
- <sha> base

## Auto-push health
- ⚠ 1 unpushed commit(s) on `work/stream` — auto-push lagging; run: git push origin work/stream (see .git/push-failures.log)
""" + _TAIL,
    "work_push_failure": """\
---
generated_by: handoff
generated_at: <t>
git_head_at_generation: <sha>
---

# Orientation Cache

## Active workstreams
1. Alpha stream
2. Beta stream
3. Gamma stream

## Rechecks due ≤7 days
- `tasks/handoff-recheck-due-2020-01-01.md` (due 2020-01-01)

## Branch
`work/stream` — 2/1 vs origin/main

## Recent commits
- <sha> ahead-two
- <sha> ahead-one
- <sha> base

## Auto-push health
- ⚠ 1 unpushed commit(s) on `work/stream` — auto-push lagging; last failure: network; transient — retry: git push
""" + _TAIL,
}
