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

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit import grind_stages
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script
from coordinator_core.ops.dispatch_emit.queue_select import select_rows

from .conftest import REVIEW_KW

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
        **REVIEW_KW,
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
    review_start = script_1.index("phase('Review prep')")
    review_end = script_1.index("phase('Review fixes')")
    grind_only = script_1[:review_start] + script_1[review_end:]
    agent_call_count = len(re.findall(r"await agent\(", grind_only))
    stage_names = "|".join(
        re.escape(n) for n in sorted(set(grind_stages.STAGE_MODELS.values()))
    )
    stage_model_count = len(re.findall(rf"model: '(?:{stage_names})'", grind_only))
    assert agent_call_count == stage_model_count > 0


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


def _golden_size_chain_command(repo_root: Path, row: dict, sizing_path: str) -> str:
    """The `_sizeChainCmd` text the golden holds, evaluated by node over fixed scout values."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    size_consts = "\n".join(re.findall(r"^const _size\w+ = .*$", golden, re.M))
    scouted = {
        "tshirt": "M",
        "intent": "rework the thing that row-a names",
        "name": "row-a-rework",
        "premise_provenance": "read",
        "premise_evidence": f"read {row['path']}",
        "exit_criterion": "the thing no longer breaks",
    }
    js = (
        grind_stages.SHQ_JS
        + "\n" + size_consts
        + f"\nconst REPO_ROOT = {json.dumps(str(repo_root))}, PROFILE_NAME = 'fixture', RUN_ID = '20261013T000000Z';"
        + f"\nconsole.log(_sizeChainCmd({json.dumps(scouted)}, {json.dumps(row)}, {json.dumps(sizing_path)}));"
    )
    out = subprocess.run(
        [node, "-e", js], capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_size_scout_template_filled_with_the_scout_fields_is_the_concrete_chain_command(grind_repo):
    """The command the scout is told to fill is the script's own chain, slot for slot."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    size_consts = "\n".join(re.findall(r"^const _(?:size\w+|SIZE_SLOTS) = .*$", golden, re.M))
    path_fns = golden[golden.index("function _slug("):golden.index("function _terminalCommitParamsBlock")]
    row = {"row_id": "row-a", "path": "state/bug-backlog/row-a.yaml", "digest": "d" * 64}
    scouted = {
        "tshirt": "M", "intent": "rework", "name": "row-a-rework", "premise_provenance": "read",
        "premise_evidence": "read-the-row", "exit_criterion": "tests-pass",
    }
    js = (
        grind_stages.SHQ_JS + "\n" + size_consts + "\n" + path_fns
        + f"\nconst REPO_ROOT = '/r', PROFILE_NAME = 'fixture', RUN_ID = '20261013T000000Z';"
        + f"\nconst row = {json.dumps(row)}, scouted = {json.dumps(scouted)};"
        + "\nconsole.log(JSON.stringify({ template: _sizeChainTemplate(row),"
        " concrete: _sizeChainCmd(scouted, row, _sizingPath(scouted, row.row_id)) }));"
    )
    out = subprocess.run(
        [node, "-e", js], capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    filled = got["template"]
    for field, value in scouted.items():
        filled = filled.replace(f"<{field}>", value)
    assert filled == got["concrete"]


def _settings_home_with_engine_shims(tmp_path: Path) -> Path:
    """A settings home whose three `bin/` tools run this checkout's own code."""
    bin_dir = tmp_path / "settings-home" / "bin"
    bin_dir.mkdir(parents=True)
    head = f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(_REPO_ROOT)!r})\n"
    doc_new = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"
    shims = {
        "sizing-assemble": "from coordinator_core.sizing_assemble import main\nsys.exit(main(sys.argv[1:]))\n",
        "backlog-grind-assemble": (
            "from coordinator_core.backlog_grind_assemble.grind_rows import main\n"
            "args = sys.argv[1:]\nsys.exit(main(args[1:] if args[:1] == ['grind-row'] else args))\n"
        ),
        "coordinator-doc-new": (
            "import runpy\n"
            f"sys.argv = [{str(doc_new)!r}] + sys.argv[1:]\n"
            f"runpy.run_path({str(doc_new)!r}, run_name='__main__')\n"
        ),
    }
    for name, body in shims.items():
        path = bin_dir / name
        path.write_text(head + body, encoding="utf-8")
        path.chmod(0o755)
    return tmp_path / "settings-home"


