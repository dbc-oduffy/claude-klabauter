"""A re-emit over a commit-failed wave re-attempts that commit and keeps every earlier prompt byte-identical."""

from __future__ import annotations

import re
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import _resume_guard_clause, emit_script
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_REL = "docs/plans/p.md"
REFUSAL = "wave 1: claimed by live session 65b9230f"

_PLAN = """---
title: "p"
sizing_object: null
---

# p

## Goal

g

## Tasks

```yaml plan-tasks
- id: A1
  title: tA1
  change_kind: doc-edit
  surface: docs/x/a1.md
  writes:
    - docs/x/a1.md
  queue_scope: project
  disposition: open
  body: |
    Do A1.
```
"""

_RETRY_CONST = re.compile(r"  const _commitRetry = .*;\n")
_RETRY_EXPR = re.compile(r" \+ \(_commitRetry\[n\] \? .*? : ''\)")


def _git(root, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    plan = root / PLAN_REL
    plan.write_text(_PLAN, encoding="utf-8")
    _git(root, "add", "--", PLAN_REL)
    _git(root, "commit", "-q", "-m", "seed")
    return root, plan, _git(root, "rev-parse", "HEAD")


def _move_head(root, name="peer.txt") -> None:
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", "--", name)
    _git(root, "commit", "-q", "-m", f"unrelated {name}")


def _emit(root, plan, **kw):
    return emit_script(plan, repo_root=root, **REVIEW_KW, **kw)


def test_reattempt_changes_only_the_failed_waves_commit_prompt(tmp_path):
    root, plan, base = _repo(tmp_path)
    failed_run = _emit(root, plan)
    _move_head(root)

    retry = _emit(root, plan, resume_run_base=base, commit_retry={1: REFUSAL})

    assert failed_run != retry
    assert REFUSAL in retry and "Re-attempt the commit now" in retry
    assert _RETRY_EXPR.sub("", _RETRY_CONST.sub("", retry)) == failed_run


def test_the_pinned_base_keeps_every_earlier_prompt_in_the_resume_cache(tmp_path):
    root, plan, base = _repo(tmp_path)
    failed_run = _emit(root, plan)
    _move_head(root)

    unpinned = _emit(root, plan)
    pinned = _emit(root, plan, resume_run_base=base, commit_retry={1: REFUSAL})

    assert unpinned != failed_run
    assert f'let _runBase = "{base}";' in pinned or f"let _runBase = '{base}';" in pinned
    assert pinned.count(base) == failed_run.count(base)


def test_without_a_retry_the_script_is_unchanged(tmp_path):
    root, plan, _ = _repo(tmp_path)
    script = _emit(root, plan)
    assert "_commitRetry" not in script


@pytest.mark.parametrize("kw", [{"commit_retry": {1: REFUSAL}}, {"resume_run_base": "a" * 40}], ids=["no-base", "no-refusal"])
def test_retry_and_base_travel_together(tmp_path, kw):
    root, plan, _ = _repo(tmp_path)
    with pytest.raises(ValueError, match="only together"):
        _emit(root, plan, **kw)


def test_a_base_that_is_not_an_ancestor_of_head_is_refused(tmp_path):
    root, plan, seed = _repo(tmp_path)
    main = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(root, "checkout", "-q", "-b", "side")
    _move_head(root, "side.txt")
    side = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", main)
    with pytest.raises(ValueError, match="not an ancestor"):
        _emit(root, plan, resume_run_base=side, commit_retry={1: REFUSAL})
    _emit(root, plan, resume_run_base=seed, commit_retry={1: REFUSAL})


def test_the_commit_leg_stays_haiku_effort_low_on_a_retry(tmp_path):
    root, plan, base = _repo(tmp_path)
    _move_head(root)
    script = _emit(root, plan, resume_run_base=base, commit_retry={1: REFUSAL})
    leg = script.split("label: 'commit:wave-' + n", 1)[1].split("}", 1)[0]
    assert "effort: 'low'" in leg and "haiku" in leg


def test_the_resume_guard_names_this_plans_trailers_and_the_format_prints_them(tmp_path):
    root, _, base = _repo(tmp_path)
    clause = _resume_guard_clause(base, PLAN_REL)
    assert "Checkpoint-Plan" in clause and PLAN_REL in clause and "checkpoint(" in clause

    (root / "own.txt").write_text("o", encoding="utf-8")
    _git(root, "add", "--", "own.txt")
    _git(root, "commit", "-q", "-m", "mechanism: hand landing", "-m", f"Plan: {PLAN_REL}")
    (root / "foreign.txt").write_text("f", encoding="utf-8")
    _git(root, "add", "--", "foreign.txt")
    _git(root, "commit", "-q", "-m", "revert something", "-m", "Plan: docs/plans/other.md")

    fmt = re.search(r"git log (--format=\S+)", clause).group(1)
    lines = [ln for ln in _git(root, "log", fmt, f"{base}..HEAD").splitlines() if ln.strip()]
    own = next(ln for ln in lines if "hand landing" in ln)
    foreign = next(ln for ln in lines if "revert something" in ln)
    assert own.split("\t")[1] == PLAN_REL
    assert PLAN_REL not in foreign
