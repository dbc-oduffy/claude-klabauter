"""mise-prep-run — gate and stamp a set of plans, and name each one's state.

WHY THIS EXISTS. mise-prep's two halves both shipped and neither drives the ceremony.
`plan.prep_gate` reports the authoring bar for ONE plan; `plan.stamp_prepped` writes the
attest for ONE plan and refuses unless the gate passes over the bytes it is about to
stamp. Between them, nothing — so certifying a landed wave means an EM reading the entry
query, picking plan paths out of it, and typing two op calls per plan. That is the same
retype `mise-prep-entry.py` removed at the plan-blitz seam, still standing one step later.

This is the driver: one call over a set, gate then stamp each, and one report that names
every plan's state with the repair that state routes to.

THE FOUR STATES ROUTE FOUR WAYS, which is why "not certified" is the wrong noun for three
of them and this module never prints it:

  CERTIFIED    the recomputed body sha matches the stamp. Nothing to do.
  STALE        stamped, but the body moved since — re-GATE, then re-stamp. A wave's
               reviewer rewrites plan bodies applying its own findings, so STALE is
               the ORDINARY state after a landing, not an anomaly.
  UNSTAMPED    never certified. Gate and stamp.
  MALFORMED    a hand-written stamp. Repair the frontmatter; do not stamp over it.

CENSUS LEG. After the seam pass and before any stamp, each plan this run is about to stamp (gate
PREPPED, not SEAM-REFUSED, not already CERTIFIED) has its `census[]` commands re-run through the
sibling `mise-census-revalidate` executor (`require_stamp=False`: the plan is not stamped yet). A
DRIFT refuses the stamp and reads CENSUS-DRIFT; MATCH, UNDECIDABLE, REFUSED and UNRUNNABLE do not
block, the same rule the fire-time leg uses. Other rows are never measured. It runs under
`--dry-run` too, being read-only.

WHAT IT DOES NOT DO. It does not decide what to fire, does not refresh a plan's recorded
`result:` values, and does not author anything into a plan beyond the `depends_on_plan`
edges `plan.seam_fix` writes. A plan that fails the bar is REPORTED with its failing
classes and the upgrade command that writes the declarations derivable from the plan's own text —
`mise-prep-upgrade` never invents one, and neither does this. `--upgrade` runs it for
you and re-gates; without the flag nothing under `docs/plans/` is written except an
attest the gate has just passed.

Usage:

    python3 mise-prep-run.py --repo-root <abs> [--upgrade] [--dry-run] [plan ...]

With no plan arguments the set is every APPROVED plan the engine's plan gate reports —
mise-prep's own entry set, taken from the same seam `mise-prep-entry.py` reads, never
re-derived from plan frontmatter.

Before any plan is gated, `plan.seam_fix` runs once over the set (`dry_run` under `--dry-run`),
minting the `depends_on_plan` edges that order in-set writes collisions. It is the one op here that
edits plan bodies, and it runs first so every stamp is computed over the edged body. Pairs it
cannot order without a cycle come back as `skippedCycles` on the report.

After every plan is gated, one seam check runs over the whole set (`plan.seam_record` at
`phase: prep`; `plan.seam_check` under `--dry-run`, which writes no sidecar). A plan the check
implicates is not stamped and reads SEAM-REFUSED with one `seam:` line per finding, each ending in
its route. An op error is a refusal (exit 2), never a pass.

RESIDUE WORKFLOW. After the report rows and the seam-fix reply exist (dry run or not),
`mise_residue.residue_args` sorts what stayed short: plans to author go to Sonnet
`coordinator:plan-author-residue` agents, up to 8 plans per agent; unscreenable census entries and
`skippedCycles` go to one `coordinator:apm` agent. When either list is non-empty the run binds
`workflows/mise-prep-residue.mjs` with those args (`workflow.bind_args`), validates it
(`workflow.validate`) and writes `state/mise-workflows/<runId>.residue.mjs` plus its receipt, then
prints a `Workflow({scriptPath})` line for the EM to fire (`--dry-run` still writes the script
and receipt: it withholds stamps, not files). Nothing is fired from here; `--json`
carries `residueWorkflow` (repo-relative path or null). A bind or validate refusal is exit 2, taken
only AFTER the full report prints (`--json`: `residueWorkflow: null` plus `residueError`), because
the stamps are already written and the report is the record of which plans they covered.

Exit 0 when every plan in the set ends CERTIFIED, 1 when at least one does not (the
report names which and why), 2 on a refusal that stopped the run.

Landed here (`coordinator/bin/mise-prep-run.py`, claude-klabauter) from coordinator-content-repo
`coordinator/bin/mise-prep-run.py`, docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W2-C9.
`_invoke` no longer spawns `sys.executable coordinator-invoke.py` per op — the DoE original's
one non-negotiable invocation tax this plan exists to remove. Both ops (`plan.prep_gate`,
`plan.stamp_prepped`) are called IN-PROCESS via `coordinator_core.ipc.get_op_handler`, the same
seam `coordinator_core/ops/cutover_advance.py` already uses to call `cutover.gate` from inside
another op's own handler. The upgrade leg is in-process too: `_upgrade` loads
`mise-prep-upgrade.py` by file path and calls its `upgrade_plan`, so the driver spawns no process
per plan (docs/plans/2026-10-21-mise-prep-run-full-set-fits-its-op-budget.md). `_ENTRY` now points at the sibling `coordinator/bin/mise-prep-entry.py` this
same chunk lands, not a DoE `skills/plan-blitz/` path.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib.util
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_NOT_ALL_CERTIFIED = 1
EXIT_REFUSED = 2

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
#: mise-prep's entry query, now a sibling bin CLI (this chunk lands both). Imported rather than
#: re-implemented: re-deriving the approved set from plan frontmatter would be a second answer to
#: a question the engine's gate settles.
_ENTRY = _HERE / "mise-prep-entry.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("mise_prep_entry", _ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@functools.cache
def _revalidator():
    """The census leg, loaded from its sibling file so prep, certify and fire share one executor."""
    path = _HERE / "mise-census-revalidate.py"
    spec = importlib.util.spec_from_file_location("mise_census_revalidate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CENSUS_ROUTE = (
    "route: the premise moved — re-plan the census and the body resting on it; a format-only or "
    "benign-growth ruling is coordinator:apm's, never a hand edit of result:"
)


def _default_engine_root() -> Path | None:
    """The engine root when neither `--engine-root` nor `$COORDINATOR_ENGINE_ROOT` names one:
    `cc_invoke.resolve_engine_root`'s ladder (own enclosing checkout, then the settings-home
    pointer file and registry) — the same answer `_invoke` and the entry query bind, so a copy
    that is not itself the engine still resolves one.

    Returns `None` on any failure rather than raising: an unresolved engine root here is not
    fatal to this driver the way it is to a gate — `_invoke` already reports its own refusal
    when the op cannot be resolved.
    """
    try:
        import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
        from cc_invoke import resolve_engine_root

        return Path(resolve_engine_root(__file__)).resolve()
    except Exception:  # noqa: BLE001 - unresolved is reported by the op call, not here
        return None


def _invoke(repo_root: Path, op: str, params: dict) -> dict:
    """One op call, IN-PROCESS — never a `coordinator-invoke.py` subprocess.

    Dispatched via `coordinator_core.invoke.dispatch.dispatch_message` — the same JSON-RPC
    envelope the subprocess route parsed, minus the process hop — against `repo_root` via
    `_origin_worktree`. Raises `ValueError` on any failure — unresolvable
    engine, an error envelope, or a malformed reply — so callers keep treating a refusal as a
    reply, never a crash.
    """
    try:
        import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
        import cc_invoke

        cc_invoke.require_engine_on_path(__file__)
        from coordinator_core.invoke.dispatch import dispatch_message
    except Exception as exc:
        raise ValueError(
            f"{op}: coordinator_core unresolvable: {exc} — set COORDINATOR_ENGINE_ROOT to the "
            "engine checkout or run the settings-home `mise-prep-run` launcher"
        )

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": op,
        "params": params,
        "_origin_worktree": str(repo_root),
    }
    try:
        reply = asyncio.run(
            dispatch_message(msg, caller="coordinator_core.roadmap.mise_prep_run")
        )
    except Exception as exc:
        raise ValueError(f"{op} failed: {exc}") from exc

    if not isinstance(reply, dict):
        raise ValueError(f"{op} failed: dispatch returned no reply envelope")
    if "error" in reply:
        err = reply["error"]
        message = str((err or {}).get("message") or err) if isinstance(err, dict) else str(err)
        raise ValueError(f"{op} refused: {message}")
    if "result" not in reply:
        raise ValueError(f"{op} returned a reply carrying neither `result` nor `error`")
    result = reply["result"]
    if not isinstance(result, dict):
        raise ValueError(f"{op} returned a non-dict result: {result!r}")
    return result


#: The op's bare verdicts collide with this command's own exit-2 `REFUSED`, so every caller-facing
#: surface spells them with the `SEAM-` prefix; the bare values live only in the op reply.
_SEAM_SPELLING = {"REFUSED": "SEAM-REFUSED", "DRIFT": "SEAM-DRIFT"}
_SEAM_OPS = {False: "plan.seam_record", True: "plan.seam_check"}
#: Each refusal carries its own route: the engine and the command text publish separately.
_ROUTE_AUTHOR = "route: coordinator:plan-author-residue, declare capabilities"
_ROUTE_APM = "route: coordinator:apm, scope"
_FINDING_FIELDS = ("capability", "missing_consumer", "counterpart_plan", "path", "row", "detail")


def _seam_call(repo_root: Path, plans: list[str], phase: str, named_set: bool, read_only: bool) -> dict:
    """One `plan.seam_record` (`plan.seam_check` when `read_only`) call over the whole set.

    A named set is checked with `universe` on: its gaps against open plans outside the set come
    back as non-blocking `missing-seam` findings rather than blocking `unpromised-export`, so an
    out-of-set dependency never refuses the stamp. The default set is every approved plan, so it
    has no outside worth the extra parse.

    Raises `ValueError` on an op error or a reply without `verdict`/`per_plan`: a seam check that
    did not answer is never a pass.
    """
    op = _SEAM_OPS[read_only]
    reply = _invoke(
        repo_root, op, {"plans": list(plans), "phase": phase, "named_set": named_set, "universe": named_set}
    )
    if not isinstance(reply.get("verdict"), str) or not isinstance(reply.get("per_plan"), dict):
        raise ValueError(f"{op} returned a reply without `verdict` and `per_plan`")
    return reply


def _seam_fix(repo_root: Path, plans: list[str], dry_run: bool) -> dict:
    """One `plan.seam_fix` call over the set, normalized to the report's `seamFix` shape.

    Runs before any gate so every stamp is computed over the edged body. The reply is returned
    once for the report and the downstream APM stage; callers never re-call the op. Raises
    `ValueError` on an op error.
    """
    reply = _invoke(repo_root, "plan.seam_fix", {"plans": list(plans), "dry_run": dry_run})
    return {
        "edgesAdded": list(reply.get("edges_added") or []),
        "skippedCycles": list(reply.get("skipped_cycles") or []),
        "converged": bool(reply.get("converged")),
        "files": list(reply.get("files") or []),
    }


_RESIDUE_SOURCE_NAME = "mise-prep-residue.mjs"
_RESIDUE_DIR = ("state", "mise-workflows")


def _emit_residue(repo_root: Path, rows: list[dict], seam_fix: dict) -> Path | None:
    """Build, bind, validate and write the residue workflow for this round; `None` when no
    plan needs authoring and no census or seam-fix item needs a ruling.

    Contract: writes `state/mise-workflows/<runId>.residue.mjs` and its emission receipt; fires
    nothing, spawns nothing of its own. Raises `ValueError` on a bind or validate refusal, so the
    caller exits 2 rather than printing a path to a script that would not run.
    """
    try:
        import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
        import cc_invoke

        cc_invoke.require_engine_on_path(__file__)
        from coordinator_core.ops.dispatch_emit import emit
        from coordinator_core.ops.dispatch_emit.emission_receipt import _write_emission_receipt
        from coordinator_core.roadmap import mise_residue
    except Exception as exc:
        raise ValueError(f"residue workflow: coordinator_core unresolvable: {exc}")

    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    )
    args = mise_residue.residue_args(
        rows,
        {"skipped_cycles": seam_fix["skippedCycles"]},
        repo_root=repo_root,
        run_id=run_id,
        gate_command=f"mise-prep-gate --repo-root {repo_root.as_posix()}",
        residue_agent_type=mise_residue.RESIDUE_AGENT_TYPE,
        residue_model=emit.resolve_agent_model(mise_residue.RESIDUE_AGENT_TYPE),
        apm_model=emit.resolve_agent_model(mise_residue.APM_AGENT_TYPE),
    )
    if not mise_residue.has_work(args):
        return None

    source = Path(mise_residue.__file__).resolve().parent / "workflows" / _RESIDUE_SOURCE_NAME
    script = _invoke(
        repo_root, "workflow.bind_args", {"script_path": str(source), "args": args}
    ).get("script")
    if not isinstance(script, str) or not script:
        raise ValueError("workflow.bind_args returned no script text")

    out_dir = repo_root.joinpath(*_RESIDUE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.residue.mjs"
    path.write_text(script, encoding="utf-8", newline="\n")

    verdict = _invoke(repo_root, "workflow.validate", {"script_path": str(path)})
    if not verdict.get("ok"):
        messages = "; ".join(str(f.get("message")) for f in verdict.get("findings") or [])
        raise ValueError(f"workflow.validate refused {path.name}: {messages}")

    _write_emission_receipt(
        path, None, {}, extras={"batches": len(args["batches"]), "apmItems": len(args["apmItems"])}
    )
    return path


def _seam_spell(verdict: str | None) -> str | None:
    return _SEAM_SPELLING.get(verdict, verdict)


def _seam_implicated(reply: dict, plan: str) -> bool:
    per_plan = reply.get("per_plan") or {}
    verdict = per_plan.get(plan, per_plan.get(Path(plan).as_posix()))
    return verdict in _SEAM_SPELLING


def _seam_findings_for(reply: dict, plan: str) -> list[dict]:
    """The findings that name `plan`. A finding carrying no `plan` key is attributed to every
    implicated plan, since the pinned reply names `counterpart_plan` but not the owner."""
    out = []
    for f in reply.get("findings") or []:
        if not isinstance(f, dict):
            continue
        owner = f.get("plan")
        if owner is None:
            if _seam_implicated(reply, plan) or not f.get("blocking"):
                out.append(f)
        elif plan in (owner, f.get("counterpart_plan")):
            out.append(f)
    return out


_COLLISION = "writes-collision"
_MISSING_SEAM = "missing-seam"
_MISSING_SEAM_SHOWN = 10


def _only_collisions_block(findings: list[dict]) -> bool:
    """True when `findings` hold at least one blocking `writes-collision` and no other blocking
    finding: a converged seam-fix's edges order exactly those, so the refusal would clear."""
    blocking = [f for f in findings if f.get("blocking")]
    return bool(blocking) and all(f.get("class") == _COLLISION for f in blocking)