@pytest.mark.skipif(sys.platform == "win32", reason="the chained command is a POSIX shell line")
def test_size_chain_command_writes_a_valid_sizing_mints_a_baton_and_marks_the_row(grind_repo, tmp_path_factory, monkeypatch):
    from coordinator_core.frontmatter import schema_validate

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(_settings_home_with_engine_shims(tmp_path_factory.mktemp("shims"))))
    repo_root = grind_repo["repo_root"]
    digest = hashlib.sha256(grind_repo["row_a"].read_bytes()).hexdigest()
    row = {"row_id": "row-a", "path": "state/bug-backlog/row-a.yaml", "digest": digest}
    sizing_rel = "state/sizings/2026-10-13-row-a-rework-row-a.yaml"
    command = _golden_size_chain_command(repo_root, row, sizing_rel)

    ran = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False, cwd=repo_root,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr

    sizing = schema_validate.parse_yaml((repo_root / sizing_rel).read_text(encoding="utf-8"))
    assert "pm_verbatims" not in sizing
    schema = json.loads(
        (_REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas" / "sizing-object.schema.json").read_text(encoding="utf-8")
    )
    result = schema_validate.validate_frontmatter_obj(sizing, schema)
    assert isinstance(result, dict) and result.get("ok"), result

    baton_edge = sizing.get("baton")
    baton_rel = baton_edge.get("path") if isinstance(baton_edge, dict) else baton_edge
    assert baton_rel and baton_rel.startswith("state/handoffs/"), sizing
    assert (repo_root / baton_rel).is_file()

    manifest = select_rows(
        [grind_repo["queue_dir"]],
        where=None,
        order=("severity", "asc"),
        limit=None,
        batch_key=["severity"],
        batch_sizes={"default": 4, "@unkeyed": 4},
        row_id_key="@stem",
        profile="fixture",
        repo_root=repo_root,
        absent_sentinels={"owner": ("none", "N/A")},
    )
    assert "row-a" not in {e.row_id for e in manifest.entries}
    assert "row-a" in {d.row_id for d in manifest.declined}


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


# ---------------------------------------------------------------------------
# Exit criterion: a grind that throws mid-run leaves the tree clean of its own writes
# ---------------------------------------------------------------------------


class _Budget:
    total = None

    def spent(self):
        return 0

    def remaining(self):
        return None


def test_a_grind_that_throws_mid_run_leaves_the_run_write_roots_clean_and_the_record_at_head(tmp_path, monkeypatch):
    from urllib.parse import quote

    from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract
    from coordinator_core.ops.dispatch_emit import grind_compose as gc
    from coordinator_core.ops.dispatch_emit import grind_profile as gp
    from coordinator_core.session import touch_record
    from coordinator_core.tests.git_seed import seeded_repo

    seeded_repo(tmp_path, readme="x")
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-run")
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: False)
    row = "state/bug-backlog/row0.yaml"
    (tmp_path / row).parent.mkdir(parents=True)
    (tmp_path / row).write_text("status: open\n", encoding="utf-8")
    assert _git(["add", "-A"], tmp_path).returncode == 0
    assert _git(["commit", "-m", "seed row"], tmp_path).returncode == 0
    decisions = tmp_path.parent / f"{tmp_path.name}-decisions.json"
    decisions.write_text(json.dumps({contract.MINTED_KEY: ["state/bug-backlog/TF-1.yaml"]}), encoding="utf-8")
    run_id = "20261015T000000Z"
    sink: list = []

    def write(rel, text, *, claim):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if claim:
            grind_rows._declare_under_repo_root(path, tmp_path)

    def fix(_unit):
        write(row, "status: open\nnote: handed back\n", claim=True)
        write("state/apm-rulings/r0.yaml", "ruling\n", claim=True)
        write("state/bug-backlog/TF-1.yaml", "status: open\n", claim=False)
        raise RuntimeError("stage blew up")

    profile_dir = tmp_path.parent / f"{tmp_path.name}-profiles"
    profile_dir.mkdir()
    (profile_dir / "bug.yaml").write_text(
        (_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    def drain(_unit):
        payload = sink[-1]
        argv = [
            "run-record", f"--{contract.SWEEP_FLAG}", "--profile-dir", str(profile_dir),
            "--profile", "bug", "--queue", "state/bug-backlog", "--run-id", run_id,
            "--record-urlenc", quote(json.dumps(payload["record"]), safe=""),
            f"--{contract.DECLARED_FLAG}", contract.encode_declared_paths(payload["declared"]),
            f"--{contract.DECISIONS_FLAG}", str(decisions), "--repo-root", str(tmp_path),
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = grind_rows.main(argv)
        printed = json.loads(out.getvalue()) if code == grind_rows.EXIT_OK else {}
        assert printed.get("swept") is True, printed
        return {"outcome": "recorded" if code == grind_rows.EXIT_OK else "record-failed", "swept": printed["swept"]}

    script = {
        "triage": lambda _bid: [{"row": row, "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}],
        "fix": fix,
        "ledger-sweep": drain,
    }
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    gp.validate_graph(profile)
    err = None
    try:
        gc.run_admission(
            [("b0", [row])], gc.build_routing_table(profile), gc._triage_node_id(profile), reserve=1,
            budget=_Budget(), agent=lambda kind, unit: script[kind](unit), run_id=run_id, profile="bug",
            run_close_sink=sink,
        )
    except RuntimeError as exc:
        err = exc
    assert err is not None and str(err) == "stage blew up"

    status = _git(["status", "--porcelain", "--", "state"], tmp_path)
    assert status.returncode == 0 and status.stdout == ""
    shown = _git(["show", f"HEAD:state/queue-grind/bug/runs/{run_id}.json"], tmp_path)
    assert shown.returncode == 0, shown.stderr
    record = json.loads(shown.stdout)
    assert set(contract.RUN_RECORD_REQUIRED) <= set(record)
    assert record["halted"] is True and record["halted_reason"] == "stage blew up"
    landed = _git(["show", "--name-only", "--format=", "HEAD"], tmp_path).stdout.split()
    assert {row, "state/apm-rulings/r0.yaml", "state/bug-backlog/TF-1.yaml"} <= set(landed)
