"""runtime_ignore_set: template-rule loading, missing/tracked findings, exemption, scale."""
import subprocess
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workday_start import runtime_ignore_set as mod
from coordinator_core.workday_start.runtime_ignore_set import (
    UNTRACK_EXEMPT_PREFIXES,
    load_template_rules,
    resolve_template_path,
    runtime_ignore_findings,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

TEMPLATE = """# managed block
.coordinator-local/
state/live.json
/.approved

# group
state/**/fire-*.mjs
scratch/
"""
RULES = (".coordinator-local/", "state/live.json", "/.approved", "state/**/fire-*.mjs", "scratch/")


def _git(root, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr


def _repo(tmp_path, gitignore, tracked=()):
    _git(tmp_path, "init", "-q")
    if gitignore is not None:
        (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    for rel in tracked:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")
    if tracked:
        _git(tmp_path, "add", "-f", *tracked)
    return tmp_path


def test_load_template_rules_skips_blank_and_comments(tmp_path):
    t = tmp_path / "t"
    t.write_text(TEMPLATE, encoding="utf-8")
    assert load_template_rules(t) == RULES


def test_resolve_template_path_none_when_root_unresolved(monkeypatch):
    import coordinator_core.subagent_sandbox.provision_report as pr

    monkeypatch.setattr(pr, "resolve_plugin_root", lambda: None)
    assert resolve_template_path() is None


def test_resolve_template_path_none_when_file_absent(monkeypatch, tmp_path):
    import coordinator_core.subagent_sandbox.provision_report as pr

    monkeypatch.setattr(pr, "resolve_plugin_root", lambda: str(tmp_path))
    assert resolve_template_path() is None
    target = tmp_path / mod.TEMPLATE_REL
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")
    assert resolve_template_path() == target


def test_missing_reports_each_uncovered_rule(tmp_path):
    repo = _repo(tmp_path, "scratch/\n")
    got = runtime_ignore_findings(repo, RULES)
    assert got.missing == (".coordinator-local/", "state/live.json", "/.approved", "state/**/fire-*.mjs")
    assert got.tracked == ()


@pytest.mark.parametrize("rule", ["logs/run[0-9].log", "a/[abc]-*.tmp/", "b/[!x]z", "c/[]]q"])
def test_probe_for_character_class_is_matched_by_its_own_rule(rule):
    probe = mod._probe(rule)
    assert "[" not in probe
    assert mod._covered(mod._parse_rules(rule), probe)


def test_character_class_rule_is_covered_not_missing(tmp_path):
    rule = "logs/run[0-9].log"
    repo = _repo(tmp_path, rule + "\n")
    assert runtime_ignore_findings(repo, (rule,)).missing == ()


def test_negation_form_covers_directory_rule(tmp_path):
    ign = ".coordinator-local/*\n!.coordinator-local/memo-outbox/\nstate/live.json\n/.approved\nstate/**/fire-*.mjs\nscratch/\n"
    repo = _repo(tmp_path, ign)
    assert runtime_ignore_findings(repo, RULES).missing == ()


def test_tracked_matches_literal_dir_glob_and_anchored(tmp_path):
    ign = ".coordinator-local/\nstate/live.json\n/.approved\nstate/**/fire-*.mjs\nscratch/\n"
    files = [
        "state/live.json", ".approved", "state/a/b/fire-x.mjs", "scratch/n.txt",
        "sub/scratch/n.txt", ".coordinator-local/dispatch-briefs/x.md", "sub/.approved", "state/keep.json",
    ]
    repo = _repo(tmp_path, ign, files)
    got = runtime_ignore_findings(repo, RULES)
    assert got.missing == ()
    assert got.tracked == (
        ".approved", ".coordinator-local/dispatch-briefs/x.md", "scratch/n.txt",
        "state/a/b/fire-x.mjs", "state/live.json", "sub/scratch/n.txt",
    )


def test_memo_outbox_is_exempt_and_other_paths_are_not(tmp_path):
    ign = ".coordinator-local/*\n!.coordinator-local/memo-outbox/\n"
    repo = _repo(tmp_path, ign, [".coordinator-local/memo-outbox/sent/x.md", ".coordinator-local/plan-sidecars/p.md"])
    got = runtime_ignore_findings(repo, (".coordinator-local/",))
    assert got.missing == ()
    assert got.tracked == (".coordinator-local/plan-sidecars/p.md",)
    assert UNTRACK_EXEMPT_PREFIXES == (".coordinator-local/memo-outbox/",)


def test_exempt_prefix_dropped_even_when_rule_is_missing(tmp_path):
    repo = _repo(tmp_path, "", [".coordinator-local/memo-outbox/sent/x.md"])
    got = runtime_ignore_findings(repo, (".coordinator-local/",))
    assert got.missing == (".coordinator-local/",)
    assert got.tracked == ()


def test_repo_negation_reincluded_path_outside_memo_outbox_not_reported(tmp_path):
    ign = ".coordinator-local/*\n!.coordinator-local/keep/\n"
    repo = _repo(tmp_path, ign, [".coordinator-local/keep/a.md", ".coordinator-local/other/b.md"])
    got = runtime_ignore_findings(repo, (".coordinator-local/",))
    assert got.missing == ()
    assert got.tracked == (".coordinator-local/other/b.md",)


def test_anchored_literal_does_not_match_nested_copy(tmp_path):
    repo = _repo(tmp_path, "/.approved\n", ["sub/.approved"])
    assert runtime_ignore_findings(repo, ("/.approved",)).tracked == ()


def test_findings_over_50k_paths_stays_under_200ms_process_time(tmp_path, monkeypatch):
    (tmp_path / ".gitignore").write_text(
        ".coordinator-local/\nstate/live.json\n/.approved\nstate/**/fire-*.mjs\nscratch/\n", encoding="utf-8"
    )
    index = {f"src/pkg{i % 200}/mod{i}.py" for i in range(48_000)}
    index |= {f"state/dir{i % 50}/rec{i}.yaml" for i in range(1_900)}
    index |= {f"state/dir{i}/fire-{i}.mjs" for i in range(40)}
    index |= {f".coordinator-local/memo-outbox/sent/{i}.md" for i in range(60)}
    monkeypatch.setattr(mod, "read_index_paths", lambda root: frozenset(index))
    t0 = time.process_time()
    got = runtime_ignore_findings(tmp_path, RULES)
    elapsed = time.process_time() - t0
    assert got.missing == ()
    assert len(got.tracked) == 40
    assert elapsed < 0.2, elapsed
