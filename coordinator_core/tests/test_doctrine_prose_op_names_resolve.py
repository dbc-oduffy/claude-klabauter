"""Fleet doctrine prose never tells a reader to dial an op the live registry lacks.

A kill pass de-registers an op and its code oracles (the commit roster, the budget
suspension table, classification, scopes) follow; the doctrine PROSE that names the
op lives in coordinator-content-repo, outside every one of those oracles. A reader who copies
`coordinator-invoke <op>` from a skill, or who believes a sentence that names a dead
op as the owner of a mechanism, gets a -32006/unknown-op refusal or a wrong model of
the engine. This test scans coordinator-content-repo `coordinator/{skills,commands,docs/wiki}` for
two reference shapes:

- INVOKE: `coordinator-invoke [flags] <op>` where `<op>` is neither registered nor in
  `OP_MODULE_MAP`. Any unknown dotted name is flagged.
- BARE: a dotted token anywhere in prose that is a known-dead op. Dead = named in
  `op_budget_suspension.SUSPENDED_OPS` (tracked, present on every checkout) and not
  live; the strict worklist test also adds every op key in the kill ledger.

A reference whose own neighbourhood narrates the op's death ("killed", "retired",
"K-057", ...) is exempt: it documents the absence rather than relying on the op. The
neighbourhood is a character window around the token, not the whole line, so a
live-tense instruction that merely shares a line with a death word is still flagged.

The DoE checkout comes from `machine-local get engine.working_repos.content_root`;
unregistered means skip.

Spec backlink: docs/plans/2026-10-01-ratchet-and-guard-coverage-gaps.md
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

import pytest

from coordinator_core.machine_resolver import registry_get

_SCAN_DIRS = ("skills", "commands", "docs/wiki")

# `coordinator-invoke <op>`, `coordinator-invoke.exe" <op>` (PowerShell door), with
# optional `--flag value`-less switches. The op is a dotted lowercase name.
_CALL = re.compile(
    r"coordinator-invoke(?:\.exe)?[\"'`]?\s+(?:--[\w-]+\s+)*"
    r"([a-z][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+)(?![\w./-])"
)

# A dotted token in running prose. Excludes path/URL fragments (`a/b.c`, `x-y.z`) and
# a trailing `.ext` continuation, so `foo.bar.md` is not read as `foo.bar`.
_BARE = re.compile(
    r"(?<![\w./-])([a-z][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+)(?![\w/-]|\.\w)"
)

# Narration of an op's absence. Checked inside `_NARRATION_WINDOW` characters either
# side of the token, never across the whole line.
_DEAD_NARRATION = re.compile(
    r"\b(?:dead|killed|kill|de-?registered|retired|removed|deleted|gravestone[d]?|"
    r"unregistered|superseded|frozen|archaeology|no longer|unknown op|K-\d+)\b|-32006",
    re.IGNORECASE,
)
_NARRATION_WINDOW = 100

# The last segment of a ledger heading key that names a file, not an op.
_FILE_SUFFIXES = frozenset({"py", "md", "json", "yaml", "yml", "sh", "mjs", "js", "cmd"})

# Un-narrated bare-token references to a suspension-table op that doc prose carries
# today, as {(page under coordinator/, op): occurrences}. Each needs a DoE-side repoint
# to the live owner or a death narration beside the token; a count only ever drops, and
# a page that gains an occurrence beyond its count fails the gate.
# `test_doctrine_prose_has_no_dead_op_references` (designed_red) prints the full worklist.
_KNOWN_STALE_BARE: dict[tuple[str, str], int] = {
    ("commands/architecture-survey.md", "cartography.churn"): 1,
    ("commands/workday-start.md", "session.boot_sweep"): 1,
    ("docs/wiki/ceremony-calibration/workstream-complete-conversion-archaeology.md", "session.boot_sweep"): 1,
    ("docs/wiki/ceremony-calibration/workstream-complete-review.md", "review_trail.write"): 2,
    ("docs/wiki/ceremony-calibration/workstream-complete-review.md", "session.boot_sweep"): 1,
    ("docs/wiki/concurrent-em-git-operations/concurrent-em-hazards.md", "handoff.archive_transition"): 1,
    ("docs/wiki/coordinator-tripwires/a-number-keeps-travelling-after-its-instrument-is-forgotten.md", "session.boot_sweep"): 1,
    ("docs/wiki/coordinator-tripwires/git-commit-tree-bypasses-the-session-id-trailer-and-review-trail-refuses-the-commit-later.md", "review_trail.write"): 2,
    ("docs/wiki/cross-repo-communication/cross-repo-memo-lifecycle.md", "session.boot_sweep"): 1,
    ("docs/wiki/cross-repo-communication/cross-repo-op-ownership-discriminator.md", "handoff.reconcile_open"): 1,
    ("docs/wiki/doctrine-authoring/named-contracts-vs-incidental-flags.md", "handoff.archive_transition"): 1,
    ("docs/wiki/doctrine-authoring/named-contracts-vs-incidental-flags.md", "handoff.has_live_children"): 1,
    ("docs/wiki/em-operating-model/verification-before-completion.md", "review_trail.write"): 1,
    ("docs/wiki/hook-best-practices/state-placement-law.md", "session.boot_sweep"): 1,
    ("docs/wiki/reviewer-pipeline/terminal-judge.md", "deliverable.cascade_terminal"): 1,
    ("docs/wiki/skills-corpus/architecture-survey-residue.md", "cartography.churn"): 1,
    ("skills/roadmap-planning/residue/contact-points-checklist.md", "session.boot_sweep"): 1,
}


def _live_op_names() -> frozenset[str]:
    from coordinator_core import ipc
    from coordinator_core.ops import _eager_import_all
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    _eager_import_all()
    return frozenset(ipc._REGISTRY) | frozenset(OP_MODULE_MAP)


def _dead_op_names(live: frozenset[str], *, with_ledger: bool) -> frozenset[str]:
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS

    dead = {name for name in SUSPENDED_OPS if "." in name}
    if with_ledger:
        from coordinator_core.op_census.kill_ledger_inventory import LedgerAbsent, fate_entries

        try:
            entries = fate_entries()
        except LedgerAbsent:
            entries = []
        dead.update(
            key
            for entry in entries
            for key in entry.op_keys
            if "." in key and key.rsplit(".", 1)[1] not in _FILE_SUFFIXES
        )
    return frozenset(dead) - live


def _references(line: str) -> Iterator[tuple[str, int, int, str]]:
    """(token, start, end, shape) for each dotted-name reference on `line`."""
    seen: set[int] = set()
    for match in _CALL.finditer(line):
        seen.add(match.start(1))
        yield match.group(1), match.start(1), match.end(1), "invoke"
    for match in _BARE.finditer(line):
        if match.start(1) not in seen:
            yield match.group(1), match.start(1), match.end(1), "bare"


def _stale_op_references(
    coordinator_dir: Path, live: frozenset[str], dead: frozenset[str]
) -> Iterator[tuple[str, int, str, str]]:
    """(page, line, op, shape) for each un-narrated reference to an op that is gone."""
    for sub in _SCAN_DIRS:
        base = coordinator_dir / sub
        if not base.is_dir():
            continue
        for page in sorted(base.rglob("*.md")):
            rel = page.relative_to(coordinator_dir).as_posix()
            text = page.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                for op, start, end, shape in _references(line):
                    gone = op not in live if shape == "invoke" else op in dead
                    if not gone:
                        continue
                    window = line[max(0, start - _NARRATION_WINDOW) : end + _NARRATION_WINDOW]
                    if not _DEAD_NARRATION.search(window):
                        yield rel, lineno, op, shape


def _doe_coordinator_dir() -> Optional[Path]:
    value = registry_get("engine.working_repos.content_root")
    if not value:
        return None
    candidate = Path(value) / "coordinator"
    return candidate if candidate.is_dir() else None


# Resolved at collection time: the per-test fixtures quarantine HOME, which hides the
# real machine-local registry from a test-time read.
_DOE_COORDINATOR_DIR = _doe_coordinator_dir()

_SKIP_REASON = (
    "engine.working_repos.content_root is unregistered or absent; register it with "
    "'machine-local set engine.working_repos.content_root <path>' to run this scan"
)


def _format(found) -> list[str]:
    return [
        f"{rel}:{lineno} `{op}`"
        + (" (invoked)" if shape == "invoke" else "")
        for rel, lineno, op, shape in found
    ]


def _beyond_baseline(hits) -> list[tuple[str, int, str, str]]:
    """Every invoke hit, plus bare hits past their page's baselined occurrence count."""
    seen: dict[tuple[str, str], int] = {}
    kept = []
    for rel, lineno, op, shape in hits:
        if shape == "bare":
            seen[(rel, op)] = seen.get((rel, op), 0) + 1
            if seen[(rel, op)] <= _KNOWN_STALE_BARE.get((rel, op), 0):
                continue
        kept.append((rel, lineno, op, shape))
    return kept