def _is_missing_seam(finding: dict) -> bool:
    return finding.get("class") == _MISSING_SEAM and not finding.get("blocking")


def _missing_seam_section(rows: list[dict]) -> list[str]:
    """One bounded block of the non-blocking `missing-seam` findings, deduplicated across rows,
    kept apart from the per-plan verdict lines."""
    seen: dict[str, None] = {}
    for r in rows:
        for f in r.get("seam") or []:
            if _is_missing_seam(f):
                seen.setdefault(_seam_line(f), None)
    lines = list(seen)
    if not lines:
        return []
    out = [f"missing-seam: {len(lines)} non-blocking finding(s)"]
    out += [f"  {line}" for line in lines[:_MISSING_SEAM_SHOWN]]
    if len(lines) > _MISSING_SEAM_SHOWN:
        out.append(f"  … {len(lines) - _MISSING_SEAM_SHOWN} further finding(s) omitted")
    return out


def _seam_route(finding: dict) -> str:
    return _ROUTE_AUTHOR if finding.get("class") == "capabilities-undeclared" else _ROUTE_APM


def _seam_line(finding: dict) -> str:
    parts = [str(finding.get("class"))]
    parts += [f"{k}={finding[k]}" for k in _FINDING_FIELDS if finding.get(k) not in (None, "")]
    if not finding.get("blocking"):
        parts.append("(not blocking)")
    return "seam: " + " ".join(parts) + f" — {_seam_route(finding)}"


