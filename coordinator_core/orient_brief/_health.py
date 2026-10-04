"""
Health family of the rebuilt `orient-assemble brief`: the machine and repo health
probes (R0 REQ-H1..H11). Every probe answers one operator question and either emits
nothing (healthy, or not applicable at this cadence) or one directive / judgment
point. The family is read-only: a needed heal is a `directives[]` entry naming the
existing repairing CLI, never an in-op write.

Cost contract (DR-344): zero spawns except two named, justified ones. The machine-local
registry is read once per call from `machine_resolver.merged_flat_registry()` and
threaded to every probe that needs it; nothing here calls `machine-local`.
  * REQ-H6 week leg: one `git rev-list --count <reset-sha>..HEAD`. The commit distance
    exists only in the commit graph, and `cat-file -e` ahead of it is folded into the
    same call (a sha that does not resolve fails it, and reads UNKNOWN).
  * REQ-H5: when, and only when, the repo registers a post-ceremony command, the
    registered command is run through `coordinator-ceremony-hook.py` (a repo-declared
    command is the one thing no in-process read can answer).

Reuse over reimplementation: REQ-H1/H2/H4 call the existing zero-spawn detectors in
`workday-start-health-probes.py` (`cmd_claude_klabauter_bin_sentinel`,
`cmd_working_repo_registration`, `cmd_git_perf_currency`) in process and carry their
stderr as the directive detail. REQ-H3 is `cmd_hook_currency --check-only`'s contract
(one walk, check only, a walk that could not run reads as stale) run through
`git_hook_install.ensure_hooks_fleet(check_only=True, registry=...)`; the bin command
itself is not called because it resolves the registry per key through `machine-local`.

Negative spec:
  * Never `git_perf_config.apply_fleet` (134 spawns even with `dry_run=True`).
  * Never `drift.check_plugin` at any setting (`_check_default` spawns git per mirror);
    REQ-H8 is the sentinel + refresh-log predicate, and its directive names the full walk.
  * Never `git_maintenance.run_tier`, `workweek_trail_scope.main`, or
    `check_weekly_staleness.main`: each spawns git or writes.
  * Never a worktree `status`/`diff` read (REQ-B3 is retired).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.contract.decision_object.reader_result import (
    ReaderResult,
    build_directive as _directive,
    truncate_external_text,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_BIN = _ENGINE_ROOT / "coordinator" / "bin"

_PROBES_CLI = "workday-start-health-probes"

#: Hook of the per-cadence ceremony whose post-command REQ-H5 surfaces.
_CEREMONY_BY_CADENCE = {
    "session": "workstream-start",
    "day": "workday-start",
    "week": "workweek-start",
}

_MARKER_FILENAME = ".workday-start-marker"
_STALE_VERDICTS = ("STALE", "MILD")

#: Record types a goal's `origin_goal_id` coverage is counted over.
_COVERAGE_TYPES = ("handoff", "plan", "debt", "bug", "improvement")

_HEADER_REL = "state/week-changelog/HEADER.md"

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

#: Bytes read from the head of a record when asking "does this file mention a stub
#: pair". Frontmatter longer than this is read in full by `_frontmatter_bytes`.
_HEAD_BYTES = 8192


@dataclass(frozen=True)
class _Ctx:
    cadence: str
    root: Path
    flat: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

_BIN_MODULES: dict[str, Any] = {}


@contextlib.contextmanager
def _bin_on_path():
    """`coordinator/bin` and `coordinator/bin/lib` importable for the call.

    The bin scripts are run as scripts in production, with their own directory on
    `sys.path`; called in process they need the same for their `import lib` and
    `bin/lib` sibling imports. Restored on exit so the shared path is not left grown.
    """
    wanted = [str(_BIN), str(_BIN / "lib")]
    added = [p for p in wanted if p not in sys.path]
    sys.path[:0] = added
    try:
        yield
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


def _load_bin(relpath: str, modname: str):
    """A bin script loaded by path (hyphenated names are not importable)."""
    mod = _BIN_MODULES.get(relpath)
    if mod is None:
        spec = importlib.util.spec_from_file_location(modname, _BIN / relpath)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {_BIN / relpath}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BIN_MODULES[relpath] = mod
    return mod


def _run_probe_cmd(fn_name: str, argv: list[str]) -> tuple[int, str]:
    """`(exit code, stderr text)` of one in-process `workday-start-health-probes` subcommand."""
    probes = _load_bin("workday-start-health-probes.py", "workday_start_health_probes")
    buf = io.StringIO()
    with _bin_on_path(), contextlib.redirect_stderr(buf):
        rc = getattr(probes, fn_name)(argv)
    return rc, buf.getvalue().strip()


def _stderr_probe(
    fn_name: str, argv: list[str], id_: str, args: list[str]
) -> list[dict[str, Any]]:
    rc, detail = _run_probe_cmd(fn_name, argv)
    if rc == 0:
        return []
    return [_directive(id_, _PROBES_CLI, args, detail)]


def _state_root(flat: Mapping[str, Any]) -> Optional[str]:
    """The coordinator state root, resolved as the weekly-staleness resolver does.

    Deliberately NOT the target root (REQ-H6): the cwd's git toplevel, or, when that is
    the `~/.claude` meta-repo home, the engine repo's `state/`. The engine root comes
    from the registry the caller already holds, so no `machine-local` spawn.
    """
    from coordinator_core.git.repo_root import show_toplevel
    from coordinator_core.ops import check_weekly_staleness as cws

    override = os.environ.get("CWS_TEST_STATE_ROOT")
    if override:
        return override
    git_root = show_toplevel(None)
    if not git_root:
        return None
    if cws._same_path(git_root, cws._claude_home()):
        claude_klabauter = str(flat.get("repos.claude_klabauter") or "").strip()
        return str(Path(claude_klabauter) / "state") if claude_klabauter else None
    return os.path.join(git_root, "state")


# ---------------------------------------------------------------------------
# Probes, in R0 order
# ---------------------------------------------------------------------------

def _claude_klabauter_bin_sentinel(ctx: _Ctx) -> ReaderResult:
    """REQ-H1: is the engine repo's `coordinator/bin/` a populated checkout?"""
    return ReaderResult(directives=_stderr_probe(
        "cmd_claude_klabauter_bin_sentinel", [], "d-claude-klabauter-bin-sentinel", ["claude-klabauter-bin-sentinel"]
    ))


