"""`coordinator_core.ops.dispatch_emit.dirty_write_set`.

Pins the emit-time refusal over uncommitted work in the wave's union write set.
Every unit case injects a fake `run`; only the CLI-wiring cases spawn real git.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import dirty_write_set as tool
from coordinator_core.session.record_homes import record_path


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0, exc: Exception | None = None):
        self.stdout, self.returncode, self.exc = stdout, returncode, exc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(stdout=self.stdout, returncode=self.returncode, stderr="")


def test_modified_path_is_reported(tmp_path) -> None:
    run = FakeRun(" M a.py\n")
    assert tool._dirty_in_write_set(["a.py", "b.py"], tmp_path, run=run) == ["a.py"]


def test_rename_reports_destination(tmp_path) -> None:
    run = FakeRun("R  a.py -> b.py\n")
    assert tool._dirty_in_write_set(["a.py", "b.py"], tmp_path, run=run) == ["b.py"]


def test_untracked_is_reported(tmp_path) -> None:
    run = FakeRun("?? new.py\n")
    assert tool._dirty_in_write_set(["new.py"], tmp_path, run=run) == ["new.py"]


def test_clean_output_passes(tmp_path) -> None:
    assert tool._dirty_in_write_set(["a.py"], tmp_path, run=FakeRun("")) == []


def test_empty_union_never_calls_run(tmp_path) -> None:
    run = FakeRun(" M a.py\n")
    assert tool._dirty_in_write_set([], tmp_path, run=run) == []
    assert run.calls == []


def test_nonzero_exit_and_oserror_return_none(tmp_path) -> None:
    assert tool._dirty_in_write_set(["a.py"], tmp_path, run=FakeRun(returncode=128)) is None
    assert tool._dirty_in_write_set(["a.py"], tmp_path, run=FakeRun(exc=OSError("x"))) is None


def test_argv_is_one_scoped_spawn_with_sorted_union(tmp_path) -> None:
    run = FakeRun("")
    tool._dirty_in_write_set(["z.py", "a.py", "z.py"], tmp_path, run=run, ignorecase=False)
    assert len(run.calls) == 1
    argv = run.calls[0]
    assert "--no-optional-locks" in argv
    assert "--untracked-files=all" in argv
    assert "--porcelain" in argv and "status" in argv
    assert argv[argv.index("--") + 1 :] == ["a.py", "z.py"]


def test_path_outside_the_union_does_not_count(tmp_path) -> None:
    """Real scoped git never returns an out-of-set path; this proves the
    intersect filter, not the prime criterion's 'outside dirt never refuses'."""
    run = FakeRun(" M peer.py\n")
    assert tool._dirty_in_write_set(["a.py"], tmp_path, run=run) == []


def test_run_omitted_reaches_the_shared_git_runner(tmp_path, monkeypatch) -> None:
    fake = FakeRun(" M a.py\n")
    monkeypatch.setattr(tool, "run_git", fake)
    assert tool._dirty_in_write_set(["a.py"], tmp_path) == ["a.py"]
    assert len(fake.calls) == 1


# --- plan-level: union derivation and refusal ---------------------------------


def _plan(tmp_path: Path, rows_yaml: str) -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text(
        "---\ntitle: fixture\n---\n\n# Fixture\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{rows_yaml}\n```\n",
        encoding="utf-8",
    )
    return plan


def _guard(plan: Path, tmp_path: Path, run, ignorecase=False):
    return tool.guard_against_dirty_write_set(plan, tmp_path, run=run, ignorecase=ignorecase)


ROWS_OVERLAP = """\
- id: C1
  title: one
  change_kind: code-edit
  surface: src/a.py
  writes:
    - src/a.py
    - src/shared.py
  disposition: open
  body: |
    do a
- id: C2
  title: two
  change_kind: code-edit
  surface: src/b.py
  writes:
    - src/shared.py
    - src/b.py
  disposition: open
  body: |
    do b
"""


def test_union_is_deduplicated_and_one_call(tmp_path) -> None:
    run = FakeRun("")
    _guard(_plan(tmp_path, ROWS_OVERLAP), tmp_path, run)
    assert len(run.calls) == 1
    argv = run.calls[0]
    assert argv[argv.index("--") + 1 :] == ["src/a.py", "src/b.py", "src/shared.py"]


def test_dirty_path_refuses_naming_it(tmp_path) -> None:
    run = FakeRun(" M src/shared.py\n?? src/b.py\n")
    with pytest.raises(tool.DirtyWriteSetError) as info:
        _guard(_plan(tmp_path, ROWS_OVERLAP), tmp_path, run)
    assert "src/shared.py" in str(info.value) and "src/b.py" in str(info.value)


def test_could_not_run_refuses_fail_closed(tmp_path) -> None:
    with pytest.raises(tool.DirtyWriteSetError, match="could not run"):
        _guard(_plan(tmp_path, ROWS_OVERLAP), tmp_path, FakeRun(returncode=1))
    with pytest.raises(tool.DirtyWriteSetError, match="could not run"):
        _guard(_plan(tmp_path, ROWS_OVERLAP), tmp_path, FakeRun(exc=OSError("x")))


def test_clean_union_passes(tmp_path) -> None:
    _guard(_plan(tmp_path, ROWS_OVERLAP), tmp_path, FakeRun(""))


ROWS_FALLBACK = """\
- id: C1
  title: surface only
  change_kind: code-edit
  surface: src/only.py
  disposition: open
  body: |
    do it
"""


