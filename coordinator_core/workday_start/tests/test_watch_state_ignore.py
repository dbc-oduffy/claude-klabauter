"""watch_state_ignore: the four S4 cases, tracked-beats-ignore, and the spawn bound."""
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workday_start import watch_state_ignore as mod
from coordinator_core.workday_start.watch_state_ignore import WATCH_STATE_FILES, watch_state_ignore

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ALL_LITERAL = "\n".join(WATCH_STATE_FILES) + "\n"


def _git(root, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr


def _repo(tmp_path, gitignore, tracked=()):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    (tmp_path / "state").mkdir()
    for rel in tracked:
        (tmp_path / rel).write_text("{}", encoding="utf-8")
    if tracked:
        _git(tmp_path, "add", "-f", *tracked)
    return tmp_path


@pytest.fixture
def no_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_confirm_ignored", lambda r, p: calls.append(p) or set())
    return calls


@pytest.mark.parametrize("gitignore", [ALL_LITERAL, "state/group-em-watch*\nstate/housekeeping-liveness.json\nstate/engine-provenance-counts.jsonl\n", "state/\n"])
def test_ignored_and_untracked_is_silent_with_zero_spawns(tmp_path, gitignore, no_spawn):
    assert watch_state_ignore(_repo(tmp_path, gitignore)) == (0, "")
    assert no_spawn == []


def test_tracked_and_ignored_reports_tracked(tmp_path, no_spawn):
    f = WATCH_STATE_FILES[0]
    _, text = watch_state_ignore(_repo(tmp_path, ALL_LITERAL, tracked=[f]))
    assert text == f"{f}: tracked (fix: git rm --cached {f})"


def test_not_ignored_reports_after_one_batched_confirm(tmp_path, monkeypatch):
    calls = []
    real = mod._confirm_ignored
    monkeypatch.setattr(mod, "_confirm_ignored", lambda r, p: calls.append(p) or real(r, p))
    root = _repo(tmp_path, WATCH_STATE_FILES[0] + "\n")
    _, text = watch_state_ignore(root)
    assert text.splitlines() == [f"{p}: not ignored (fix: add to .gitignore)" for p in WATCH_STATE_FILES[1:]]
    assert len(calls) == 1 and calls[0] == list(WATCH_STATE_FILES[1:])


def test_plain_read_miss_rescued_by_check_ignore(tmp_path):
    root = _repo(tmp_path, "")
    (root / ".git" / "info" / "exclude").write_text("state/*\n", encoding="utf-8")
    assert watch_state_ignore(root) == (0, "")


def test_negation_last_match_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_confirm_ignored", lambda r, p: set())
    root = _repo(tmp_path, "state/*\n!state/group-em-watch.json\n")
    _, text = watch_state_ignore(root)
    assert text == f"{WATCH_STATE_FILES[0]}: not ignored (fix: add to .gitignore)"


def test_absent_index_is_silent_when_ignored(tmp_path, no_spawn):
    (tmp_path / ".gitignore").write_text(ALL_LITERAL, encoding="utf-8")
    assert watch_state_ignore(tmp_path) == (0, "")


def test_unreadable_index_reports_once(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "index").write_bytes(b"garbage")
    code, text = watch_state_ignore(tmp_path)
    assert code == 0 and text.count("\n") == 0 and "could not be determined" in text


def test_report_only_leaves_gitignore_and_index_untouched(tmp_path, no_spawn):
    root = _repo(tmp_path, "", tracked=[WATCH_STATE_FILES[0]])
    before = ((root / ".gitignore").read_bytes(), (root / ".git" / "index").read_bytes())
    watch_state_ignore(root)
    assert before == ((root / ".gitignore").read_bytes(), (root / ".git" / "index").read_bytes())


_DOE_TEMPLATE = Path("E:/coordinator-content-repo/coordinator/skills/repo-setup/templates/gitignore.project.template")


def test_the_covered_set_includes_the_templates_runtime_state_groups():
    """The two template groups `ceremony and coverage transients` and `group-EM watch state` are covered."""
    assert {"state/housekeeping-liveness.json", "state/engine-provenance-counts.jsonl"} <= set(WATCH_STATE_FILES)
    if not _DOE_TEMPLATE.is_file():
        return
    wanted: list[str] = []
    group = False
    for ln in _DOE_TEMPLATE.read_text(encoding="utf-8").splitlines():
        if ln.startswith("#"):
            group = "ceremony and coverage transients" in ln or "group-EM watch state" in ln
        elif ln.strip() and group:
            wanted.append(ln.strip())
    assert wanted and set(wanted) <= set(WATCH_STATE_FILES)