def test_doctrine_prose_names_only_registered_ops():
    if _DOE_COORDINATOR_DIR is None:
        pytest.skip(_SKIP_REASON)
    live = _live_op_names()
    found = _beyond_baseline(
        _stale_op_references(_DOE_COORDINATOR_DIR, live, _dead_op_names(live, with_ledger=False))
    )
    assert not found, (
        "doctrine prose names ops absent from the live registry (de-registered or "
        "misspelled); repoint each to the live op or narrate the death next to the "
        "token:\n  " + "\n  ".join(_format(found))
    )


@pytest.mark.designed_red
def test_doctrine_prose_has_no_dead_op_references():
    """Failure output is the worklist: every reference, baselined or not, with the
    kill ledger's op keys included where the ledger is present."""
    if _DOE_COORDINATOR_DIR is None:
        pytest.skip(_SKIP_REASON)
    live = _live_op_names()
    found = list(
        _stale_op_references(
            _DOE_COORDINATOR_DIR, live, _dead_op_names(live, with_ledger=True)
        )
    )
    assert not found, "dead-op references in doctrine prose:\n  " + "\n  ".join(_format(found))


def test_known_stale_baseline_names_dead_ops_only():
    live = _live_op_names()
    dead = _dead_op_names(live, with_ledger=False)
    assert {op for _page, op in _KNOWN_STALE_BARE} <= dead


