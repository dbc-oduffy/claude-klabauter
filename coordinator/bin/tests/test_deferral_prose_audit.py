"""Tests for deferral-prose-audit.py: population, out-of-spine rule, seeding, idempotency, exit codes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deferral-prose-audit.py"
_spec = importlib.util.spec_from_file_location("deferral_prose_audit", _SCRIPT)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

PLAN = """---
title: t
status: implemented
plan_id: "pln-x-1"
note: deferred in frontmatter
---

# T

Prose says this is deferred to a follow-up.

## Tasks

```yaml plan-tasks
- id: C1
  body: deferred later
```

- deferred in spine

## Distill pass

follow-up in distill pass

## Notes

```
later in a fence
```
Already routed: later, see state/improvement-queue/x.yaml
"""


def _write(root: Path, name: str, text: str) -> Path:
    p = root / "docs" / "plans" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


class Recorder:
    def __init__(self, rc: int = 0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv):
        self.calls.append(argv)
        return self.rc


def run(root, *extra, runner=None, launcher="launcher"):
    return audit.main(["--root", str(root), *extra], runner=runner, launcher=launcher)


def test_hit_in_prose_listed_and_spine_regions_skipped(tmp_path, capsys):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    assert run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "docs/plans/2026-08-01-a.md:10: Prose says this is deferred to a follow-up." in out
    assert "1 hit(s) in 1 plan(s); seeded 0, already-seeded 0" in out


def test_clean_plan_exits_0(tmp_path):
    _write(tmp_path, "2026-08-01-a.md", PLAN.replace("deferred to a follow-up", "done"))
    assert run(tmp_path) == 0


def test_since_excludes_older_plan(tmp_path):
    _write(tmp_path, "2026-06-01-old.md", PLAN)
    assert run(tmp_path) == 0
    assert run(tmp_path, "--since", "2026-01-01") == 1


def test_non_implemented_plan_excluded(tmp_path):
    _write(tmp_path, "2026-08-01-a.md", PLAN.replace("status: implemented", "status: executing"))
    assert run(tmp_path) == 0


def test_plan_flag_bypasses_status_and_since(tmp_path):
    p = _write(tmp_path, "2026-01-01-a.md", PLAN.replace("status: implemented", "status: executing"))
    assert run(tmp_path, "--plan", str(p)) == 1


def test_missing_plan_exits_2(tmp_path):
    assert run(tmp_path, "--plan", str(tmp_path / "nope.md")) == 2


def test_crlf_matches_lf(tmp_path, capsys):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    run(tmp_path)
    lf = capsys.readouterr().out
    _write(tmp_path, "2026-08-01-a.md", PLAN.replace("\n", "\r\n"))
    run(tmp_path)
    assert capsys.readouterr().out == lf


def test_seed_one_call_per_plan_with_key_and_path(tmp_path):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    r = Recorder()
    assert run(tmp_path, "--seed", runner=r) == 0
    assert len(r.calls) == 1
    argv = r.calls[0]
    assert argv[argv.index("--evidence") + 1] == "deferral-prose-key: pln-x-1; plan: docs/plans/2026-08-01-a.md"
    assert argv[argv.index("--status") + 1] == "open"
    assert "--pm-approved" not in argv
    assert not list((tmp_path / "scratch" / "deferral-prose-audit").glob("*"))


def test_second_seed_makes_no_call(tmp_path):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    r = Recorder()
    run(tmp_path, "--seed", runner=r)
    q = tmp_path / "state" / "improvement-queue"
    q.mkdir(parents=True)
    (q / "e.yaml").write_text("evidence: deferral-prose-key: pln-x-1; plan: docs/plans/2026-08-01-a.md\n")
    r2 = Recorder()
    assert run(tmp_path, "--seed", runner=r2) == 0
    assert r2.calls == []
    assert run(tmp_path) == 0


def test_key_is_not_a_prefix_match(tmp_path):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    q = tmp_path / "archive" / "improvement-queue" / "2026-09"
    q.mkdir(parents=True)
    (q / "e.yaml").write_text("evidence: deferral-prose-key: pln-x-10; plan: z\n")
    r = Recorder()
    run(tmp_path, "--seed", runner=r)
    assert len(r.calls) == 1


def test_plan_without_plan_id_keys_on_path_and_reseed_is_noop(tmp_path):
    _write(tmp_path, "2026-08-01-b.md", PLAN.replace('plan_id: "pln-x-1"\n', ""))
    r = Recorder()
    run(tmp_path, "--seed", runner=r)
    ev = r.calls[0][r.calls[0].index("--evidence") + 1]
    assert ev == "deferral-prose-key: docs/plans/2026-08-01-b.md; plan: docs/plans/2026-08-01-b.md"
    q = tmp_path / "state" / "improvement-queue"
    q.mkdir(parents=True)
    (q / "e.yaml").write_text(ev + "\n")
    r2 = Recorder()
    assert run(tmp_path, "--seed", runner=r2) == 0
    assert r2.calls == []


def test_failing_runner_exits_2(tmp_path):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    assert run(tmp_path, "--seed", runner=Recorder(rc=1)) == 2


def test_seed_with_launcher_absent_exits_2(tmp_path, monkeypatch):
    _write(tmp_path, "2026-08-01-a.md", PLAN)
    monkeypatch.setattr(audit, "resolve_launcher", lambda: None)
    assert audit.main(["--root", str(tmp_path), "--seed"], runner=Recorder()) == 2


def test_launcher_found_in_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    exe = home / "bin" / "coordinator-queue-append"
    exe.write_text("")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    assert audit.resolve_launcher() == str(exe)
    exe.unlink()
    assert audit.resolve_launcher() is None


@pytest.mark.parametrize("heading,skipped", [("## Tasks", True), ("## Tasks (spine)", True),
                                             ("## Distill pass", True), ("## Tasks and follow-ups", False),
                                             ("## Taskforce", False)])
def test_section_skip_is_not_a_prefix_match(heading, skipped):
    hits = audit.find_hits(f"{heading}\nlater item\n")
    assert (not hits) is skipped


def test_tilde_fence_skipped():
    assert not audit.find_hits("~~~\nlater in a tilde fence\n~~~\n")


def test_fence_of_other_kind_inside_fence_does_not_close_it():
    assert not audit.find_hits("~~~\n```\nlater inside nested fence\n~~~\n")
    assert audit.find_hits("~~~\n```\n~~~\nlater after the fence\n")


@pytest.mark.parametrize("word", ["deferred", "Follow-up", "followups", "follow up", "later"])
def test_keyword_families(word):
    assert audit.KEYWORDS.search(f"x {word} y")


@pytest.mark.parametrize("line", ["a delayed plan", "alternately", "preserve this row"])
def test_non_keywords(line):
    assert not audit.KEYWORDS.search(line)
