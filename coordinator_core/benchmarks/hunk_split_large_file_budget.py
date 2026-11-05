"""Process-time falsifier for the hunk split and land_run on ~70k-line files.

One JSON row per axis: `{axis, process_time_ms, spawns, timed_out, ...}`. Process time is this
process plus descendants (`land_run_slow_disk_profile.measure`); wall clock is never a figure.

Axes (each in its own child process, so a quadratic pass is killed at `--cap-secs`, not awaited):
  hunk_stage.split:repetitive_rewritten    committed ~70k-line JSON map, rewritten by the run
  hunk_stage.split:repetitive_new          untracked at HEAD (baseline = an earlier draft), rewritten
  hunk_stage.split:unique_heavy            distinct float lines, many patience anchors (split only)
  unclaimed_dirt_stage.own_only_blobs      both repetitive files in one call
  dispatch.land_run                        the whole op, both repetitive files declared
  run_token.second_land_after_abandoned    a first land_run abandoned in a thread, its claim aged
                                           past the op budget, then a second land_run: `refusal` is
                                           the second call's `refused` key, None when it was served

The repetitive maps draw from a 12-value alphabet (most lines `    0.0,`), the shape of
metahuman-head-vertex-map.json, so difflib's matcher sees many equal lines. Fixtures are built
under a managed temp dir outside the measured window. A killed axis reports `timed_out: true` and
`process_time_ms` = the cap as a lower bound. Exit 0 when every axis is under `BAR_MS` and the
token axis is not refused `run-token-in-flight`, else 1.

    python coordinator_core/benchmarks/hunk_split_large_file_budget.py [--lines N] [--cap-secs S] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402

BAR_MS = 500.0
DEFAULT_LINES = 70_000
DEFAULT_CAP_SECS = 60.0
SETUP_SECS = 180.0
RESULT_MARK = "HUNK-SPLIT-RESULT "
MEASURING_FILE = "measuring"
IN_FLIGHT = "run-token-in-flight"

AXIS_SPLIT_REWRITTEN = "hunk_stage.split:repetitive_rewritten"
AXIS_SPLIT_NEW = "hunk_stage.split:repetitive_new"
AXIS_SPLIT_UNIQUE = "hunk_stage.split:unique_heavy"
AXIS_OWN_ONLY = "unclaimed_dirt_stage.own_only_blobs"
AXIS_LAND = "dispatch.land_run"
AXIS_TOKEN = "run_token.second_land_after_abandoned"
AXES = (AXIS_SPLIT_REWRITTEN, AXIS_SPLIT_NEW, AXIS_SPLIT_UNIQUE, AXIS_OWN_ONLY, AXIS_LAND, AXIS_TOKEN)

SELF = "5e1f0000-0000-4000-8000-0000000000c4"
RUN = "ask-20261103T100000-c0c0c0"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-c0c0c0"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
REWRITTEN = "data/metahuman-head-vertex-map.json"
NEW = "data/avatarsdk-head-vertex-map.json"
UNIQUE = "data/unique-heavy-map.json"
TARGETS = (REWRITTEN, NEW)
VALUES = ("    0.0,\n",) * 8 + ("    1.0,\n", "    -1.0,\n", "    0.5,\n", "    0.25,\n")
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
FALLBACK_BUDGET_SECS = 30.0


def repetitive_map(seed: int, lines: int) -> str:
    """A JSON-shaped vertex map: fixed head and tail, `lines` value lines drawn from `VALUES`."""
    rng = random.Random(seed)
    body = [rng.choice(VALUES) for _ in range(lines - 1)] + ["    0.0\n"]
    return '{\n  "vertices": [\n' + "".join(body) + "  ]\n}\n"


def unique_map(rewrite: bool, lines: int) -> str:
    """Distinct float lines; `rewrite` replaces every 11th line (and the first and last) with another
    distinct float, so the changed middle is the whole file and every other line is a patience anchor."""
    rows = []
    for i in range(lines):
        edited = rewrite and (i % 11 == 0 or i == lines - 1)
        rows.append(f"    {i * 0.0173 + (9000.5 if edited else 0.5):.6f},\n")
    return '{\n  "vertices": [\n' + "".join(rows) + "  ]\n}\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _git(root: Path, *args: str) -> str:
    from coordinator_core.benchmarks.land_run_slow_disk_profile import _git as git

    return git(list(args), root)


def build_fixture(workdir: Path, lines: int, *, with_unique: bool) -> dict:
    """A repo whose worktree diff carries two repetitive maps the run rewrote, and, with `with_unique`,
    one unique-heavy map (split-only: it is no landing chunk path). `REWRITTEN` is committed;
    `NEW` is untracked at HEAD and its baseline is an earlier draft. Returns `{root, baseline, token}`;
    the token is minted for the manifest-declared landing of `TARGETS`."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit import write_baseline as wb
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.session import core as session_core
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record

    root = workdir / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(root, "config", key, value)
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(root, REWRITTEN, repetitive_map(1, lines))
    if with_unique:
        _write(root, UNIQUE, unique_map(False, lines))
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XS\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship it\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: X1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    _write(root, NEW, repetitive_map(2, lines))
    paths = [*TARGETS, *([UNIQUE] if with_unique else [])]
    baseline = wb.capture(root, paths)

    os.environ["COORDINATOR_SESSION_ID"] = SELF
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    session_core.init(SELF, cwd=str(root))
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    for rel, text in ((REWRITTEN, repetitive_map(3, lines)), (NEW, repetitive_map(4, lines))):
        _write(root, rel, text)
        session_scope.touch(SELF, rel, cwd=str(root))
        touch_record.append_event(
            touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF),
            session_id=SELF, agent_id=None, verb="T", path=rel,
            content_hash=touch_record.compute_content_hash(root / rel), kind="w",
        )
    if with_unique:
        _write(root, UNIQUE, unique_map(True, lines))

    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=TARGETS, report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        write_baseline={p: baseline[p] for p in TARGETS},
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    token = run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)
    return {"root": root, "baseline": baseline, "token": token}


