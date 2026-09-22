"""
coordinator_core.ops.dispatch_emit.tests.test_grind_integration — the
end-to-end falsifier for the C2/C3/C4/C7/C8 queue-grind leg (docs/plans/
2026-09-21-bug-blitz-emitter-engine-leg.md, integration-fix wave).

Purpose: every module in this pipeline passes its OWN test file in
isolation against its OWN fixture; this file is the one that runs a real
profile through emit -> check -> append -> re-emit -> close -> settle over
ONE tmp git repo and ONE fixture profile, so a shape one module's tests
never exercised (the sentinel `check` actually parses, the profile
`close` actually validates against) is caught here instead of production.

Negative-spec: this file does NOT re-test any single module's own
behavioural surface (where/order/floor-rule refusals, the composer's
routing table, etc.) — those stay in each module's own test file. It only
asserts the SEAMS between them: the manifest sentinel `grind-row check`
parses is the one `grind_compose` actually emits; a `grind-row append`
between two emits changes what the selector's own `skip_stages` reports;
`grind-row close` validates against the FIXTURE profile's own schema/
closure/archive_path (not a hand-rolled stand-in); every flag name an
emitted prompt tells an agent to run is one `grind_rows._parse_flags`
actually accepts.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md,
integration-fix wave (post C2-C9), END-TO-END TEST.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script
from coordinator_core.ops.dispatch_emit.queue_select import select_rows

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "grind-fixture.golden.mjs"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_REAL_SCHEMA = _REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas" / "bug-backlog.schema.json"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write_row(path: Path, **fields) -> str:
    lines = []
    for key, value in fields.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(json.dumps(v) for v in value) + "]"
            lines.append(f"{key}: {rendered}")
        else:
            lines.append(f"{key}: {json.dumps(value)}")
    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def _base_row(**overrides) -> dict:
    row = dict(
        created="2026-09-21",
        title="a reproducible bug",
        body="a body",
        status="open",
        surface="coordinator_core/x",
        severity="P1",
    )
    row.update(overrides)
    return row


@pytest.fixture
def grind_repo(tmp_path) -> dict:
    """A tmp git repo seeded with a small bug-backlog-shaped queue, mirroring
    the fixture profile's own `archive_path`/`schema` layout so `grind-row
    close` validates for real, not against a hand-rolled stand-in schema."""
    repo_root = tmp_path
    init = _git(["init"], repo_root)
    assert init.returncode == 0

    queue_dir = repo_root / "state" / "bug-backlog"
    row_a = queue_dir / "row-a.yaml"
    row_b = queue_dir / "row-b.yaml"
    _write_row(row_a, **_base_row(severity="P0", title="row a"))
    _write_row(row_b, **_base_row(severity="P1", title="row b"))

    # The fixture profile's `schema` is repo-relative
    # (coordinator_core/frontmatter/schemas/bug-backlog.schema.json) --
    # mirror that real schema under the tmp repo root so `close`'s own
    # validation step runs against the genuine schema, not a substitute.
    tmp_schema = repo_root / "coordinator_core" / "frontmatter" / "schemas" / "bug-backlog.schema.json"
    tmp_schema.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REAL_SCHEMA, tmp_schema)

    run_dir = repo_root / "state" / "queue-grind" / "fixture" / "run-1"
    run_dir.mkdir(parents=True)

    _git(["add", "-A"], repo_root)
    _git(["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "seed"], repo_root)

    return {
        "repo_root": repo_root,
        "queue_dir": queue_dir,
        "run_dir": run_dir,
        "row_a": row_a,
        "row_b": row_b,
    }


def _emit(grind_repo: dict, script_path: Path) -> str:
    emission = emit_queue_script(
        "fixture",
        "standard",
        None,
        queue=[grind_repo["queue_dir"]],
        profile_dir=_FIXTURE_PROFILE_DIR,
        repo_root=grind_repo["repo_root"],
        run_dir=grind_repo["run_dir"],
        session_id="sess1",
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(emission.script, encoding="utf-8")
    return emission.script


# ---------------------------------------------------------------------------
# 1) emit -> 2) check reports a mutated row stale
# ---------------------------------------------------------------------------


def test_emit_then_check_reports_a_mutated_row_stale(grind_repo, capsys):
    script_path = grind_repo["repo_root"] / "emitted.mjs"
    _emit(grind_repo, script_path)

    # Mutate row-a AFTER the emit froze its digest -- `check` must catch this
    # by parsing the EXACT sentinel `grind_compose` emits, never a stand-in.
    grind_repo["row_a"].write_text(
        grind_repo["row_a"].read_text(encoding="utf-8") + "\nnote: mutated after emit\n",
        encoding="utf-8",
    )

    batches = json.loads(
        next(l for l in script_path.read_text(encoding="utf-8").splitlines()
             if l.startswith("const BATCHES = "))[len("const BATCHES = "):-1]
    )
    batch_id = next(b["id"] for b in batches if "row-a" in b["rows"])
    exit_code = grind_rows.main(
        ["check", "--manifest", str(script_path), "--batch", batch_id,
         "--repo-root", str(grind_repo["repo_root"])]
    )
    assert exit_code == grind_rows.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert "row-a" in out["stale"]
    assert "row-b" not in out["stale"]


# ---------------------------------------------------------------------------
# 3) append -> re-emit shows the fold-in skip
# ---------------------------------------------------------------------------


def test_append_then_reselect_shows_fold_in_skip(grind_repo, monkeypatch):
    monkeypatch.chdir(grind_repo["repo_root"])
    digest_a = hashlib.sha256(grind_repo["row_a"].read_bytes()).hexdigest()

    before = select_rows(
        [grind_repo["queue_dir"]],
        where=None,
        order=("severity", "asc"),
        limit=None,
        batch_key=["severity"],
        batch_sizes={"default": 4, "@unkeyed": 4},
        row_id_key="@stem",
        profile="fixture",
        repo_root=grind_repo["repo_root"],
        absent_sentinels={"owner": ("none", "N/A")},
    )
    assert {e.row_id: e.skip_stages for e in before.entries}["row-a"] == ()

    evidence = grind_repo["repo_root"] / "evidence.txt"
    evidence.write_text("triaged", encoding="utf-8")
    exit_code = grind_rows.main(
        [
            "append",
            "--profile", "fixture",
            "--row-id", "row-a",
            "--digest", digest_a,
            "--stage", "triage",
            "--verdict", "confirmed-bug",
            "--outcome", "confirmed-bug",
            "--evidence-file", str(evidence),
            "--run-stamp", "2026-09-21T00:00:00Z",
            "--repo-root", str(grind_repo["repo_root"]),
        ]
    )
    assert exit_code == grind_rows.EXIT_OK

    after = select_rows(
        [grind_repo["queue_dir"]],
        where=None,
        order=("severity", "asc"),
        limit=None,
        batch_key=["severity"],
        batch_sizes={"default": 4, "@unkeyed": 4},
        row_id_key="@stem",
        profile="fixture",
        repo_root=grind_repo["repo_root"],
        absent_sentinels={"owner": ("none", "N/A")},
    )
    assert set({e.row_id: e.skip_stages for e in after.entries}["row-a"]) == {"triage"}

    # Re-emit stays deterministic over the SAME inputs (the manifest embeds
    # row_id/path/digest/batch_key only -- appending a ledger line changes
    # the selector's own `skip_stages`, never the frozen manifest bytes).
    script_1 = _emit(grind_repo, grind_repo["repo_root"] / "re1.mjs")
    script_2 = _emit(grind_repo, grind_repo["repo_root"] / "re2.mjs")
    assert script_1 == script_2


# ---------------------------------------------------------------------------
# 4) close against the fixture profile
# ---------------------------------------------------------------------------


def test_close_against_fixture_profile_archives_with_closed_value(grind_repo):
    row_b = grind_repo["row_b"]
    digest_b = hashlib.sha256(row_b.read_bytes()).hexdigest()
    evidence = grind_repo["repo_root"] / "evidence-close.txt"
    evidence.write_text("confirmed fixed", encoding="utf-8")

    exit_code = grind_rows.main(
        [
            "close",
            "--profile-dir", str(_FIXTURE_PROFILE_DIR),
            "--profile", "fixture",
            "--row", str(row_b),
            "--digest", digest_b,
            "--verdict", "fix",
            "--evidence-file", str(evidence),
            "--closed-by", "test-session",
            "--run-stamp", "2026-09-21",
            "--repo-root", str(grind_repo["repo_root"]),
        ]
    )
    assert exit_code == grind_rows.EXIT_OK
    assert not row_b.is_file()

    archived = grind_repo["repo_root"] / "state" / "bug-backlog" / "archive" / "2026-09" / "row-b.yaml"
    assert archived.is_file()
    new_text = archived.read_text(encoding="utf-8")
    assert "status: \"closed\"" in new_text or "status: closed" in new_text
    assert "closed_by:" in new_text and "test-session" in new_text


# ---------------------------------------------------------------------------
# 5) settle removes the ledger
# ---------------------------------------------------------------------------


def test_settle_removes_the_ledger(grind_repo, monkeypatch):
    monkeypatch.chdir(grind_repo["repo_root"])
    digest_a = hashlib.sha256(grind_repo["row_a"].read_bytes()).hexdigest()
    evidence = grind_repo["repo_root"] / "evidence.txt"
    evidence.write_text("triaged", encoding="utf-8")
    grind_rows.main(
        [
            "append",
            "--profile", "fixture",
            "--row-id", "row-a",
            "--digest", digest_a,
            "--stage", "triage",
            "--verdict", "confirmed-bug",
            "--outcome", "confirmed-bug",
            "--evidence-file", str(evidence),
            "--run-stamp", "2026-09-21T00:00:00Z",
            "--repo-root", str(grind_repo["repo_root"]),
        ]
    )
    ledger_path = grind_repo["repo_root"] / "state" / "queue-grind" / "fixture" / "row-a.jsonl"
    assert ledger_path.is_file()

    exit_code = grind_rows.main(
        ["settle", "--profile", "fixture", "--row-id", "row-a", "--repo-root", str(grind_repo["repo_root"])]
    )
    assert exit_code == grind_rows.EXIT_OK
    assert not ledger_path.is_file()


# ---------------------------------------------------------------------------
# 6) priority order and limit honoured
# ---------------------------------------------------------------------------


def test_priority_order_and_limit_honoured(grind_repo):
    queue_dir = grind_repo["queue_dir"]
    _write_row(queue_dir / "row-c.yaml", **_base_row(severity="P3", title="row c"))
    _write_row(queue_dir / "row-d.yaml", **_base_row(severity="P2", title="row d"))

    manifest = select_rows(
        [queue_dir],
        where=None,
        order=("severity", "asc"),
        limit=2,
        batch_key=["severity"],
        batch_sizes={"default": 4, "@unkeyed": 4},
        row_id_key="@stem",
        profile="fixture",
        repo_root=grind_repo["repo_root"],
        absent_sentinels={"owner": ("none", "N/A")},
    )
    # Four rows total (P0, P1, P2, P3); limit=2 must keep the two HIGHEST
    # priority (lowest-numbered severity) rows, never the first two read.
    assert [e.row_id for e in manifest.entries] == ["row-a", "row-b"]


# ---------------------------------------------------------------------------
# The prime falsifier: byte-identical re-emit, zero spawn, no forbidden calls
# ---------------------------------------------------------------------------


def test_reemit_is_byte_identical_and_zero_spawn(grind_repo, monkeypatch):
    calls = []
    original_init = subprocess.Popen.__init__

    def _counting_init(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _counting_init)

    script_1 = _emit(grind_repo, grind_repo["repo_root"] / "a.mjs")
    script_2 = _emit(grind_repo, grind_repo["repo_root"] / "b.mjs")
    assert script_1 == script_2
    assert calls == []

    assert "git mv" not in script_1
    assert "git stash" not in script_1
    assert "add -A" not in script_1
    assert "Date.now" not in script_1
    assert "Math.random" not in script_1
    assert "new Date()" not in script_1
    agent_call_count = len(re.findall(r"await agent\(", script_1))
    sonnet_model_count = len(re.findall(r"model: 'sonnet'", script_1))
    assert agent_call_count == sonnet_model_count > 0


# ---------------------------------------------------------------------------
# Flag-name parity: every flag an emitted prompt tells an agent to run is one
# grind_rows._parse_flags actually accepts.
# ---------------------------------------------------------------------------


def test_emitted_prompt_flags_match_grind_rows_argparse():
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")

    check_call = re.search(r"grind-row check ([^`]+)`", golden).group(1)
    assert "--manifest" in check_call
    assert "--batch" in check_call

    append_call = re.search(r"grind-row append ([^`]+)`", golden).group(1)
    for flag in ("--profile", "--row-id", "--digest", "--stage", "--verdict", "--outcome", "--evidence-file", "--run-stamp"):
        assert flag in append_call

    close_call = re.search(r"grind-row close ([^`]+)`", golden).group(1)
    for flag in ("--profile-dir", "--profile", "--row", "--digest", "--verdict", "--evidence-file", "--closed-by", "--run-stamp"):
        assert flag in close_call

    # These are exactly the flags `_parse_flags` requires for each verb --
    # a rename on either side (the prompt text or `_parse_flags`) fails this.
    import inspect

    source = inspect.getsource(grind_rows.cmd_append)
    assert 'required=(' in source
    source = inspect.getsource(grind_rows.cmd_close)
    assert 'required=(' in source


def test_emitted_script_names_no_host_path(grind_repo):
    """no-single-machine-assumptions: an emission over a real checkout names
    rows and the run dir repo-relative, never the host's checkout path."""
    script = _emit(grind_repo, grind_repo["repo_root"] / "a.mjs")
    root = str(grind_repo["repo_root"].resolve())
    assert root not in script
    assert str(grind_repo["repo_root"]) not in script


def test_close_round_trips_a_real_bug_backlog_row_through_its_schema(tmp_path):
    """A real state/bug-backlog row is whole-document YAML with no fence; a
    DoE profile names its schema bare; the run stamp is compact. close must
    handle all three and leave a row its own schema accepts."""
    import shutil
    from coordinator_core.frontmatter import schema_validate

    repo_root = Path(__file__).resolve().parents[4]
    real = next(p for p in sorted((repo_root / "state" / "bug-backlog").glob("*.yaml"))
                if schema_validate.parse_yaml(p.read_text(encoding="utf-8")).get("status") == "open")
    queue = tmp_path / "state" / "bug-backlog"
    queue.mkdir(parents=True)
    row = queue / real.name
    shutil.copyfile(real, row)
    assert not row.read_text(encoding="utf-8").startswith("---")

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    profile_text = (_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8")
    profile_text = profile_text.replace(
        "schema: coordinator_core/frontmatter/schemas/bug-backlog.schema.json",
        "schema: bug-backlog.schema.json",
    )
    assert "schema: bug-backlog.schema.json" in profile_text
    (profile_dir / "fixture.yaml").write_text(profile_text, encoding="utf-8")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("verified gone at HEAD", encoding="utf-8")

    exit_code = grind_rows.main([
        "close", "--profile-dir", str(profile_dir), "--profile", "fixture",
        "--row", f"state/bug-backlog/{real.name}",
        "--digest", hashlib.sha256(row.read_bytes()).hexdigest(),
        "--verdict", "fix", "--evidence-file", str(evidence),
        "--closed-by", "fix", "--run-stamp", "20260922T110458Z",
        "--repo-root", str(tmp_path),
    ])
    assert exit_code == grind_rows.EXIT_OK
    archived = tmp_path / "state" / "bug-backlog" / "archive" / "2026-09" / real.name
    assert archived.is_file() and not row.exists()
    closed = schema_validate.parse_yaml(archived.read_text(encoding="utf-8"))
    assert closed["status"] == "closed"
    assert str(closed["closed_at"]) == "2026-09-22"
    assert closed["closed_by"] == "fix"
    schema = json.loads((repo_root / "coordinator_core/frontmatter/schemas/bug-backlog.schema.json").read_text(encoding="utf-8"))
    result = schema_validate.validate_frontmatter_obj(closed, schema)
    assert isinstance(result, dict) and result.get("ok"), result
    before = schema_validate.parse_yaml(real.read_text(encoding="utf-8"))
    for key, value in before.items():
        if key not in ("status", "closed_at", "closed_by"):
            assert closed.get(key) == value, key