def test_surface_fallback_is_in_the_union(tmp_path) -> None:
    with pytest.raises(tool.DirtyWriteSetError, match="src/only.py"):
        _guard(_plan(tmp_path, ROWS_FALLBACK), tmp_path, FakeRun(" M src/only.py\n"))


ROWS_PREFIX = """\
- id: C1
  title: prefix
  change_kind: doc-edit
  surface: state/audits/
  writes: []
  writes_under:
    - state/audits/
  disposition: open
  body: |
    write a dated audit
"""


def test_writes_under_prefix_is_in_the_union(tmp_path) -> None:
    with pytest.raises(tool.DirtyWriteSetError, match=re.escape(Path(record_path(".", "audits", "x.md")).as_posix())):
        _guard(_plan(tmp_path, ROWS_PREFIX), tmp_path, FakeRun("?? state/audits/x.md\n"))


ROWS_WITHHELD = """\
- id: C1
  title: live
  change_kind: code-edit
  surface: src/live.py
  writes:
    - src/live.py
  disposition: open
  body: |
    live
- id: C2
  title: backlogged
  change_kind: code-edit
  surface: src/held.py
  writes:
    - src/held.py
  disposition: backlogged
  disposition_ref: state/x.yaml
  body: |
    held
"""


def test_non_dispatchable_row_path_is_outside_the_union(tmp_path) -> None:
    run = FakeRun(" M src/held.py\n")
    _guard(_plan(tmp_path, ROWS_WITHHELD), tmp_path, run)
    argv = run.calls[0]
    assert argv[argv.index("--") + 1 :] == ["src/live.py"]


# --- example-game-repo shape: repo-relative declared path, git prints forward slashes --


def test_backslash_declared_path_matches_forward_slash_git_output(tmp_path) -> None:
    run = FakeRun(" M Source/Mod/Private/Create.cpp\n")
    got = tool._dirty_in_write_set([r"Source\Mod\Private\Create.cpp"], tmp_path, run=run, ignorecase=False)
    assert got == ["Source/Mod/Private/Create.cpp"]
    argv = run.calls[0]
    assert argv[argv.index("--") + 1 :] == ["Source/Mod/Private/Create.cpp"]


def test_case_differing_declared_path_matches_when_ignorecase(tmp_path) -> None:
    run = FakeRun(" M Source/Mod/Private/Create.cpp\n")
    got = tool._dirty_in_write_set(["source/mod/private/create.cpp"], tmp_path, run=run, ignorecase=True)
    assert got == ["Source/Mod/Private/Create.cpp"]


def test_ignorecase_scopes_git_with_icase_literal_pathspecs(tmp_path) -> None:
    run = FakeRun("")
    tool._dirty_in_write_set(["a.py"], tmp_path, run=run, ignorecase=True)
    argv = run.calls[0]
    assert argv[argv.index("--") + 1 :] == [":(icase,literal)a.py"]


def test_case_differing_declared_path_does_not_match_when_case_sensitive(tmp_path) -> None:
    run = FakeRun(" M Source/Mod/Private/Create.cpp\n")
    assert tool._dirty_in_write_set(["source/mod/private/create.cpp"], tmp_path, run=run, ignorecase=False) == []


def test_directory_prefix_with_backslashes_and_case(tmp_path) -> None:
    run = FakeRun("?? Source/Mod/New.cpp\n")
    got = tool._dirty_in_write_set(["SOURCE\\Mod\\"], tmp_path, run=run, ignorecase=True)
    assert got == ["Source/Mod/New.cpp"]


# --- generated_outputs: regenerable dirt does not refuse ----------------------

ROWS_GENERATED = """\
- id: C1
  title: gen
  change_kind: code-edit
  surface: data/serving/
  writes:
    - src/a.py
  writes_under:
    - data/serving/
  disposition: open
  body: |
    regenerate
"""


def _declare(tmp_path: Path, value: str) -> None:
    (tmp_path / "coordinator.local.md").write_text(
        f"---\ngenerated_outputs: {value}\n---\n", encoding="utf-8"
    )


def test_declared_glob_lets_emit_proceed_and_lists_paths(tmp_path) -> None:
    _declare(tmp_path, '"data/serving/**, public/exports/**"')
    status = "".join(f"?? data/serving/s/{i}.json\n" for i in range(12))
    out = _guard(_plan(tmp_path, ROWS_GENERATED), tmp_path, FakeRun(status))
    assert out["count"] == 12
    assert len(out["examples"]) == 5
    assert out["note"] == tool.REGENERABLE_NOTE


def test_non_matching_dirty_path_still_refuses(tmp_path) -> None:
    _declare(tmp_path, "[data/serving/**]")
    run = FakeRun("?? data/serving/x.json\n M src/a.py\n")
    with pytest.raises(tool.DirtyWriteSetError) as info:
        _guard(_plan(tmp_path, ROWS_GENERATED), tmp_path, run)
    assert "src/a.py" in str(info.value) and "x.json" not in str(info.value)


def test_no_declaration_is_unchanged(tmp_path) -> None:
    with pytest.raises(tool.DirtyWriteSetError, match="data/serving/x.json"):
        _guard(_plan(tmp_path, ROWS_GENERATED), tmp_path, FakeRun("?? data/serving/x.json\n"))
    assert _guard(_plan(tmp_path, ROWS_GENERATED), tmp_path, FakeRun("")) is None