def _working_repo_registration(ctx: _Ctx) -> ReaderResult:
    """REQ-H2: is `engine.working_repos.claude_klabauter` registered at this engine root?

    The bare detector is identity-gated and never raises; only the bare form runs here,
    the directive names the `--fix` form.
    """
    return ReaderResult(directives=_stderr_probe(
        "cmd_working_repo_registration",
        [],
        "d-working-repo-registration",
        ["working-repo-registration", "--fix"],
    ))


def _hook_walk_detail(ctx: _Ctx) -> str:
    """Stale-hook report of one check-only fleet walk; empty when the fleet is current.

    Same contract as `cmd_hook_currency --check-only`: any report on stderr is staleness,
    and a walk that could not run is staleness too, never a clean fleet.
    """
    buf = io.StringIO()
    try:
        git_hook_install = _load_bin("lib/git_hook_install.py", "git_hook_install")
        with _bin_on_path(), contextlib.redirect_stderr(buf):
            git_hook_install.ensure_hooks_fleet(
                str(_BIN), check_only=True, registry=ctx.flat
            )
    except Exception as exc:  # noqa: BLE001 - a walk that cannot run is stale, not clean
        return f"hook-currency: COULD NOT RUN the fleet hook heal: {exc}"
    return buf.getvalue().strip()


