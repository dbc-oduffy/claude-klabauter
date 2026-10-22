"""
coordinator_core.ops.plan_mise_prep_probe — JSON-RPC "plan.mise_prep_probe",
"plan.mise_upgrade_apply" and "plan.mise_certify_probe" operations.

Purpose: the three per-plan ops a client-side ``mise-prep-run`` / ``mise-certify`` loop
issues, so a full-set run is N short ops and never one long one. Each is a thin composition of reads that
already exist; this module's only original code is param validation, the HEAD-keyed
corpus cache and the census/upgrade gating rules.

``plan.mise_prep_probe`` (COMPUTE_ONLY) answers, for ONE plan, in one reply: the gate
verdict and the upgrade derivation.
``plan.mise_upgrade_apply`` (MUTATING) writes the derivable fields of ONE plan.

Wire params, probe:
    plan           (str, required)   — repo-relative or absolute; inside the main worktree.
    upgrade        (bool, default F) — derive what ``mise-prep-upgrade`` would write.
    check_census, census_timeout, census_run — admitted for a launcher that still sends them;
                     never read.

Reply, probe:
    {"gate": <plan.prep_gate reply, same shape>, "census": None,
     "upgrade": <upgrade_plan dict>|{"skipped": "not upgradeable"}|None,
     "process_ms": float}

Set form: ``plans`` (non-empty list of non-empty strings) in place of ``plan``; exactly one of
the two. ``upgrade`` applies to every plan. Reply:
    {"probes": {<plan string as sent>: {"gate", "census", "upgrade"} | {"error": text}},
     "store_hits": int, "store_misses": int, "process_ms": float}
Each plan is answered by the same ``_probe_one`` the single form uses, or from the on-disk
``ProbeStore`` when its bytes, HEAD, engine key and recorded external inputs are unchanged. A
ValueError on one plan is that plan's ``{"error"}``; the rest are still answered.

There is no census op: no census re-runs on mise-prep or mise-certify.

Wire params, apply: ``plan`` (str, required).
Reply, apply: ``{"plan", "error", "derived", "residue", "changed"}`` — ``upgrade_plan``'s dict.

``plan.mise_certify_probe`` (COMPUTE_ONLY) is ``mise-certify.py :: certify`` for ONE plan:
the sha leg only; the census leg is deleted.
Wire params, certify: ``plan`` (str, required), ``census_timeout`` (admitted, ignored).
Reply, certify: ``{"plan", "sha", "sha_detail"?, "stamp": <read_stamp dict>|None,
"census": census.not_run_outcome(...), "process_ms"}`` — the census is NOT-RUN for every plan.
An unreadable plan replies sha MALFORMED with the OSError text; a plan absent from the
worktree raises ValueError.

Negative-spec:
  - The probe and certify run no census.
  - The single form takes ONE plan; the set form takes a list and is the only caller shape that
    scales: a per-plan loop measured ~10.0s warm over a 448-plan set (448 spawns, YAML loading
    5.21s). The single form never reads or writes the store.
  - The store holds only a reply that answered; a plan whose resolution, read or gate raised
    ValueError is never stored. Every key leg is checked before a stored reply is served, and a
    leg is never narrowed to get a hit.
  - Does NOT write. The probe runs ``upgrade_plan(check=True)``; only
    ``plan.mise_upgrade_apply`` writes.
  - Does NOT re-implement the census executor or the deriver: both are loaded by file path
    from THIS engine's ``coordinator/bin/`` and called. Never a foreign root.
  - Does NOT serve a corpus across a HEAD move. ``CorpusInputs`` is cached on
    (repo_root, HEAD sha); an unreadable HEAD is never cached. A top-level directory
    created without a commit, or a ``.gitignore`` edit made without one, is therefore not seen
    until HEAD moves.
  - ``process_ms`` is this process's CPU time over the op body, an upper bound: concurrent ops
    in one warm server share the clock.
"""

from __future__ import annotations

import functools
import importlib.util
import time
from pathlib import Path
from typing import Optional

MUTATES = ["docs/plans/*.md"]  # plan.mise_upgrade_apply writes the derivable fields of the caller-named plan only

from coordinator_core.bin_lib_binding import exec_module_bin_bound
from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.engine_version import engine_build
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops.plan_prep_gate import _resolve_plan
from coordinator_core.roadmap import census
from coordinator_core.roadmap.probe_store import ProbeStore, engine_key, input_paths, plan_sha
from coordinator_core.roadmap.prep_gate import (
    PREPPED,
    CorpusInputs,
    corpus_inputs,
    gate_plan_with_corpus,
    read_stamp,
)

GENERATES: list = []

#: Failing gate classes the upgrade path cannot reach: a schema class refuses a value the
#: plan already states, so there is nothing derivable to write.
_NOT_UPGRADEABLE = ("schema",)

