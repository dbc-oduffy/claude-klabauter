"""EM-audience render baseline for every speaking bash-guard corpus row.

Enumeration method: the registered chain (`dispatch._build_guard_chain`) is the
population of GuardEntry names. Each name must carry at least one corpus row
that speaks to the EM audience, or sit in `_SILENT_FOR_EM` with a reason. A
return path that yields a non-None envelope is a message branch; a branch is
covered when some speaking row fires it. The fixture holds one entry per
`<guard>::<row_id>` and is keyed on the chain-entry name only, never a module
path or function name.

Regeneration is explicit and runs under pytest, so the fixture is captured in
the same hermetic environment the comparison runs in: set
`REGENERATE_GUARD_RENDER_BASELINE=1` and run `test_regenerate_baseline`. No test
here sets that variable.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Dict, List

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    CONFINEMENT_ROWS,
    EM_AUDIENCE,
    PLATFORM_CONDITIONED_ROWS,
    CorpusRow,
    fire_row_for_audience,
    fixture_scratch_spans,
)

REGENERATE_ENV = "REGENERATE_GUARD_RENDER_BASELINE"

FIXTURE_PATH =Path(__file__).with_name("fixtures") / "guard_render_baseline.json"

_ROWS: List[CorpusRow] = CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS

#: Guards with no EM-audience message branch, each with the reason it cannot speak.
#: Exact-set pinned: a guard leaving this set needs a speaking row and a fixture entry.
_SILENT_FOR_EM: Dict[str, str] = {
    "block-em-post-fire-product-write": "corpus fire row names no path owned by a live run (owns_path scope); setup lives in guard_message_corpus.py",
    "block-subagent-stash-creation": "subagent-cohort guard",
    "block-subagent-plan-body-bash-write": "subagent-cohort guard",
    "block-reviewer-bash-outside-allowlist": "reviewer-cohort guard",
    "block-subagent-destructive-action": "subagent-cohort guard",
    "block-subagent-commit": "subagent-cohort guard",
    "guard-host-subagent-bash-ban": "subagent-cohort guard",
    "guard-host-subagent-bash-spawn-shapes": "subagent-cohort guard",
    "guard-subagent-heavy-ue-launch": "subagent-cohort guard",
    "block-subagent-grant-acquisition": "subagent-cohort guard",
    "block-subagent-findings-reject": "subagent-cohort guard",
    "block-subagent-guard-grant": "subagent-cohort guard",
    "guard-post-executor-verification": "subagent-cohort guard",
    "reap-stale-git-lock": "side-effect only; always returns None",
}

_SESSION_RE = re.compile(r"guard-message-corpus-audience-[a-z]+-[0-9a-f]{32}")
_SCRATCH_RE = re.compile(r"^.*?guard-message-corpus-audience-[A-Za-z0-9_]+")
_SCRATCH_TAIL_RE = re.compile(r"(?<=guard-message-corpus-audience-)[A-Za-z0-9_]+")


_DAY_BRANCH_RE = re.compile(r"work/[A-Za-z0-9_-]+/\d{4}-\d{2}-\d{2}")
_PYTHON_PATH = r'(?:\$HOME|~|/|[A-Za-z]:)[^"\s]*python[0-9.]*(?:\.exe)?'
#: A quoted interpreter path, or an unquoted one standing alone as a command word.
_PYTHON_RE = re.compile(r'"%s"|(?<![^\s"\'])%s(?=[\s"\'])' % (_PYTHON_PATH, _PYTHON_PATH))
#: macOS resolves a tmp dir through `/private`, which a guard that realpaths its target
#: renders in front of the scratch span.
_REALPATH_PREFIX_RE = re.compile(r"/private(?=<scratch>)")
#: A clause a guard renders only when the host is Windows. It is a real platform branch
#: (pinned in test_bump_outside_repo_write), so the host-invariant comparison drops it
#: from both sides rather than letting whichever platform captured the fixture win.
_PLATFORM_CLAUSE_RE = re.compile(r"; unquoted backslashes, so bash writes into cwd instead")


def _drop_platform_clauses(text: str) -> str:
    return _PLATFORM_CLAUSE_RE.sub("", text)


def _normalise_text(text: str) -> str:
    """Rewrite only what varies per fire or per host: session ids, the per-row scratch
    dir, the day-branch name a guard suggests, the interpreter path it renders, the macOS
    `/private` realpath prefix, and Windows-only clauses."""
    text = _SESSION_RE.sub("<session>", text)
    text = _DAY_BRANCH_RE.sub("work/<machine>/<date>", text)
    text = _PYTHON_RE.sub("<python>", text)
    for start, end in sorted(fixture_scratch_spans(text), reverse=True):
        span = text[start:end].replace("\\", "/")
        span = _SCRATCH_RE.sub("<scratch>", span)
        text = text[:start] + span + text[end:]
    text = _REALPATH_PREFIX_RE.sub("", text)
    return _drop_platform_clauses(text)


def _normalise(value):
    if isinstance(value, str):
        return _normalise_text(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _key(row: CorpusRow) -> str:
    return "%s::%s" % (row.guard, row.row_id)


def render_all() -> Dict[str, str]:
    """Fire every expected-speaker row as the EM; key -> canonical normalised envelope."""
    rendered: Dict[str, str] = {}
    with pytest.MonkeyPatch.context() as mp:
        # The write-bump guards stand down on a host with no fleet; pin the host so the
        # render does not depend on where it runs.
        mp.setenv("COORDINATOR_CAP_FLEET_PRESENT", "1")
        for row in _ROWS:
            if not row.expected_speaker:
                continue
            capture = fire_row_for_audience(row, EM_AUDIENCE)
            envelope = envelope_of(capture.envelope, "em")
            if envelope is None:
                continue
            rendered[_key(row)] = json.dumps(
                _normalise(envelope), sort_keys=True, ensure_ascii=False, indent=1
            )
    return rendered


def write_baseline() -> None:
    entries = render_all()
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps({"entries": entries}, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def _load_baseline() -> Dict[str, str]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["entries"]


def _registered_names() -> set:
    chain = dispatch._build_guard_chain(
        cmd="git status",
        session_id="guard-render-baseline-registration",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
        policy_file=None,
        host_is_windows=False,
    )
    return {entry.name for entry in chain}


def test_every_speaking_row_matches_the_committed_baseline():
    """A key that disappears or appears is a failure, never a skip."""
    baseline = _load_baseline()
    rendered = render_all()
    assert set(rendered) == set(baseline), (
        "baseline keys drifted: missing from render %s; absent from fixture %s"
        % (sorted(set(baseline) - set(rendered)), sorted(set(rendered) - set(baseline)))
    )
    drifted = sorted(
        key for key in baseline if rendered[key] != _drop_platform_clauses(baseline[key])
    )
    detail = "".join(
        "\n--- %s\n%s" % (
            key,
            "\n".join(
                difflib.unified_diff(
                    _drop_platform_clauses(baseline[key]).splitlines(),
                    rendered[key].splitlines(),
                    lineterm="",
                    n=0,
                )
            ),
        )
        for key in drifted[:3]
    )
    assert not drifted, "EM-audience render changed for: %s%s" % (drifted, detail)


def test_every_expected_speaker_row_has_a_fixture_entry():
    baseline = _load_baseline()
    missing = sorted(
        _key(row)
        for row in _ROWS
        if row.expected_speaker and _key(row) not in baseline and row.guard not in _SILENT_FOR_EM
    )
    assert not missing, "expected-speaker rows absent from the fixture: %s" % missing


def test_baseline_keys_are_chain_entry_names_never_module_paths():
    names = _registered_names()
    for key in _load_baseline():
        guard, _, row_id = key.partition("::")
        assert guard in names, "baseline key names no registered chain entry: %s" % key
        assert row_id and "/" not in key and "\\" not in key and ".py" not in key, key


def test_every_registered_guard_speaks_to_the_em_or_is_pinned_silent():
    spoke = {key.partition("::")[0] for key in _load_baseline()}
    silent = _registered_names() - spoke
    assert silent == set(_SILENT_FOR_EM), (
        "silent-for-EM set drifted: unexpected silent %s; pinned but now speaking %s"
        % (sorted(silent - set(_SILENT_FOR_EM)), sorted(set(_SILENT_FOR_EM) & spoke))
    )
    assert all(_SILENT_FOR_EM.values())


def test_baseline_text_carries_no_per_fire_noise():
    for key, text in _load_baseline().items():
        assert not _SESSION_RE.search(text), key
        assert not _SCRATCH_TAIL_RE.search(text), key


@pytest.mark.skipif(
    os.environ.get(REGENERATE_ENV) != "1",
    reason="rewrites the committed baseline; set %s=1 to run" % REGENERATE_ENV,
)
def test_regenerate_baseline():
    write_baseline()