def call_land(root: Path, token: str) -> dict:
    """One product-mode `dispatch.land_run` of `TARGETS` for the fixture's minted run."""
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    slice_files = list(TARGETS)
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": slice_files, "foreign_claims": [], "slice_files": slice_files},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review,
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _op_budget_secs() -> float:
    try:
        from coordinator_core import ipc

        return float(ipc._timeout_for("dispatch.land_run"))
    except Exception:  # noqa: BLE001 -- the budget is a fixture input, a fixed fallback keeps the axis runnable
        return FALLBACK_BUDGET_SECS


def _age_claim(root: Path, age_secs: float) -> bool:
    """Rewrite the claimed run-token record's `claimed_at` to `age_secs` ago; False when no claim."""
    from coordinator_core.ops.dispatch_emit import run_token

    path = run_token._record_path(root, RUN, claimed=True)
    if path is None:
        return False
    record = run_token._read(path)
    if record is None:
        return False
    record["claimed_at"] = time.time() - age_secs
    if "deadline" in record:
        record["deadline"] = time.time() - 1.0
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return True


def _token_axis(root: Path, token: str, measure) -> dict:
    """First land_run in a daemon thread (abandoned, as the warm dispatcher abandons an over-budget
    op), its claim aged past the op budget, then the measured second call."""
    threading.Thread(target=lambda: call_land(root, token), daemon=True).start()
    give_up = time.monotonic() + SETUP_SECS
    while not _age_claim(root, _op_budget_secs() + 1.0):
        if time.monotonic() > give_up:
            return {"axis": AXIS_TOKEN, "error": "first call never claimed the run token"}
        time.sleep(0.05)
    seen: dict = {}

    def second() -> dict:
        seen["result"] = call_land(root, token)
        return seen["result"]

    row = measure(second)
    result = seen.get("result") or {}
    row["refusal"] = result.get("refused") if isinstance(result, dict) else None
    row["first_claim_aged_secs"] = round(_op_budget_secs() + 1.0, 1)
    return row


def run_axis(axis: str, workdir: Path, lines: int) -> dict:
    """Build the axis fixture, mark the measured window start, measure once, return the row."""
    from coordinator_core.benchmarks.land_run_slow_disk_profile import measure as meter

    declare_benchmark_origin()
    fx = build_fixture(workdir, lines, with_unique=axis == AXIS_SPLIT_UNIQUE)
    root, baseline = fx["root"], fx["baseline"]

    def measure(call) -> dict:
        (workdir / MEASURING_FILE).write_text("1", encoding="utf-8")
        row = meter(axis, "", call, None, attribute=False)
        return {"axis": axis, "process_time_ms": row.process_time_ms, "spawns": row.spawns, "outcome": row.outcome}

    def split(path: str):
        from coordinator_core.ops.dispatch_emit import hunk_stage

        return lambda: hunk_stage.split(root, path, own_run_id=RUN, own_baseline_blob=baseline[path], foreign=[])

    if axis == AXIS_SPLIT_REWRITTEN:
        row = measure(split(REWRITTEN))
    elif axis == AXIS_SPLIT_NEW:
        row = measure(split(NEW))
    elif axis == AXIS_SPLIT_UNIQUE:
        row = measure(split(UNIQUE))
    elif axis == AXIS_OWN_ONLY:
        from coordinator_core.ops.dispatch_emit import unclaimed_dirt_stage

        row = measure(lambda: unclaimed_dirt_stage.own_only_blobs(root, RUN, list(TARGETS), {p: baseline[p] for p in TARGETS}))
    elif axis == AXIS_LAND:
        from coordinator_core.ops.dispatch_emit import land_run

        land_run._launch_close_job = lambda *a, **k: True
        _quiet_claims()
        row = measure(lambda: call_land(root, fx["token"]))
    elif axis == AXIS_TOKEN:
        from coordinator_core.ops.dispatch_emit import land_run

        land_run._launch_close_job = lambda *a, **k: True
        _quiet_claims()
        row = _token_axis(root, fx["token"], measure)
    else:
        raise SystemExit(f"unknown axis {axis!r}")
    row["lines"] = lines
    return row