_BIN_DIR = ENGINE_CHECKOUT_ROOT / "coordinator" / "bin"

_CORPUS_CACHE: dict = {}

_CENSUS_DELETED = "census revalidation leg deleted (DR-344 kill bar)"


@functools.cache
def _load_bin(filename: str, modname: str):
    """A sibling ``coordinator/bin`` script of THIS engine, loaded once by file path."""
    path = _BIN_DIR / filename
    if not path.is_file():
        raise ValueError(f"engine has no {path}")
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    exec_module_bin_bound(spec.loader, mod, str(_BIN_DIR))
    return mod


def _upgrader():
    return _load_bin("mise-prep-upgrade.py", "mise_prep_upgrade")


def _head_sha(worktree_root: Path) -> Optional[str]:
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.repo_root import _walk_for_repo

    try:
        found = _walk_for_repo(worktree_root)
        return head_sha(found[1]) if found is not None else None
    except OSError:
        return None


def _corpus(worktree_root: Path) -> tuple[CorpusInputs, Optional[str]]:
    """``(CorpusInputs, head)`` for the root, derived once per (root, HEAD sha).

    ``head`` is the sha the cache keyed on (None when unreadable).
    """
    head = _head_sha(worktree_root)
    if head is None:
        return corpus_inputs(worktree_root), None
    key = (str(worktree_root), head)
    cached = _CORPUS_CACHE.get(key)
    if cached is None:
        _CORPUS_CACHE.clear()
        cached = _CORPUS_CACHE[key] = corpus_inputs(worktree_root)
    return cached, head


def _rel(plan_path: Path, worktree_root: Path) -> str:
    try:
        return plan_path.relative_to(worktree_root).as_posix()
    except ValueError:  # pragma: no cover - contained_path already refused this
        return plan_path.as_posix()


def _plan_param(params: dict, op: str, repo_root: Optional[Path]) -> Path:
    if repo_root is None:
        raise ValueError(f"{op} requires a resolved repo_root")
    raw = aliased_param(params, "plan", "plan_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"{spellings('plan', 'plan_path')} must be a non-empty string naming one plan file"
        )
    return _resolve_plan(raw.strip(), Path(main_worktree_root(repo_root)))


