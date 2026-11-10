"""Replay of three halted runs whose terminal commit refused work the run itself finished.

(1) ask-20261103T045104-030545 (handoff.schema.json, refused `@@ -5,3 +5,3 @@` as `unattributed`) and
(2) warp-2026-11-03-landing-verify-importer-closure-5a0d91 (reverify_scope.py and its test): the run's
`write_baseline` blob was taken over uncommitted bytes that already differed from HEAD on the lines the
run then rewrote, so `hunk_stage.split` read those lines as pre-run dirt no run owns and
`unclaimed_dirt_stage.own_only_blobs` returned a refusal row that `land_run` surfaces as
`own-hunk-stage-refused ... (unattributed)`.
(3) ask-20261103T043326-d5e2eb: ledger rows met, review PASS, and `recovery.json` records the chunk's blocked
fix attempt followed by a `remnant` pass that fixed it, but the chunk's report still reads BLOCKED, so
`terminal_commit._ledger_met_regrades` marks it blocked, the chunk stays stranded and the commit refuses
`no-credited-chunk-landed`. The report text at the refusal is inferred (state/warp holds no copy).

The files are reduced analogues of the recorded blobs (handoff.schema.json is 148 KB, the baselines were
CRLF over LF HEAD blobs, the test file's baseline already held 26 of the run's 59 added lines); each keeps
the recorded overlap: baseline and run edit the same lines, or the same contiguous insertion. Records in
case (3) are the recorded recovery.json, ledger and kept paths. Nothing reads state/warp at run time.

Each case asserts the outcome a fixed engine gives (committed, nothing refused).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops.dispatch_emit import land_run, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c0"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"
REPORT = "reports/C1.md"
CRLF = "\r\n"
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}, {"id": "R2", "status": "met", "wired": True}]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _put(root: Path, rel: str, text: str, eol: str = "\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))


def _author(root: Path, rel: str, text: str, eol: str = "\n") -> None:
    _put(root, rel, text, eol)
    session_scope.touch(SELF, rel, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(session_dir_for(root / ".git", SELF)),
        session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on the box's line-ending policy (`core.autocrlf=true`: CRLF worktree, LF blobs)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "true")
    _put(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _put(root, "README.md", "seed\n")
    _put(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XS\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: first\n"
        "- turn: 2\n  text: second\n",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _commit_head(root: Path, files: dict) -> None:
    for rel, text in files.items():
        _put(root, rel, text)
    _put(
        root, PLAN,
        "---\ntitle: p\ndeliverable_id: dlv-alpha-c0c0c0\nsizing_object: " + SIZING + "\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")


def _declare(root: Path, run: str, paths: tuple, baseline: dict) -> str:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=paths, report=REPORT),),
        deliverable_id="dlv-alpha-c0c0c0", plan_path=PLAN,
    )
    _put(root, f"{RUN_DIR_ROOT}/{run}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{run}/commit-request.txt", write_baseline=baseline,
    )
    _put(root, f"{RUN_DIR_ROOT}/{run}/manifest.json", json.dumps(manifest.to_json()))
    _put(root, f"{run}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{run}/manifest.json\n")
    rl.start(root, run, all_of_pm_ask=False)
    rl.bind(root, run, SIZING)
    return run_token.mint(root, run, f"{run}.mjs", session_id=SELF)


def _review(slice_files: list) -> dict:
    return {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": slice_files, "foreign_claims": [], "slice_files": slice_files},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }


def _replay_own_hunks(root: Path, run: str, shapes: dict) -> dict:
    """`shapes[path] = (head, baseline, worktree, eol)`: HEAD holds `head` (LF blob); the live tree held
    `baseline` when the run staged (blob persisted raw); the run then wrote `worktree`. Lands through
    `dispatch.land_run`, the path the terminal commit takes."""
    _commit_head(root, {p: head for p, (head, _b, _w, _e) in shapes.items()})
    for p, (_h, base, _w, eol) in shapes.items():
        _put(root, p, base, eol)
    baseline = wb.capture(root, list(shapes))
    _put(root, REPORT, "PARTIAL: C1\nNot done: nothing edited, files written by the run\n")
    for p, (_h, _b, work, eol) in shapes.items():
        _author(root, p, work, eol)
    paths = tuple(shapes)
    token = _declare(root, run, paths, baseline)
    return land_run._handler(
        {
            "script_path": f"{run}.mjs", "incomplete_chunks": ["C1"], "inline_review": _review(list(paths)),
            "session_id": SELF, "run_id": run, "run_token": token,
            "ledger_verdict": _MET,
        },
        repo_root=root / ".git",
    )


def _landed(out: dict, root: Path, paths) -> None:
    assert out.get("committed") is True, out
    assert "unattributed" not in str(out.get("error")), out
    assert set(paths) <= set(_git(root, "show", "--name-only", "--format=", "HEAD").split()), out


# ---- case (1): handoff.schema.json -------------------------------------------------------------

HANDOFF = "coordinator_core/frontmatter/schemas/handoff.schema.json"


def _handoff(*, version, bump, note, extras, pattern) -> str:
    """Recorded lines 1-9, the producer.typed_command vocabulary window and the `gate_cleared_by` window
    of handoff.schema.json; `extras` is the two vocabulary members the 11.2.0 bump on HEAD added."""
    vocab = ["architecture-survey", *(["artifact-investigation"] if extras else []), "autonomous",
             *(["box-onboarding"] if extras else []), "brainstorming"]
    lines = [
        "{",
        '  "$schema": "https://json-schema.org/draft/2020-12/schema",',
        '  "$id": "https://coordinator.local/schemas/handoff.schema.json",',
        '  "x-schema-name": "handoff",',
        f'  "x-schema-version": "{version}",',
        f'  "x-bump-class": "{bump}",',
        f'  "x-bump-note": "{note}",',
        '  "applies_to": "state/handoffs/**/*.md",',
        '  "kinds": [',
        *[f'    "kind-{i}",' for i in range(24)],
        "  ],",
        '  "mapping": {',
        *[line for name in vocab for line in (f'    "{name}": {{', '      "group": "g"', "    },")],
        *[f'    "member-{i}": {{ "group": "g" }},' for i in range(200)],
        '    "gate_cleared_by": {',
        '      "type": "array",',
        '      "items": {',
        '        "type": "string",',
        f'        "pattern": "{pattern}"',
        "      },",
        '      "description": "Provenance of the act that discharged the gate."',
        "    },",
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


_HEX = "^[0-9a-fA-F]{7,64}$"
_PM_TURN = "^(?:[0-9a-fA-F]{7,64}|pm-turn:[A-Za-z0-9._-]+#[0-9]+)$"


def test_handoff_schema_own_hunks_over_a_baseline_that_held_a_peers_bump_land(repo):
    head = _handoff(version="11.2.0", bump="enum-value-additive", note="11.1.0 -> 11.2.0 adds vocabulary",
                    extras=True, pattern=_HEX)
    baseline = _handoff(version="11.2.0", bump="nested-field-additive",
                        note="11.1.0 -> 11.2.0 widens the gate_cleared_by pattern", extras=False, pattern=_PM_TURN)
    work = _handoff(version="11.3.0", bump="nested-field-additive",
                    note="11.2.0 -> 11.3.0 widens the gate_cleared_by pattern", extras=True, pattern=_PM_TURN)

    out = _replay_own_hunks(repo, "ask-20261103T045104-030545", {HANDOFF: (head, baseline, work, CRLF)})

    _landed(out, repo, [HANDOFF])


# ---- case (2): reverify_scope.py and its test ---------------------------------------------------

SOURCE = "coordinator_core/ops/reverify_scope.py"
TEST = "coordinator_core/tests/test_reverify_scope.py"

_SOURCE_HEAD = '''"""Reverify scope."""

from __future__ import annotations

from typing import List

from pkg.git import run_git
from pkg.review import _imported_edited
from pkg.map import map_source_to_tests


def _changed_paths(repo, base, head) -> List[str]:
    return run_git(["diff", "--name-only", base, head])


def select_tests(repo, base, head="HEAD") -> dict:
    tests = set()
    for path in _changed_paths(repo, base, head):
        tests.update(map_source_to_tests(path, repo))
    return {"tests": sorted(tests)}
'''
_SOURCE_BASELINE = '''"""Reverify scope."""

from __future__ import annotations

from typing import List

from pkg.git import run_git
from pkg.git import run_git
from pkg.review import _imported_edited
from pkg.map import map_source_to_tests


def _changed_paths(repo, base, head) -> List[str]:
    return run_git(["diff", "--name-only", base, head])


def _importers(repo, sources):
    out = set()
    for cand in run_git(["grep", "-l", "-F", "-e", "leaf"]):
        if _imported_edited(repo, cand, sources):
            out.add(cand)
    return out


def select_tests(repo, base, head="HEAD") -> dict:
    tests = set()
    changed = _changed_paths(repo, base, head)
    for path in changed:
        tests.update(map_source_to_tests(path, repo))
    for importer in _importers(repo, set(changed)):
        tests.update(map_source_to_tests(importer, repo))
    return {"tests": sorted(tests)}
'''
_SOURCE_WORK = '''"""Reverify scope."""

from __future__ import annotations

import re
from typing import Dict, List

from pkg.git import run_git
from pkg.map import map_source_to_tests


def _changed_paths(repo, base, head) -> List[str]:
    return run_git(["diff", "--name-only", base, head])


def _importers(repo, sources) -> Dict[str, set]:
    out: Dict[str, set] = {}
    pattern = "|".join(re.escape(s) for s in sorted(sources))
    for cand in run_git(["grep", "-l", "-E", "-e", pattern]):
        out[cand] = set(sources)
    return out


def select_tests(repo, base, head="HEAD") -> dict:
    tests = set()
    changed = _changed_paths(repo, base, head)
    for path in changed:
        tests.update(map_source_to_tests(path, repo))
    for importer in _importers(repo, set(changed)):
        tests.update(map_source_to_tests(importer, repo))
    return {"tests": sorted(tests)}
'''

_TEST_HEAD = '''from pkg.reverify_scope import select_tests


def test_changed_file_selects_its_test():
    assert select_tests("r", "a")["tests"] == ["t"]


def test_second():
    assert True
'''
_TEST_BASELINE = _TEST_HEAD + '''

def test_importer_tests_are_selected():
    assert select_tests("r", "a")["tests"] == ["t", "u"]


def test_unrelated_module_is_not_an_importer():
    assert select_tests("r", "a")["tests"] == ["t"]
'''
_TEST_WORK = _TEST_BASELINE + '''

def test_live_run_writes_miss_oracle_is_selected_in_one_pass():
    assert len(select_tests("r", "a")["tests"]) == 1
'''


def test_reverify_scope_own_hunks_over_a_baseline_that_held_an_earlier_attempt_land(repo):
    shapes = {
        SOURCE: (_SOURCE_HEAD, _SOURCE_BASELINE, _SOURCE_WORK, CRLF),
        TEST: (_TEST_HEAD, _TEST_BASELINE, _TEST_WORK, "\n"),
    }

    out = _replay_own_hunks(repo, "warp-2026-11-03-landing-verify-importer-closure-5a0d91", shapes)

    _landed(out, repo, [SOURCE, TEST])


# ---- case (3): met ledger, a BLOCKED report the remnant pass superseded ---------------------------

RUN3 = "ask-20261103T043326-d5e2eb"
C1_PATHS = (
    "coordinator/bin/publish.py",
    "coordinator/bin/tests/test_publish_quiesces_warm_pair.py",
    "coordinator_core/warm/pair_stop.py",
    "coordinator_core/warm/tests/test_pair_stop.py",
)
DISPATCH_REPORT = ".coordinator-local/subagent-share/dispatch-reports/2026-11-03-pair-stop-load-tolerant-exit-wait-d5e2eb/C1.md"
#: The report the blocked fix leg left in the chunk's dispatch-report file (the C1.md it wrote outside the
#: footprint). Inferred, not recorded: state/warp keeps no copy of the refused call's report text.
C1_REPORT = (
    "BLOCKED: R2 gap already closed in the tree, no edit made\n\n"
    "The dispatch report path is outside the footprint.\n"
)
RECOVERY = {
    "cap": 2,
    "chunks": {
        "C1": {
            "rows": ["R2"], "attempts": 1, "status": "blocked", "footprint": list(C1_PATHS),
            "paths": [DISPATCH_REPORT],
            "last_reason": f"fix wrote outside the footprint: {DISPATCH_REPORT}",
        }
    },
    "remnant": {
        "field": "remnant", "loop_owner": "engine", "cap": 1,
        "items": [{"kind": "ledger", "id": "R2"}, {"kind": "criterion", "id": "criterion"}],
        "footprint": [*C1_PATHS], "status": "fixed", "paths": [],
        "last_reason": "Both gaps were already closed in the working tree, so I made no edits.",
        "still_open": [],
    },
}


def test_met_ledger_chunk_a_blocked_report_the_remnant_pass_superseded_is_credited(repo):
    _commit_head(repo, {p: "v1\n" for p in C1_PATHS})
    for p in C1_PATHS:
        _author(repo, p, "v2\n")
    _put(repo, REPORT, C1_REPORT)
    _put(repo, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=C1_PATHS, report=REPORT),
            ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),
        )
    )
    _put(repo, f"{RUN_DIR_ROOT}/{RUN3}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN3}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN3}/commit-request.txt",
    )
    _put(repo, f"{RUN_DIR_ROOT}/{RUN3}/manifest.json", json.dumps(manifest.to_json()))
    _put(repo, f"{RUN_DIR_ROOT}/{RUN3}/recovery.json", json.dumps(RECOVERY))
    _put(repo, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN3}/manifest.json\n")
    rl.start(repo, RUN3, all_of_pm_ask=False)
    rl.bind(repo, RUN3, SIZING)

    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": ["C1"],
            "inline_review": _review(list(C1_PATHS)), "session_id": SELF,
            "ledger_verdict": _MET,
        },
        repo_root=repo / ".git",
    )

    assert out.get("committed") is True, out
    assert out.get("refused") != "no-credited-chunk-landed", out
    assert out.get("regraded", {}).get("C1", "").startswith("ledger-met"), out
    assert set(C1_PATHS) <= set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split()), out
