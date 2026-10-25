"""warp_run_deliverable.halted_run_write_claims against a real git repo; split out so only the spawning tests leave the fast tier."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.test_warp_run_deliverable_halted_claims import HALTED_ID, _claims, _record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git_repo_with_two_withheld(tmp_path, b_head):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*args, stdin=""):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, input=stdin,
            capture_output=True, text=True, check=True, **no_console_creationflags(),
        ).stdout.strip()

    git("init", "-q")
    for name, text in (("a.py", "a = 1\n"), ("b.py", "b = 1\n")):
        (tmp_path / name).write_text(text, encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    (tmp_path / "a.py").write_text("a = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(b_head, encoding="utf-8")
    git("commit", "-q", "-am", "head")
    rec = _record(tmp_path, HALTED_ID, writes=["a.py", "b.py"], withheld=["a.py", "b.py"])
    kept = {p: git("hash-object", "-w", "--stdin", stdin="%s = 2\n" % p[0]) for p in ("a.py", "b.py")}
    (rec / "kept.json").write_text(json.dumps({"chunks": {"X1": {"paths": kept}}, "plan_gate": None}), encoding="utf-8")


def test_withheld_path_kept_at_head_is_released_per_path(tmp_path):
    _git_repo_with_two_withheld(tmp_path, "b = 3\n")
    assert _claims(tmp_path, ["a.py", "b.py"]) == {"b.py": [HALTED_ID]}


def test_withheld_path_released_is_not_reclaimed_through_hunk_attribution(tmp_path):
    _git_repo_with_two_withheld(tmp_path, "b = 3\n")
    seen = []

    def attributable(run_id, paths):
        seen.append(set(paths))
        return set(paths)

    got = _claims(tmp_path, ["a.py", "b.py"], dirty=lambda: {"a.py", "b.py"}, attributable=attributable)
    assert got == {"b.py": [HALTED_ID]}
    assert all("a.py" not in s for s in seen)


def test_all_withheld_paths_at_head_hold_nothing(tmp_path):
    _git_repo_with_two_withheld(tmp_path, "b = 2\n")
    assert _claims(tmp_path, ["a.py", "b.py"]) == {}
