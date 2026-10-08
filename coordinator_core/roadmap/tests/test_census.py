"""coordinator_core.roadmap.census -- the shared census screen and the structured-count compare.

The screen is the one function both the prep gate and the fire-time revalidator ask, so these
pin the two failure directions it has had: refusing a plain read (the corpus's dominant idioms
must pass), and passing a write that hides inside a read (each hole below ran its second command).
"""

from __future__ import annotations

import pytest

from coordinator_core.roadmap import census, prep_gate


@pytest.mark.parametrize(
    "command",
    [
        r"grep -rn 'a\|b' path | wc -l",
        "grep -c x f 2>/dev/null",
        "ls a; ls b | wc -l",
        "git -C ../x log --oneline | head -3",
        "cd ../x && grep -c y z",
        "grep x f 2>&1 | head",
        "grep '; rm' f",
        "awk '/a/' f |\n  grep -c b",
        "grep -c x f\ngrep -c y f",
        "grep 'a\nb' f",
        "grep x \\\n  f",
    ],
)
def test_a_read_clears_the_screen(command):
    assert census.screen(command) is None


@pytest.mark.parametrize(
    "command",
    [
        # Each of these screened as a lone read under `shlex.split` and ran the write.
        "grep x\nrm y",
        "grep x;rm y",
        "grep x & rm y",
        "grep x&rm y",
        "grep -c x <placeholder path> f",
        "python3 - <<'EOF'\nprint(1)\nEOF",
        # The forms the 2026-10-07 dogfood found stamped by prep and refused at fire time.
        "python -c 'print(1)'",
        "echo $(rm x)",
        'echo "$(rm x)"',
        "for f in a; do grep x $f; done",
        # And the rest of the refused shapes.
        "grep x > out",
        "grep x>out",
        "grep x | tee o",
        "C=p grep x $C",
        "git push",
        "(rm x)",
        "diff <(ls a) <(ls b)",
        "grep x &>o",
        "cat `rm x`",
    ],
)
def test_a_write_or_unscreenable_form_is_refused(command):
    assert census.screen(command)


@pytest.mark.parametrize(
    "count,stdout,state",
    [
        (12, "12\n", "MATCH"),
        (142, "142 file.py\n", "MATCH"),  # wc -l: one line whose number is the count
        (3, "a\nb\nc\n", "MATCH"),  # grep -n: the count is the line count
        (12, "13\n", "DRIFT"),
        (26, "a\n" * 31, "DRIFT"),
        (0, "", "MATCH"),
    ],
)
def test_a_structured_count_is_always_decidable(count, stdout, state):
    assert census.compare_count(count, stdout)["state"] == state


@pytest.mark.parametrize("value", ["12", -1, True, 1.5, None])
def test_a_count_that_is_not_a_non_negative_int_is_named(value):
    assert census.count_defect({"count": value})


def test_an_absent_count_is_not_a_defect():
    assert census.count_defect({"result": "12 (prose)"}) is None


def _census_class(entries):
    return prep_gate._census({"census": entries})


def test_prep_refuses_a_command_the_revalidator_would_refuse_and_names_the_allowed_form():
    verdict = _census_class(
        [{"question": "q", "command": "python -c 'print(1)'", "result": "1"}]
    )
    assert verdict["status"] == "DEFECT"
    assert verdict["kind"] == "census-unscreenable"
    assert census.ALLOWED_FORM in verdict["detail"]


def test_prep_refuses_a_malformed_count():
    verdict = _census_class(
        [{"question": "q", "command": "grep -c x f", "result": "1", "count": "1"}]
    )
    assert verdict["kind"] == "census-incomplete"
    assert "count" in verdict["detail"]


def test_prep_passes_a_screenable_entry_carrying_a_count():
    verdict = _census_class(
        [{"question": "q", "command": "grep -c x f", "result": "12 (prose)", "count": 12}]
    )
    assert verdict["status"] == "PASS"


@pytest.fixture
def repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tool.py").write_text("print(1)\n")
    (tmp_path / "pkg" / "__main__.py").write_text("print(1)\n")
    (tmp_path / "untracked.py").write_text("print(1)\n")
    subprocess.run(["git", "add", "tool.py", "pkg/__main__.py"], cwd=tmp_path, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return tmp_path


@pytest.mark.parametrize("command", ["python tool.py", "python3 -u ./tool.py | wc -l", "python -m pkg"])
def test_a_tracked_python_target_clears_the_screen(repo, command):
    assert census.screen(command, repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "python untracked.py",
        "python missing.py",
        "python -m json.tool",
        "python -m pkg.nope",
        "python $X/tool.py",
        "python ${X}",
        "python *.py",
        "python tool?.py",
        "python t[o].py",
        "python ../tool.py",
        "python /elsewhere/tool.py",
        "python",
    ],
)
def test_an_untracked_or_dynamic_python_target_is_refused(repo, command):
    assert census.screen(command, repo)


def test_python_without_repo_context_stays_pure_string_screened():
    assert census.screen("python anything.py") is None


def test_prep_resolves_every_python_target_in_one_ls_files_spawn(repo, monkeypatch):
    from coordinator_core.git import run as git_run

    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda args, **k: calls.append(args) or real(args, **k))
    entries = [{"question": "q", "command": "python tool.py", "result": "1"}] * 5 + [
        {"question": "q", "command": "python -m pkg", "result": "1"},
        {"question": "q", "command": "grep -c x f", "result": "1"},
    ]
    assert prep_gate._census({"census": entries}, repo)["status"] == "PASS"
    assert len(calls) == 1
    bad = prep_gate._census(
        {"census": [{"question": "q", "command": "python untracked.py", "result": "1"}]}, repo
    )
    assert bad["kind"] == "census-unscreenable"
    assert census.ALLOWED_FORM in bad["detail"]


@pytest.mark.parametrize(
    "count,recorded,observed,state",
    [
        (None, "count 12; v", "v", "MATCH"),  # prefix is shape, not value
        (7, "multi\nline", "multi\nline", "MATCH"),  # identical output, count counts something else
        (None, "count 12; v", "count 12; v", "MATCH"),
        (None, "count 12; v", "count 13; v", "DRIFT"),  # both sides carry a count and differ
        (None, "count 12; v", "w", None),  # genuinely different value: left to the count compare
    ],
)
def test_same_value_normalises_the_count_prefix(count, recorded, observed, state):
    verdict = census.same_value(count, recorded, observed)
    assert (verdict["state"] if verdict else None) == state