def _hook_currency(ctx: _Ctx) -> ReaderResult:
    """REQ-H3: are the coordinator git hooks stale in any registered repo?

    Check only. An author or default box gets the repairing bare form as a directive; a
    consumer box gets a judgment point instead, because repair rewrites hooks the
    operator owns.
    """
    detail = _hook_walk_detail(ctx)
    if not detail:
        return ReaderResult()
    from coordinator_core.machine_profile import machine_profile

    if machine_profile() == "consumer":
        return ReaderResult(judgment_points=[build_judgment_point(
            None,
            id="j-hook-currency-repair",
            question="Coordinator git hooks are stale in the fleet registry. Repair them now?",
            dispositions=[
                build_disposition("repair_hooks_now"),
                build_disposition("defer"),
            ],
            evidence=truncate_external_text(detail),
            reason="recommendation-forbidden",
        )])
    return ReaderResult(directives=[
        _directive("d-hook-currency", _PROBES_CLI, ["hook-currency"], detail)
    ])


def _git_perf_currency(ctx: _Ctx) -> ReaderResult:
    """REQ-H4: is `core.untrackedCache` fleet-current across registered repos?

    Fires at every cadence, which keeps the heal window `ops/configure_git.py` describes
    bounded to the next workday-start. Detector only: `cmd_git_perf_currency` reads
    `.git/config` as text, so no `git config` spawn and no `apply_fleet`.
    """
    return ReaderResult(directives=_stderr_probe(
        "cmd_git_perf_currency",
        [],
        "d-git-perf-currency",
        ["git-perf-currency", "--fix"],
    ))


def _ceremony_hook(ctx: _Ctx) -> ReaderResult:
    """REQ-H5: the repo's registered post-ceremony command output, verbatim.

    Silent, and zero-spawn, when no command is registered for this cadence's ceremony.
    """
    from coordinator_core.resolve_validation_cmd import read_local_md_key
    from coordinator_core.win_portability import no_console_creationflags

    ceremony = _CEREMONY_BY_CADENCE[ctx.cadence]
    key = ceremony.replace("-", "_") + "_post_command"
    if not read_local_md_key(str(ctx.root), key):
        return ReaderResult()
    # The hook CLI answers an unknown ceremony with a warning and no output, so spawning it
    # for one (workstream-start is not a hook ceremony) buys a process and no directive.
    hook = _load_bin("coordinator-ceremony-hook.py", "coordinator_ceremony_hook")
    if ceremony not in hook._KNOWN_CEREMONIES:
        return ReaderResult()
    try:
        proc = subprocess.run(
            [sys.executable, str(_BIN / "coordinator-ceremony-hook.py"), ceremony],
            capture_output=True,
            text=True,
            cwd=str(ctx.root),
            timeout=10,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return ReaderResult(directives=[_directive(
            "d-ceremony-hook-output", _PROBES_CLI, ["ceremony-hook", ceremony],
            f"{ceremony} post-command did not finish within 10s; run it directly.",
        )])
    out = (proc.stdout or "").rstrip("\n")
    if not out:
        return ReaderResult()
    return ReaderResult(directives=[
        _directive("d-ceremony-hook-output", _PROBES_CLI, ["ceremony-hook", ceremony], out)
    ])


def _week_staleness(state_root: str) -> str:
    """STALE / MILD / FRESH / UNKNOWN for `<state>/week-changelog/HEADER.md`.

    Same thresholds and parse as `check_weekly_staleness`, with one spawn where it runs
    two: `git rev-list --count <sha>..HEAD` already fails for a sha that does not
    resolve, which is the verdict `cat-file -e` bought.
    """
    from datetime import date, datetime

    from coordinator_core.ops import check_weekly_staleness as cws
    from coordinator_core.win_portability import leaf_spawn_creationflags

    header = Path(state_root) / "week-changelog" / "HEADER.md"
    try:
        text = header.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "UNKNOWN"
    reset_sha = cws._extract_reset_sha(text)
    week_line = cws._extract_week_start(text)
    m = cws._DATE_EXTRACT_RE.search(week_line)
    if not reset_sha or not m or "not yet set" in week_line:
        return "UNKNOWN"
    try:
        week_start = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return "UNKNOWN"
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", f"{reset_sha}..HEAD"],
            capture_output=True,
            text=True,
            cwd=state_root,
            timeout=5,
            **leaf_spawn_creationflags(),
        )
        commits = int(proc.stdout.strip()) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        commits = None
    if commits is None:
        return "UNKNOWN"
    days_crossed = max((date.today() - week_start).days, 0) >= cws.DAY_THRESHOLD
    commits_crossed = commits >= cws.COMMIT_THRESHOLD
    if days_crossed and commits_crossed:
        return "STALE"
    return "MILD" if days_crossed or commits_crossed else "FRESH"


