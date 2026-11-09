"""emit's scoped-test and row-verify prompts carry the target repo's pinned interpreter."""
from __future__ import annotations

import json
import sys

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit import test_interpreter as ti


def _repo(tmp_path, **keys):
    body = "".join(f'{k}: "{v}"\n' for k, v in keys.items())
    (tmp_path / "coordinator.local.md").write_text(f"---\n{body}---\n", encoding="utf-8")
    return tmp_path


def _assert_pinned(text):
    assert sys.executable in text
    assert "falling back" not in text


def test_runner_clause_carries_the_pin(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=sys.executable)
    _assert_pinned(emit._test_runner_clause(repo))


def test_runner_clause_without_repo_root_keeps_portable_text():
    assert emit._test_runner_clause().endswith(f"{ti.PORTABLE_CLAUSE} {ti.LITERAL_PATHS_CLAUSE}")


def test_runner_clause_without_pin_keeps_portable_text(tmp_path):
    repo = _repo(tmp_path, fast_test_cmd="python3 -m pytest")
    assert emit._test_runner_clause(repo).endswith(f"{ti.PORTABLE_CLAUSE} {ti.LITERAL_PATHS_CLAUSE}")


def test_unresolvable_pin_names_the_defect(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=str(tmp_path / "no-such-python"))
    assert ti.DEFECT_PREFIX in emit._test_runner_clause(repo)


def test_scoped_test_steps_carry_the_pin(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=sys.executable)
    text = emit._scoped_test_steps(["tests/test_x.py"], repo, None, None, None, None, None)
    _assert_pinned(text)


def test_row_verify_literal_carries_the_pin(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=sys.executable)
    helper = emit._run_row_helper_js(repo_root=repo)
    assert sys.executable.replace("\\", "\\\\") in helper or sys.executable in helper
    assert "falling back" not in helper


def _seam_js(repo_root):
    seam = emit.SeamLeg((), {}, (), {}, ())
    js = "\n".join(emit._seam_leg_js(
        seam, None, plan_path=None, run_base_sha="a" * 40, js=emit._js_string_literal,
        head="", repo_flag="", sidecar_js="''", repo_root=repo_root,
    ))
    return js.split("Step 2, only when tests are listed", 1)[1].split("Run no other command", 1)[0]


def test_seam_leg_test_step_carries_the_pin(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=sys.executable)
    js = _seam_js(repo)
    assert json.dumps(sys.executable)[1:-1] in js
    assert "python -m pytest" not in js


def test_seam_leg_test_step_without_root_is_portable():
    js = _seam_js(None)
    assert ti.PORTABLE_CLAUSE in js
    assert "python -m pytest" not in js


def test_row_verify_literal_without_root_is_portable():
    assert "falling back" in emit._run_row_helper_js()
