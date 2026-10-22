"""watch_state_ignore: template-derived set, tracked-beats-ignore, memo-outbox exemption, template-missing line."""
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workday_start import runtime_ignore_set as rset
from coordinator_core.workday_start.watch_state_ignore import watch_state_ignore

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RULES = ("state/group-em-watch.json", ".coordinator-local/", "state/**/*.fire.mjs")


def _git(root, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr


@pytest.fixture
def template(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("tpl") / "gitignore.project.template"
    path.write_text("# managed\n" + "\n".join(RULES) + "\n", encoding="utf-8")
    monkeypatch.setattr(rset, "resolve_template_path", lambda: path)
    return path


def _repo(tmp_path, gitignore, tracked=()):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    for rel in tracked:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    if tracked:
        _git(tmp_path, "add", "-f", *tracked)
    return tmp_path


def test_covered_and_untracked_is_silent(tmp_path, template):
    root = _repo(tmp_path, "\n".join(RULES) + "\n")
    assert watch_state_ignore(root) == (0, "")


def test_every_uncovered_rule_reported_not_ignored(tmp_path, template):
    _, text = watch_state_ignore(_repo(tmp_path, ""))
    assert text.splitlines() == [f"{r}: not ignored (fix: add to .gitignore)" for r in RULES]


def test_tracked_matches_reported_and_memo_outbox_exempt(tmp_path, template):
    files = [
        "state/group-em-watch.json",
        "state/a/b/x.fire.mjs",
        ".coordinator-local/plan-sidecars/p.md",
        ".coordinator-local/memo-outbox/sent/x.md",
    ]
    root = _repo(tmp_path, "\n".join(RULES) + "\n", tracked=files)
    _, text = watch_state_ignore(root)
    assert sorted(text.splitlines()) == sorted(
        f"{p}: tracked (fix: git rm --cached {p})" for p in files[:3]
    )


def test_negation_form_counts_as_covered(tmp_path, template):
    root = _repo(tmp_path, ".coordinator-local/*\n!.coordinator-local/memo-outbox/\n"
                 "state/group-em-watch.json\nstate/**/*.fire.mjs\n")
    assert watch_state_ignore(root) == (0, "")


def test_template_not_found_is_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(rset, "resolve_template_path", lambda: None)
    code, text = watch_state_ignore(_repo(tmp_path, ""))
    assert code == 0 and text.startswith("runtime-ignore: template not found (") and "\n" not in text


def test_unreadable_index_reports_once(tmp_path, template):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "index").write_bytes(b"garbage")
    code, text = watch_state_ignore(tmp_path)
    assert code == 0 and text.count("\n") == 0 and "could not be determined" in text


def test_report_only_leaves_gitignore_and_index_untouched(tmp_path, template):
    root = _repo(tmp_path, "", tracked=["state/group-em-watch.json"])
    before = ((root / ".gitignore").read_bytes(), (root / ".git" / "index").read_bytes())
    watch_state_ignore(root)
    assert before == ((root / ".gitignore").read_bytes(), (root / ".git" / "index").read_bytes())