def _marker_freshness(ctx: _Ctx) -> ReaderResult:
    """REQ-H6: is this cadence's freshness marker current?

    session: `state/.workday-start-marker` is not today and the repo is registered and
    onboarded -> a day-review-due judgment point. day: marker not today -> directive
    naming the writer. week: the week header's staleness is STALE or MILD -> reset or
    update-in-place judgment point. The state root is the weekly-staleness resolver's,
    not the target root's.
    """
    state_root = _state_root(ctx.flat)
    if not state_root:
        return ReaderResult()

    if ctx.cadence == "week":
        if not (Path(state_root) / "week-changelog" / "HEADER.md").is_file():
            return ReaderResult()
        verdict = _week_staleness(state_root)
        if verdict not in _STALE_VERDICTS:
            return ReaderResult()
        return ReaderResult(judgment_points=[build_judgment_point(
            None,
            id="j-week-marker-freshness",
            question=f"{_HEADER_REL} staleness={verdict} — reset for a new week or update in place?",
            dispositions=[
                build_disposition("reset_week"),
                build_disposition("update_in_place"),
            ],
            evidence=f"check_weekly_staleness verdict={verdict} | reason: reset-vs-update-in-place is a week-cadence PM/EM judgment call, never auto-resolved",
            reason="recommendation-forbidden",
        )])

    from coordinator_core.daily_day import local_day

    today = local_day(str(ctx.root))
    try:
        marker = (Path(state_root) / _MARKER_FILENAME).read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        marker = ""
    if marker == today:
        return ReaderResult()

    if ctx.cadence == "day":
        return ReaderResult(directives=[_directive(
            "d-workday-marker-write",
            "write-workday-start-marker",
            [],
            f"workday-start marker is {marker or 'absent'}, today is {today}",
        )])

    from coordinator_core.repo_standing import repo_standing

    standing = repo_standing(ctx.root)
    if standing.registered_key is None or not standing.onboarded:
        return ReaderResult()
    return ReaderResult(judgment_points=[build_judgment_point(
        None,
        id="j-session-day-review-due",
        question="The day review (workday-start) has not run today. Run it now or defer?",
        dispositions=[
            build_disposition("run_workday_start_now"),
            build_disposition("defer"),
        ],
        evidence=f"{_MARKER_FILENAME} is {marker or 'absent'}, today is {today}",
        reason="recommendation-forbidden",
    )])