def _failing_classes(gate: dict) -> list[str]:
    return [
        f"{name}/{body.get('kind') or body.get('status')}"
        for name, body in (gate.get("classes") or {}).items()
        if body.get("status") != "PASS"
    ]


# WHAT THE CLASS ACTUALLY SAID. The repair line tells the reader to correct "the named field", and
# until this existed nothing named it: `_failing_classes` kept the class name and threw the body
# away, `--json` carried only the class string, and the one tool that would have printed detail is
# the gate — which, on the very plans that fail here, says PREPPED. So the operator was pointed at
# a class body reachable from nowhere, and example-store-repo-fb was reduced to diffing a failing plan
# against a passing one to guess at the field. A repair instruction that names an artifact the tool
# does not emit is `A-DIAGNOSTIC-THAT-NAMES-A-CAUSE-IT-DID-NOT-OBSERVE` wearing its other face.
#
# The body's shape belongs to the engine and will grow keys this does not know, so this renders
# whatever detail it carries rather than a fixed field list: preferred keys first, then any
# remaining non-bookkeeping key, so a new one surfaces instead of being silently dropped.
_DETAIL_KEYS = ("message", "reason", "detail", "details", "field", "fields", "errors", "offenders")
_CLASS_BOOKKEEPING = {"status", "kind"}