def _quiet_claims() -> None:
    """The fixture session holds no other claims (the real lookup scans the live box)."""
    from coordinator_core.session import claims

    claims.list_claims_by_session_checked = lambda sid, cwd=None: ([], [])


def _child(axis: str, workdir: Path, lines: int) -> int:
    row = run_axis(axis, workdir, lines)
    sys.stdout.write(RESULT_MARK + json.dumps(row) + "\n")
    sys.stdout.flush()
    os._exit(0)  # the token axis leaves an abandoned thread running


def _spawn(axis: str, workdir: Path, lines: int, out: Path, err: Path) -> subprocess.Popen:
    argv = [sys.executable, str(Path(__file__).resolve()), "--axis", axis, "--workdir", str(workdir), "--lines", str(lines)]
    with open(out, "wb") as o, open(err, "wb") as e:
        return subprocess.Popen(argv, stdout=o, stderr=e, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def measure_axis(axis: str, base: Path, lines: int, cap_secs: float) -> dict:
    """Run `axis` in a child; kill it `cap_secs` after its measured window opens (`timed_out`)."""
    workdir = base / axis.replace(":", "-")
    workdir.mkdir(parents=True)
    out, err = workdir / "stdout.txt", workdir / "stderr.txt"
    proc = _spawn(axis, workdir, lines, out, err)
    started = time.monotonic()
    window = None
    timed_out = False
    while proc.poll() is None:
        now = time.monotonic()
        if window is None and (workdir / MEASURING_FILE).exists():
            window = now
        if window is not None and now - window > cap_secs:
            timed_out = True
            proc.kill()
            break
        if window is None and now - started > SETUP_SECS:
            proc.kill()
            return {"axis": axis, "error": f"fixture setup exceeded {SETUP_SECS:.0f}s", "lines": lines}
        time.sleep(0.05)
    proc.wait()
    if timed_out:
        return {
            "axis": axis, "process_time_ms": round(cap_secs * 1000.0, 1), "process_time_is_lower_bound": True,
            "spawns": None, "timed_out": True, "cap_secs": cap_secs, "lines": lines,
        }
    for line in reversed(out.read_text(encoding="utf-8", errors="replace").splitlines()):
        if line.startswith(RESULT_MARK):
            return {**json.loads(line[len(RESULT_MARK):]), "timed_out": False}
    tail = err.read_text(encoding="utf-8", errors="replace").strip()[-600:]
    return {"axis": axis, "error": f"child rc={proc.returncode} printed no result: {tail}", "lines": lines}


def over_bar(row: dict) -> bool:
    """True when a timing axis is killed, errored, or past `BAR_MS`; the token axis also when the
    second call is refused `run-token-in-flight`."""
    if row.get("error") or row.get("timed_out"):
        return True
    if row["axis"] == AXIS_TOKEN and row.get("refusal") == IN_FLIGHT:
        return True
    return float(row.get("process_time_ms") or 0.0) >= BAR_MS


def run(lines: int, cap_secs: float, axes: tuple = AXES) -> list:
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.temp_layout import managed_tempdir

    base = Path(tempfile.mkdtemp(prefix="hunk_split_budget_", dir=managed_tempdir()))
    # Both homes point inside `base`: land_run mints and spends run tokens and rewrites the live-run
    # index under the machine-local registry, and the real one is shared with every live session.
    homes = {"COORDINATOR_SETTINGS_HOME": str(base / "settings"), "CLAUDE_CONFIG_DIR": str(base / "config")}
    try:
        with mock.patch.dict(os.environ, homes):
            rows = [measure_axis(axis, base, lines, cap_secs) for axis in axes]
    finally:
        rmtree_or_raise(base, label="hunk_split_large_file_budget")
    for row in rows:
        row["over_bar"] = over_bar(row)
    return rows


def render(rows: list) -> str:
    out = [f"{'axis':42} {'proc_ms':>10} {'spawns':>6}  note"]
    for r in rows:
        note = "ERROR " + r["error"] if r.get("error") else ("TIMED-OUT " if r.get("timed_out") else ("OVER " if r["over_bar"] else "ok ")) + (r.get("outcome") or "")
        if r["axis"] == AXIS_TOKEN and not r.get("error"):
            note += f" refusal={r.get('refusal')}"
        out.append(f"{r['axis']:42} {r.get('process_time_ms') or 0.0:10.1f} {r.get('spawns') if r.get('spawns') is not None else '-':>6}  {note}")
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lines", type=int, default=DEFAULT_LINES, help="lines per map (default %(default)s)")
    ap.add_argument("--cap-secs", type=float, default=DEFAULT_CAP_SECS, help="kill an axis this long after its measured window opens")
    ap.add_argument("--axis", default=None, choices=AXES, help=argparse.SUPPRESS)
    ap.add_argument("--workdir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--only", action="append", choices=AXES, help="measure only this axis (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.axis:
        return _child(args.axis, Path(args.workdir), args.lines)
    rows = run(args.lines, args.cap_secs, tuple(args.only) if args.only else AXES)
    print(json.dumps(rows, indent=1) if args.json else render(rows))
    return 1 if any(r["over_bar"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