def test_baseline_admits_its_count_and_flags_the_next_occurrence(monkeypatch):
    page, op = "docs/wiki/a.md", "session.boot_sweep"
    monkeypatch.setitem(_KNOWN_STALE_BARE, (page, op), 1)
    invoke = ("x.md", 9, "fleet.nope", "invoke")
    hits = [(page, 1, op, "bare"), (page, 5, op, "bare"), invoke]
    assert _beyond_baseline(hits) == [(page, 5, op, "bare"), invoke]


def test_registered_op_in_prose_is_clean(tmp_path):
    page = tmp_path / "skills" / "x" / "SKILL.md"
    page.parent.mkdir(parents=True)
    page.write_text("Run `coordinator-invoke ping '{}'`.\n", encoding="utf-8")
    live = _live_op_names()
    assert list(_stale_op_references(tmp_path, live, frozenset())) == []


def test_planted_dead_op_is_flagged_in_every_reference_shape(tmp_path):
    skills = tmp_path / "skills" / "x"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "Stamp via `coordinator-invoke deliverable.cascade_terminal '{}'`.\n"
        '`& "$env:H\\bin\\coordinator-invoke.exe" fleet.no_such_op_zzz \'{}\'`\n'
        "The sizing-object stamp owner is `deliverable.cascade_terminal`, which runs it.\n"
        "A page.md names fleet.no_such_op_zzz.md only as a file.\n",
        encoding="utf-8",
    )
    live = _live_op_names()
    dead = frozenset({"deliverable.cascade_terminal"})
    assert "deliverable.cascade_terminal" not in live
    found = set(_stale_op_references(tmp_path, live, dead))
    assert found == {
        ("skills/x/SKILL.md", 1, "deliverable.cascade_terminal", "invoke"),
        ("skills/x/SKILL.md", 2, "fleet.no_such_op_zzz", "invoke"),
        ("skills/x/SKILL.md", 3, "deliverable.cascade_terminal", "bare"),
    }


def test_bare_token_of_an_op_that_is_not_dead_is_not_flagged(tmp_path):
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "p.md").write_text("Set `plan.status` and read `engine.target`.\n", encoding="utf-8")
    assert list(_stale_op_references(tmp_path, _live_op_names(), frozenset())) == []


def test_narration_beside_the_token_exempts_it_but_a_distant_death_word_does_not(tmp_path):
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    far = "x " * 80
    (wiki / "p.md").write_text(
        "`handoff.archive_transition` is killed; a route naming it fails.\n"
        f"Delete the stale file, then call `handoff.archive_transition` {far}now.\n"
        f"The sweep was removed. {far}Run `handoff.archive_transition` next.\n",
        encoding="utf-8",
    )
    dead = frozenset({"handoff.archive_transition"})
    found = {
        (lineno, op)
        for _rel, lineno, op, _shape in _stale_op_references(tmp_path, _live_op_names(), dead)
    }
    assert found == {(2, "handoff.archive_transition"), (3, "handoff.archive_transition")}


def test_ledger_op_keys_join_the_dead_set_and_file_names_do_not(tmp_path, monkeypatch):
    from coordinator_core.op_census import kill_ledger_inventory as kli

    ledger = tmp_path / "kill-ledger.md"
    ledger.write_text(
        "## K-901 — `ledger.only_op`\n\n- **Fate (2026-08-30):** DEAD\n\n"
        "## K-902 — `coverage.py`\n\n- **Fate (2026-08-30):** DEAD\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kli, "KILL_LEDGER", ledger)
    monkeypatch.setattr(kli.fate_entries, "__defaults__", (ledger,))
    dead = _dead_op_names(_live_op_names(), with_ledger=True)
    assert "ledger.only_op" in dead
    assert "coverage.py" not in dead