def _failing_details(gate: dict) -> list[str]:
    out = []
    for name, body in (gate.get("classes") or {}).items():
        if not isinstance(body, dict) or body.get("status") == "PASS":
            continue
        seen, parts = set(), []
        for key in _DETAIL_KEYS:
            if body.get(key) not in (None, "", [], {}):
                seen.add(key)
                parts.append(f"{key}={body[key]!r}")
        for key, value in body.items():
            if key in seen or key in _CLASS_BOOKKEEPING:
                continue
            if value not in (None, "", [], {}):
                parts.append(f"{key}={value!r}")
        if parts:
            out.append(f"{name}: {'; '.join(parts)}")
    return out


# `mise-prep-upgrade` writes DECLARATIONS DERIVABLE FROM A PLAN'S OWN TEXT. A schema class rejects
# a value the plan already states, so `--upgrade` has nothing to derive and writes nothing — the
# blanket "repair: mise-prep-upgrade <plan>" is then a no-op that sends the reader in a circle,
# reported by example-retrieval-repo-4a after running it across a whole set for zero writes.
#
# This line names only what the runner OBSERVED: which class refused, and that the upgrade path
# cannot reach it. It deliberately names no cause.
_NOT_UPGRADEABLE = ("schema",)


def _sizing_object_citation(plan: str) -> str:
    """This plan's own top-level `sizing_object:`, when it resolves on disk.

    example-retrieval-repo-ue-addon measured 21 SCHEMA refusals on one corpus, every one a
    `prime_exit_criterion.derived_from` carrying narrative where the schema wants an openable
    link — and in 17 of 17 repaired by hand, the correct pointer was this same field, sitting in
    the same frontmatter. Naming it costs a line-scan; not naming it cost them four dispatched
    agents. Returns "" on any read failure: a repair line may name only what was read.
    """
    try:
        with open(plan, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.startswith("sizing_object:"):
            continue
        value = line.split(":", 1)[1].strip().strip("'\"")
        return value if value and os.path.exists(value) else ""
    return ""


def _repair_line(failing: list[str], plan: str) -> str:
    """The repair for THIS row's failing classes, never one constant string."""
    blocked = sorted({
        cls.split("/", 1)[0] for cls in failing
        if cls.split("/", 1)[0].lower().startswith(_NOT_UPGRADEABLE)
    })
    if blocked:
        candidate = _sizing_object_citation(plan)
        derived = (
            f" This plan's own `sizing_object: {candidate}` resolves on disk; where the refused "
            f"field is `prime_exit_criterion.derived_from`, that is usually the value it wants. "
            f"Relocate the prose it currently holds into the plan body rather than dropping it, "
            f"and re-run — a body edit invalidates `mise_prepped_sha`."
            if candidate
            else ""
        )
        return (
            f"repair: NOT `--upgrade` — it writes declarations derivable from the plan's own "
            f"text, and {', '.join(blocked)} refuses a value the plan already states, so the "
            f"upgrade path does not write it. Read that class's own body for the named field and "
            f"correct it BY HAND in {plan} — unless the field is engine-written (`mise_prepped*`, "
            f"`execution_authorized_*`), which has a producer and must be repaired there instead."
            f"{derived}"
        )
    return f"repair: mise-prep-upgrade {plan}  (or --upgrade), then re-run"


@functools.cache
def _upgrader(engine_root: Path | None):
    """`mise-prep-upgrade`'s module, loaded once per run from the driver's own `coordinator/bin/`.

    A foreign `engine_root` is refused (returns a reason string, not a module): loading its
    `mise-prep-upgrade.py` here would run that file's import-time `sys.path` insert against the
    `coordinator_core` already cached in `sys.modules`, so the tree that answers would not be the
    tree named. `None` and this driver's own tree are the same engine.
    """
    own = _HERE / "mise-prep-upgrade.py"
    if engine_root is not None and engine_root != _REPO_ROOT:
        return f"engine root {engine_root} is not the tree this driver runs from ({_REPO_ROOT})"
    if not own.is_file():
        return f"no mise-prep-upgrade.py at {own}"
    spec = importlib.util.spec_from_file_location("mise_prep_upgrade", own)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _upgrade(
    repo_root: Path, engine_root: Path | None, plan: str, dry_run: bool = False
) -> tuple[str, str]:
    """Upgrade one plan IN-PROCESS via `mise-prep-upgrade.upgrade_plan`. Returns (state, note).

    `state` is "wrote" (the tool changed, or under `dry_run` would change, the plan),
    "declined" (pure residue, nothing to derive, or not a plan), or "unavailable" (the upgrade
    module could not be loaded). `note` carries the derived fields plus any residue, e.g.
    `wrote prime_exit_criterion.derived_from; AUTHOR: prime_exit_criterion.statement: ...`.

    `dry_run` is `check=True`: nothing is written. `plan` is repo-relative and resolved against
    `repo_root`, never the process cwd.

    Delegated, never reimplemented: what is derivable from a plan's own text is that tool's
    judgment, and a second deriver here would author declarations it never agreed to.
    """
    upgrader = _upgrader(engine_root)
    if isinstance(upgrader, str):
        return "unavailable", upgrader
    path = repo_root / plan
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "declined", f"unreadable: {exc}"
    parsed = upgrader.parse_frontmatter(text)
    reason = upgrader.not_a_plan_reason(path, text, parsed)
    if reason is not None:
        return "declined", f"not a plan: {reason}"
    result = upgrader.upgrade_plan(path, check=dry_run, text=text, parsed=parsed)
    if result["error"]:
        return "declined", result["error"]
    residue = result["residue"]
    if result["changed"]:
        derived = ", ".join(result["derived"])
        note = f"wrote {derived}" if derived else "wrote"
        if residue:
            note += "; AUTHOR: " + "; ".join(residue)
        return "wrote", note
    if residue:
        return "declined", "AUTHOR: " + "; AUTHOR: ".join(residue)
    return "declined", "nothing to derive"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mise-prep-run",
        description="Gate and stamp a set of plans; name each one's certification state.",
    )
    ap.add_argument("plans", nargs="*", help="plan paths; default: every approved plan")
    ap.add_argument("--repo-root", required=True, help="ABSOLUTE repo root")
    ap.add_argument("--engine-root", help="engine checkout (default: $COORDINATOR_ENGINE_ROOT)")
    ap.add_argument("--roadmap-id", help="narrow the default set to one roadmap")
    ap.add_argument(
        "--upgrade",
        action="store_true",
        help="run mise-prep-upgrade over a plan that fails the bar, then re-gate. Writes "
        "only the declarations derivable from the plan's own text; it invents none, so a "
        "plan needing real authoring still comes back NOT-PREPPED with its classes named.",
    )
    ap.add_argument(
        "--census-timeout", type=int, default=30,
        help="seconds one census command may run before it reads UNRUNNABLE (default 30)",
    )
    ap.add_argument("--dry-run", action="store_true", help="gate and report; stamp nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    _er = args.engine_root or os.environ.get("COORDINATOR_ENGINE_ROOT") or ""
    engine_root = Path(_er).resolve() if _er.strip() else _default_engine_root()
    if args.engine_root:
        os.environ["COORDINATOR_ENGINE_ROOT"] = str(engine_root)

    def refuse(msg: str) -> int:
        print(f"mise-prep-run: REFUSED — {msg}", file=sys.stderr)
        return EXIT_REFUSED

    plans = list(args.plans)
    skipped: list[str] = []
    if not plans:
        try:
            entry = _load_entry()
            rows = entry.approved_plans(repo_root, args.roadmap_id, False)
        except Exception as exc:  # noqa: BLE001 - the entry query owns its own errors
            return refuse(f"could not read the approved-plan set: {exc}")
        plans = [r["plan"] for r in rows]
        # `plan.seam_fix` writes docs/plans/ plans only and raises on anything else, which would
        # take the whole run down; an out-of-tree plan (an archived spec) is reported, not fired.
        skipped = [p for p in plans if not p.replace("\\", "/").startswith("docs/plans/")]
        plans = [p for p in plans if p not in skipped]
        for p in skipped:
            print(
                f"mise-prep-run: SKIPPED-OUT-OF-TREE {p} — outside docs/plans/, not certifiable",
                file=sys.stderr,
            )
        if not plans:
            print("mise-prep-run: no approved plans — nothing to certify")
            return EXIT_OK
        # SAY WHAT THE SET IS MADE OF. The selection reads BOTH plan-blitz exits — `status:
        # approved` and the S lane's execution-ready park, whose plan stays `draft` by design — so
        # a count that moves with no approval in sight is ordinary.
        drafts = [
            p for p in plans
            if "status: approved" not in (repo_root / p).read_text(
                encoding="utf-8", errors="replace")[:4000]
        ] if all((repo_root / p).is_file() for p in plans) else []
        print(
            f"mise-prep-run: certification set — {len(plans)} plan(s)"
            + (f", {len(drafts)} entering from the execution-ready lane (plan still draft by "
               f"design): {', '.join(drafts[:5])}"
               f"{' …' if len(drafts) > 5 else ''}" if drafts else ""),
            # --json owns stdout: a prose line ahead of the document makes it unparseable.
            file=sys.stderr if args.json else sys.stdout,
        )

    try:
        seam_fix = _seam_fix(repo_root, plans, args.dry_run)
    except ValueError as exc:
        return refuse(f"seam fix: {exc}")

    rows = []
    gates: dict[str, dict] = {}
    for plan in plans:
        try:
            gate = _invoke(repo_root, "plan.prep_gate", {"plan": plan})
        except ValueError as exc:
            return refuse(f"{plan}: {exc}")

        upgraded = None
        if gate.get("verdict") != "PREPPED" and args.upgrade:
            state, note = _upgrade(repo_root, engine_root, plan, dry_run=args.dry_run)
            wrote = state == "wrote"
            upgraded = note if wrote else f"{state}: {note}"
            # Re-gate whenever the upgrade changed something: the ordinary outcome is a write
            # alongside residue. Never under `--dry-run`: `_upgrade` ran check-only and wrote
            # nothing, so a re-gate would just repeat the same verdict.
            if wrote and not args.dry_run:
                try:
                    gate = _invoke(repo_root, "plan.prep_gate", {"plan": plan})
                except ValueError as exc:
                    return refuse(f"{plan}: re-gate after upgrade: {exc}")

        # The gate already carries the attest's own four-state read (`plan.prep_gate`
        # returns `read_stamp`'s answer). Carrying it here is what makes a dry run
        # answer the question a dry run is asked: not "would these pass the bar" but
        # "how many of them does this run actually write". Without it every plan in the
        # set reads `would-stamp`, CERTIFIED ones included, and a set with nothing to do
        # is indistinguishable from a set where all of it is pending.
        gates[plan] = gate
        row = {
            "plan": plan,
            "verdict": gate.get("verdict"),
            "failing": _failing_classes(gate),
            "detail": _failing_details(gate),
            "withheld_rows": gate.get("withheld_rows") or [],
            "upgraded": upgraded,
            "stamp_state": (gate.get("stamp") or {}).get("state"),
            "outcome": None,
        }
        rows.append(row)

    named_set = bool(args.plans or args.roadmap_id)
    try:
        seam = _seam_call(repo_root, plans, "prep", named_set, read_only=args.dry_run)
    except ValueError as exc:
        return refuse(f"seam check: {exc}")

    projecting = args.dry_run and seam_fix["converged"] and not seam_fix["skippedCycles"]
    for row in rows:
        plan = row["plan"]
        row["seam"] = _seam_findings_for(seam, plan)
        if _seam_implicated(seam, plan):
            if projecting and _only_collisions_block(row["seam"]):
                row["seam_cleared_by_fix"] = True
            else:
                row["outcome"] = "SEAM-REFUSED"

    # Census leg: runs only on rows about to be stamped. A stamped, refused or non-PREPPED row
    # writes no stamp, and the fire-time leg owns already-certified plans. Only DRIFT blocks, the
    # same rule the fire leg uses: an UNDECIDABLE or UNRUNNABLE census is not evidence of movement.
    for row in rows:
        row["census"] = None
        if (
            row["outcome"] is None
            and row["verdict"] == "PREPPED"
            and row["stamp_state"] != "CERTIFIED"
        ):
            rep = _revalidator().revalidate(
                repo_root / row["plan"], repo_root, args.census_timeout, require_stamp=False
            )
            row["census"] = rep
            if rep.get("state") == "DRIFT":
                row["outcome"] = "CENSUS-DRIFT"

    for row in rows:
        plan, gate = row["plan"], gates[row["plan"]]
        if row["outcome"] in ("SEAM-REFUSED", "CENSUS-DRIFT"):
            continue
        if gate.get("verdict") == "PREPPED" and not args.dry_run:
            try:
                stamp = _invoke(repo_root, "plan.stamp_prepped", {"plan": plan})
            except ValueError as exc:
                return refuse(f"{plan}: {exc}")
            # A refusal is a REPLY here, not an exception — the per-class breakdown is
            # what tells an author where the fix lands, so it is carried, not raised.
            row["outcome"] = stamp.get("outcome")
            row["mise_prepped_findings"] = stamp.get("mise_prepped_findings") or []
            if stamp.get("outcome") == "refused":
                row["verdict"] = stamp.get("verdict") or row["verdict"]
                row["failing"] = _failing_classes(stamp) or row["failing"]
                row["detail"] = _failing_details(stamp) or row["detail"]
        elif gate.get("verdict") == "PREPPED":
            row["outcome"] = (
                "already-certified"
                if row["stamp_state"] == "CERTIFIED"
                else "would-stamp"
            )

    # In a dry run `would-stamp` IS the pass — the gate returned PREPPED and only the
    # write was withheld. Counting it as short would report a clean set as a failing one
    # and send an author to fix plans that need nothing, which is the wrong half of the
    # report to get wrong: a dry run exists to be believed before anything is written.
    passing = {"stamped", "already-certified"} | ({"would-stamp"} if args.dry_run else set())
    certified = [r for r in rows if r["outcome"] in passing]
    short = [r for r in rows if r not in certified]

    residue_path, residue_error = None, None
    try:
        residue_path = _emit_residue(repo_root, rows, seam_fix)
    except (ValueError, OSError) as exc:
        residue_error = str(exc)

    if args.json:
        print(
            json.dumps(
                {
                    "plans": rows,
                    "certified": len(certified),
                    "dryRun": args.dry_run,
                    "skipped": [{"plan": p, "outcome": "SKIPPED-OUT-OF-TREE"} for p in skipped],
                    "seam": _seam_spell(seam["verdict"]),
                    "seamFix": seam_fix,
                    "residueWorkflow": (
                        residue_path.relative_to(repo_root).as_posix() if residue_path else None
                    ),
                    **({"residueError": residue_error} if residue_error else {}),
                },
                indent=2,
            )
        )
    else:
        verb = "would certify" if args.dry_run else "certified"
        mode = " (dry run — nothing stamped)" if args.dry_run else ""
        pending = sum(1 for r in rows if r["outcome"] == "would-stamp")
        work = f" — {pending} would be written" if args.dry_run else ""
        print(f"mise-prep-run: {len(certified)} of {len(rows)} plan(s) {verb}{mode}{work}")
        n_edges = len(seam_fix["edgesAdded"])
        print(
            f"seam-fix: {n_edges} edge(s) {'would be added' if args.dry_run else 'added'}; "
            f"converged={seam_fix['converged']}"
        )
        for c in seam_fix["skippedCycles"]:
            print(f"seam-fix: skipped cycle {c.get('plans')} path={c.get('path')} — {c.get('detail')}")
        if projecting:
            remain = [r["plan"] for r in rows if r["outcome"] == "SEAM-REFUSED"]
            cleared = sum(1 for r in rows if r.get("seam_cleared_by_fix"))
            print(
                f"seam-fix: once the edges land, {len(remain)} plan(s) would remain SEAM-REFUSED"
                f" ({cleared} writes-collision refusal(s) cleared)"
                + (f": {', '.join(remain)}" if remain else "")
            )
        for r in rows:
            if r in certified:
                held = r.get("mise_prepped_findings") or []
                tail = f"  [{len(held)} row(s) withheld: {', '.join(held)}]" if held else ""
                print(f"  {r['outcome']:<18} {r['plan']}{tail}")
                for f in r.get("seam") or []:
                    if not _is_missing_seam(f):
                        print(f"                     {_seam_line(f)}")
            else:
                print(f"  {(r['outcome'] or r['verdict'] or 'NOT-PREPPED'):<18} {r['plan']}")
                if r["failing"]:
                    print(f"                     bar: {', '.join(r['failing'])}")
                for line in r.get("detail") or []:
                    print(f"                     said: {line}")
                if r["upgraded"]:
                    print(f"                     upgrade: {r['upgraded']}")
                elif r["verdict"] != "PREPPED":
                    print(f"                     {_repair_line(r['failing'], r['plan'])}")
                for f in r.get("seam") or []:
                    if not _is_missing_seam(f):
                        print(f"                     {_seam_line(f)}")
                if r["outcome"] == "CENSUS-DRIFT":
                    for e in (r["census"] or {}).get("entries") or []:
                        if e.get("state") == "DRIFT":
                            print(
                                f"                     census: DRIFT {str(e.get('command'))[:110]} "
                                f"— {e.get('basis')}"
                            )
                    print(f"                     {_CENSUS_ROUTE}")

    if not args.json:
        for line in _missing_seam_section(rows):
            print(line)

    if residue_path:
        sys.stdout.flush()
        print(
            f"\nmise-prep-run: residue workflow written — "
            f"{residue_path.relative_to(repo_root).as_posix()}\n"
            f'Workflow({{scriptPath: "{residue_path.as_posix()}"}})',
            file=sys.stderr if args.json else sys.stdout,
        )

    if residue_error:
        sys.stdout.flush()
        return refuse(f"residue workflow: {residue_error}")

    if short:
        refused = [r for r in short if r["outcome"] == "SEAM-REFUSED"]
        drifted = [r for r in short if r["outcome"] == "CENSUS-DRIFT"]
        unprepped = len(short) - len(refused) - len(drifted)
        lines = []
        if unprepped:
            lines.append(
                f"\nmise-prep-run: {unprepped} plan(s) are not certified. A plan that fails the "
                "bar needs authoring, not a stamp — the classes above name where. "
                + (
                    "Fire the residue workflow printed above, then re-run."
                    if residue_path
                    else "Author it via /mise-prep, then re-run."
                )
            )
        if refused:
            lines.append(
                f"\nmise-prep-run: {len(refused)} plan(s) are SEAM-REFUSED. Follow each "
                "finding's own `route:` line above; re-authoring does not clear them."
            )
        if drifted:
            lines.append(
                f"\nmise-prep-run: {len(drifted)} plan(s) are CENSUS-DRIFT. The premise moved — "
                "re-plan the census and the body resting on it; a format-only or benign-growth "
                "ruling is coordinator:apm's, never a hand edit of result:."
            )
        # stdout is block-buffered on a pipe while stderr is line-buffered, so without this the
        # summary overtakes the report on a shared stream.
        sys.stdout.flush()
        print("\n".join(lines), file=sys.stderr)
        return EXIT_NOT_ALL_CERTIFIED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