def _frontmatter_bytes(path: str) -> Optional[bytes]:
    """The file's bytes through the end of its frontmatter (whole file if unterminated).

    One short read in the common case; a frontmatter longer than `_HEAD_BYTES` is read on.
    None on `OSError`.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
            if head.startswith(b"---") and b"\n---" not in head[3:]:
                head += fh.read()
            return head
    except OSError:
        return None


# path -> [(mtime_ns, size), head bytes or None, parsed `_stub_record` or None].
# Process-lifetime: the brief is read-only (REQ-C10), so the reuse is the warm
# engine's, never a file. A head is kept only when it names a stub at all, and is
# parsed only when it names a live one, so a cold call parses a handful of records.
_STUB_MEMO: dict = {}


def _stub_record(raw: bytes) -> Any:
    """The stub-relevant fields of one record, or 0 when its frontmatter is unreadable."""
    from coordinator_core.dag import _parse_frontmatter
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops import origin_stub_staleness as oss

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return 0
    if split_frontmatter(text) is None:
        return 0
    meta = _parse_frontmatter(text)
    own = oss.read_pair(meta)
    return {
        "own": tuple(own) if own else None,
        "closes": [tuple(p) for p in oss.read_closes_stubs(meta)],
        "state": meta.get("deployment_state"),
        "status": meta.get("status"),
        "baton": bool(oss.is_baton_kind(meta.get("kind"))),
    }


def _parsed(entry: list) -> Any:
    if entry[2] is None:
        entry[2] = _stub_record(entry[1])
    return entry[2]


def _stub_heads(trees: tuple) -> dict:
    """path -> memo entry for every `.md` in `trees` whose head names a stub, revalidated
    by `(mtime_ns, size)`. The signature comes free with `os.scandir` on Windows, so a
    warm call over an unchanged corpus opens nothing."""
    from coordinator_core.ops import origin_stub_staleness as oss

    out: dict = {}

    def visit(entry: os.DirEntry) -> None:
        st = entry.stat()
        sig = (st.st_mtime_ns, st.st_size)
        hit = _STUB_MEMO.get(entry.path)
        if hit is None or hit[0] != sig:
            raw = _frontmatter_bytes(entry.path)
            if raw is not None and oss._PREFILTER_STUB not in raw and oss._PREFILTER_CLOSES not in raw:
                raw = None
            hit = _STUB_MEMO[entry.path] = [sig, raw, None]
        if hit[1] is not None:
            out[entry.path] = hit

    def walk(base: str, recursive: bool) -> None:
        try:
            it = os.scandir(base)
        except OSError:
            return
        with it:
            for e in it:
                if e.is_file() and e.name.endswith(".md"):
                    visit(e)
                elif recursive and e.is_dir():
                    walk(e.path, True)

    for base, recursive in trees:
        walk(str(base), recursive)
    return out


def _stale_origin_stubs(ctx: _Ctx) -> ReaderResult:
    """REQ-H7 (day): live origin stubs whose `(roadmap_id, stub_id)` pair a shipping record carries.

    Verdicts are `origin_stub_staleness.survey`'s: a plan in `SPEC_RIPE_STATUSES` or a
    `shipped` handoff carrying the pair, other than the stub itself. Heads come from
    `_stub_heads`, so a warm call opens only records changed since the last.
    """
    from coordinator_core.lifecycle_constants import SPEC_RIPE_STATUSES
    from coordinator_core.ops import origin_stub_staleness as oss

    root = ctx.root
    handoffs = str(root / oss._HANDOFFS_ROOT)
    trees = (
        (handoffs, False, "handoff"),
        (str(root / "archive" / "handoffs"), True, "handoff"),
        (str(root / "docs" / "plans"), False, "plan"),
        (str(root / "archive" / "specs"), True, "plan"),
    )
    heads = _stub_heads(tuple((b, r) for b, r, _k in trees))

    def rel(p: str) -> str:
        return Path(p).relative_to(root).as_posix()

    live: list[tuple[str, tuple[str, str], str]] = []
    for path, entry in heads.items():
        if os.path.dirname(path) != handoffs:
            continue
        rec = _parsed(entry)
        if rec and rec["own"] and rec["baton"] and rec["state"] in oss._LIVE_STATES:
            live.append((rel(path), rec["own"], rec["state"]))
    if not live:
        return ReaderResult()

    wanted = {pair for _r, pair, _s in live}
    id_re = re.compile(b"|".join(sorted({re.escape(p[1].encode("utf-8")) for p in wanted})))
    evidence: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for path, entry in heads.items():
        if id_re.search(entry[1]) is None:
            continue
        rec = _parsed(entry)
        if not rec:
            continue
        kind = next(k for b, _r, k in trees if path.startswith(b))
        if kind == "plan":
            shipped = rec["status"] in SPEC_RIPE_STATUSES
        else:
            shipped = rec["state"] == "shipped"
        if not shipped:
            continue
        pairs = list(rec["closes"])
        if rec["own"]:
            pairs.append(rec["own"])
        for pair in pairs:
            if pair in wanted:
                evidence.setdefault(pair, []).append((rel(path), kind))

    stale: list[tuple[str, str, str, str, str]] = []
    for stub_rel, pair, state in live:
        carriers = [e for e in sorted(evidence.get(pair, ())) if e[0] != stub_rel]
        if carriers:
            stale.append((stub_rel, f"{pair[0]}/{pair[1]}", state, carriers[0][1], carriers[0][0]))
    if not stale:
        return ReaderResult()

    lines = [
        f"{path} ({pair}, {state}) <- {kind} {ev}" for path, pair, state, kind, ev in stale
    ]
    return ReaderResult(judgment_points=[build_judgment_point(
        {
            "disposition": "close_stubs",
            "rationale": "a shipping record already carries each stub's pair; "
            "closing is the recorded remedy (handoff.close_origin_stub)",
        },
        id="j-stale-origin-stubs",
        question=f"{len(stale)} live origin stub(s) already have a shipping record. Close them?",
        dispositions=[build_disposition("close_stubs"), build_disposition("leave_as_is")],
        evidence=truncate_external_text("; ".join(lines)),
        reason="the shipping record may not be the stub's own; closing is the EM's confirmation",
        reportable=True,
    )])


def _plugin_mirrors(flat: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    prefix = "plugin.mirrors."
    mirrors: dict[str, dict[str, str]] = {}
    for key, val in flat.items():
        if not key.startswith(prefix):
            continue
        name, _, field = key[len(prefix):].partition(".")
        if name and field:
            mirrors.setdefault(name, {})[field] = "" if val is None else str(val).strip()
    return mirrors


def _plugin_drift(ctx: _Ctx) -> ReaderResult:
    """REQ-H8 (day): has a `copy_install` plugin mirror drifted?

    A deliberately weaker, pure-file-read check than the full `check-plugin-drift` walk:
    the live install's `version.txt` sentinel is absent or not a 40-hex sha, or the source
    `pyproject.toml` hash differs from the refresh-log baseline. It can miss a live-tree
    edit that leaves both intact; the directive names the full walk.
    """
    from coordinator_core.plugin_health import refresh_log as drift

    mirrors = _plugin_mirrors(ctx.flat)
    refresh_log = drift.resolve_refresh_log()
    found: list[str] = []
    for name in sorted(mirrors):
        m = mirrors[name]
        if m.get("propagation_mode") != "copy_install":
            continue
        legs: list[str] = []
        live = Path(m.get("live_path", "").replace("\\", "/") or os.devnull)
        sentinel = live / "version.txt"
        try:
            sha = sentinel.read_text(encoding="utf-8", errors="replace").strip("\r\n")
        except OSError:
            sha = None
        if sha is None:
            legs.append("sentinel missing")
        elif not _SHA40_RE.match(sha):
            legs.append("sentinel malformed")
        source = m.get("source_path", "")
        pyproject = Path(source.replace("\\", "/")) / "pyproject.toml" if source else None
        if pyproject is not None and pyproject.is_file():
            try:
                current = drift.pyproject_hash(pyproject)
            except OSError:
                current = ""
            baseline = drift.refresh_log_baseline_hash(refresh_log, name) if current else ""
            if baseline and baseline != current:
                legs.append("pyproject changed since last refresh")
        if legs:
            found.append(f"{name} ({', '.join(legs)})")
    if not found:
        return ReaderResult()
    return ReaderResult(directives=[_directive(
        "d-plugin-drift",
        "check-plugin-drift",
        [],
        "plugin mirror drift: " + "; ".join(found) + " -- full walk: check-plugin-drift",
    )])


def _git_maintenance(ctx: _Ctx) -> ReaderResult:
    """REQ-H9 (day, week): is git-maintenance liveness stale or never stamped for this repo?

    Reads the stamp only; never runs a maintenance tier. The never-stamped and stale
    states are distinct and both fire, because "maintenance never ran" and "ran and is
    fine" differ only on the stamp.
    """
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    tier = {"day": "daily", "week": "weekly"}[ctx.cadence]
    status = hl.liveness_status(str(ctx.root), [hl.GIT_MAINTENANCE])[hl.GIT_MAINTENANCE]
    if status == hl.STATUS_FRESH:
        return ReaderResult()
    detail = (
        "git maintenance has never been stamped for this repo"
        if status == hl.STATUS_NEVER_STAMPED
        else "git maintenance liveness is stale for this repo"
    )
    return ReaderResult(directives=[_directive(
        f"d-git-maintenance-{tier}", "coordinator-git-maintenance", [tier], detail
    )])


def _goal_coverage(ctx: _Ctx) -> ReaderResult:
    """REQ-H10 (week): which active goals have zero `origin_goal_id` coverage?

    One pass per record type with a bytes prefilter, instead of the CLI's one records
    query per goal per type through the door. A failed or empty goal enumeration is
    silent, never a false all-clear and never a crash.
    """
    from coordinator_core.ops.ceremony.records_query import query_records
    from coordinator_core.ops.records_query import _collect_files, _load_record

    root = ctx.root
    goals = query_records("goal", root, where="status=active")
    goal_ids = sorted({
        str(g["frontmatter"].get("id") or "")
        for g in goals
        if g.get("frontmatter")
    } - {""})
    if not goal_ids:
        return ReaderResult()

    covered: set[str] = set()
    for record_type in _COVERAGE_TYPES:
        for fpath in _collect_files(root, record_type):
            raw = _frontmatter_bytes(str(fpath))
            if raw is None or b"origin_goal_id" not in raw:
                continue
            rec = _load_record(fpath, root, record_type)
            tagged = (rec or {}).get("frontmatter", {}).get("origin_goal_id")
            if isinstance(tagged, str):
                tagged = [tagged]
            if isinstance(tagged, list):
                covered.update(str(t) for t in tagged)
    zero = [gid for gid in goal_ids if gid not in covered]
    if not zero:
        return ReaderResult()
    return ReaderResult(directives=[_directive(
        "d-goal-coverage",
        "goal-coverage-scan",
        ["--format", "text"],
        "zero-coverage active goal(s): " + ", ".join(zero),
    )])


def _trail_scope(ctx: _Ctx) -> ReaderResult:
    """REQ-H11 (week): does the workweek review-trail scope resolve?

    Silent with no session id. The header path is `$HEADER_FILE` or the cwd-relative
    default, as `workweek-trail-scope` resolves it (R0 finding F6: not the target root).
    Never writes the trail shard: only the session and week-start resolution is reused,
    never `workweek_trail_scope.main`.
    """
    from coordinator_core.ops import workweek_trail_scope as wts

    if not wts._resolve_session_id():
        return ReaderResult()
    header = Path(os.environ.get("HEADER_FILE", _HEADER_REL))
    if not header.is_file():
        detail = f"{header} not found -- run /workweek-start to initialise"
    elif not wts._parse_week_start(header):
        detail = f"cannot parse 'Week starting:' YYYY-MM-DD from {header}"
    else:
        return ReaderResult()
    return ReaderResult(directives=[
        _directive("d-workweek-trail-scope", "workweek-trail-scope", [], detail)
    ])


# (name, probe, cadences it runs at)
_ALL = ("session", "day", "week")
_PROBES: tuple[tuple[str, Callable[[_Ctx], ReaderResult], tuple[str, ...]], ...] = (
    ("REQ-H1", _claude_klabauter_bin_sentinel, _ALL),
    ("REQ-H2", _working_repo_registration, _ALL),
    ("REQ-H3", _hook_currency, _ALL),
    ("REQ-H4", _git_perf_currency, _ALL),
    ("REQ-H5", _ceremony_hook, _ALL),
    ("REQ-H6", _marker_freshness, _ALL),
    ("REQ-H7", _stale_origin_stubs, ("day",)),
    ("REQ-H8", _plugin_drift, ("day",)),
    ("REQ-H9", _git_maintenance, ("day", "week")),
    ("REQ-H10", _goal_coverage, ("week",)),
    ("REQ-H11", _trail_scope, ("week",)),
)


def collect(cadence: str, *, repo_root: Path) -> ReaderResult:
    from coordinator_core.machine_resolver import merged_flat_registry

    ctx = _Ctx(cadence=cadence, root=Path(repo_root), flat=merged_flat_registry())
    directives: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for req, probe, cadences in _PROBES:
        if cadence not in cadences:
            continue
        try:
            result = probe(ctx)
        except Exception as exc:  # noqa: BLE001 - one broken probe must not blind the rest
            print(
                f"orient-assemble: health probe {req} failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        directives.extend(result.directives)
        points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=points)