def _bool_param(params: dict, name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _not_upgradeable(gate: dict) -> bool:
    failing = [
        name for name, body in (gate.get("classes") or {}).items()
        if body.get("status") != "PASS"
    ]
    return bool(failing) and all(n.lower().startswith(_NOT_UPGRADEABLE) for n in failing)


def _upgrade_dict(result: dict, worktree_root: Path) -> dict:
    out = dict(result)
    out["plan"] = _rel(Path(result["plan"]), worktree_root)
    return out


def _probe_one(plan_path: Path, worktree_root: Path, corpus: CorpusInputs, *,
               upgrade: bool, build: dict, text: Optional[str] = None) -> dict:
    """One plan's reply, less ``process_ms``: the single form and the set form share it."""
    if text is None:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    report = gate_plan_with_corpus(plan_path, corpus, text=text)
    stamp = read_stamp(text)
    gate = {
        "plan": _rel(plan_path, worktree_root),
        "verdict": report["verdict"],
        "withheld_rows": report["withheld_rows"],
        "classes": report["classes"],
        "message": report["message"],
        "stamp": stamp,
        "engine_build": build,
    }

    derivation = None
    if upgrade and report["verdict"] != PREPPED:
        if _not_upgradeable(gate):
            derivation = {"skipped": "not upgradeable"}
        else:
            derivation = _upgrade_dict(
                _upgrader().upgrade_plan(plan_path, check=True, text=text), worktree_root
            )

    return {"gate": gate, "census": None, "upgrade": derivation}


def _ms_since(started: float) -> float:
    return round((time.process_time() - started) * 1000.0, 3)


def _stat_triple(path: Path) -> Optional[list]:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def _input_identities(text: str, worktree_root: Path) -> dict:
    """``{rel: [size, mtime_ns, ino] | None}`` for every external input the verdict could read.

    The frontmatter and spine come from the memoised shared parses the gate just used; both
    documents are read-only here.
    """
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit.spine_read import (
        load_frontmatter_doc,
        load_rows_memo,
        split_frontmatter_memo,
    )

    fm: dict = {}
    split = split_frontmatter_memo(text)
    if split is not None:
        try:
            loaded = load_frontmatter_doc(split.fm_text)
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            fm = loaded
    spine = load_rows_memo(text)
    rows = spine.rows if spine.status is LocateStatus.LOCATED else []
    return {rel: _stat_triple(worktree_root / rel) for rel in input_paths(fm, rows, worktree_root)}


def _probe_set(plans: list, upgrade: bool, repo_root: Path) -> dict:
    """The set form's reply: one corpus, one build, one store load and one save per call.

    ``engine_key`` is taken once at call start and used for every get and put: it is a
    function of the engine files alone (None once any closure file is newer than import).
    """
    worktree_root = Path(main_worktree_root(repo_root))
    started = time.process_time()
    corpus, head = _corpus(worktree_root)
    build = engine_build()
    key = engine_key(build.get("engine_sha"))
    store = ProbeStore.load(worktree_root)
    hits = misses = 0
    probes: dict = {}
    for raw in plans:
        try:
            plan_path = _resolve_plan(raw.strip(), worktree_root)
            data = plan_path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            if "\r" in text:
                text = text.replace("\r\n", "\n").replace("\r", "\n")
            rel = _rel(plan_path, worktree_root)
            sha = plan_sha(data)
            stored = store.get(rel, sha, upgrade, head, key)
            if stored is not None:
                hits += 1
                probes[raw] = stored
                continue
            reply = _probe_one(plan_path, worktree_root, corpus,
                               upgrade=upgrade, build=build, text=text)
            if key is not None:
                store.put(rel, sha, upgrade, head, key,
                          _input_identities(text, worktree_root), reply)
            misses += 1
            probes[raw] = reply
        except ValueError as exc:
            probes[raw] = {"error": str(exc)}
    store.save()
    return {"probes": probes, "store_hits": hits, "store_misses": misses,
            "process_ms": _ms_since(started)}


@register_op("plan.mise_prep_probe")
def _probe_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.mise_prep_probe" handler. See module docstring."""
    has_plan = "plan" in params or "plan_path" in params
    if "plans" in params:
        if has_plan:
            raise ValueError("send plan or plans, not both")
        if repo_root is None:
            raise ValueError("plan.mise_prep_probe requires a resolved repo_root")
        plans = params["plans"]
        if (
            not isinstance(plans, list)
            or not plans
            or not all(isinstance(p, str) and p.strip() for p in plans)
        ):
            raise ValueError("plans must be a non-empty list of plan path strings")
        return _probe_set(plans, _bool_param(params, "upgrade"), repo_root)

    plan_path = _plan_param(params, "plan.mise_prep_probe", repo_root)
    upgrade = _bool_param(params, "upgrade")

    started = time.process_time()
    worktree_root = Path(main_worktree_root(repo_root))
    corpus, _ = _corpus(worktree_root)
    reply = _probe_one(plan_path, worktree_root, corpus, upgrade=upgrade, build=engine_build())
    reply["process_ms"] = _ms_since(started)
    return reply


@register_op("plan.mise_upgrade_apply")
def _apply_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.mise_upgrade_apply" handler. See module docstring."""
    plan_path = _plan_param(params, "plan.mise_upgrade_apply", repo_root)
    worktree_root = Path(main_worktree_root(repo_root))
    # Writes are confined to docs/plans/ (the seam_record sibling rule); other plan paths are refused.
    if not _rel(plan_path, worktree_root).startswith("docs/plans/"):
        raise ValueError(f"plan.mise_upgrade_apply writes docs/plans/ plans only: {params!r}")
    upgrader = _upgrader()
    text = plan_path.read_text(encoding="utf-8", errors="replace")
    parsed = upgrader.parse_frontmatter(text)
    reason = upgrader.not_a_plan_reason(plan_path, text, parsed)
    if reason is not None:
        raise ValueError(f"not a plan: {reason}")
    return _upgrade_dict(
        upgrader.upgrade_plan(plan_path, check=False, text=text, parsed=parsed), worktree_root
    )


@register_op("plan.mise_certify_probe")
def _certify_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.mise_certify_probe" handler. See module docstring."""
    plan_path = _plan_param(params, "plan.mise_certify_probe", repo_root)
    started = time.process_time()
    worktree_root = Path(main_worktree_root(repo_root))
    rel = _rel(plan_path, worktree_root)
    try:
        text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "plan": rel, "sha": "MALFORMED", "sha_detail": str(exc), "stamp": None,
            "census": census.not_run_outcome(_CENSUS_DELETED), "process_ms": round((time.process_time() - started) * 1000.0, 3),
        }
    stamp = read_stamp(text)
    reply: dict = {"plan": rel, "sha": stamp["state"]}
    if stamp["state"] == "STALE":
        reply["sha_detail"] = f"recorded {stamp['recorded_sha']}, body {stamp['body_sha']}"
    elif stamp["state"] == "MALFORMED":
        reply["sha_detail"] = f"stamp fields missing: {', '.join(stamp.get('missing') or [])}"
    reply["stamp"] = stamp
    reply["census"] = census.not_run_outcome(_CENSUS_DELETED)
    reply["process_ms"] = round((time.process_time() - started) * 1000.0, 3)
    return reply
